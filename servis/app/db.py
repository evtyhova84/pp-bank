"""Хранилище сервиса: SQLite.

Почему SQLite, а не Postgres: сервис ставится на сервер организации, пачек
в день — десятки, а не тысячи. Один файл проще администрировать и бэкапить,
а транзакции нужны ровно в одном месте — при выдаче номеров платёжек, и их
SQLite даёт честно. Переход на Postgres, если понадобится, затронет этот файл.

Мультиарендность заложена сразу: `company_id` есть в каждой таблице с данными.
Прикрутить её потом, когда в базе уже лежат чужие платежи, нельзя.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "dannye"
DB_PATH = DATA_DIR / "servis.db"

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- организация-арендатор: её пользователи и данные не видят соседей
CREATE TABLE IF NOT EXISTS companies (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES companies(id),
    login         TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    full_name     TEXT    NOT NULL DEFAULT '',
    -- operator: грузит счета и формирует файл; admin: плюс справочники и люди
    role          TEXT    NOT NULL DEFAULT 'operator',
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- плательщик: организация, от чьего имени платим. Реквизитов банка здесь нет —
-- счетов у организации бывает несколько (Точка, Сбербанк), и они в payer_accounts.
CREATE TABLE IF NOT EXISTS payers (
    id         INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    code       TEXT    NOT NULL,
    name       TEXT    NOT NULL,
    full_name  TEXT    NOT NULL DEFAULT '',
    inn        TEXT    NOT NULL,
    kpp        TEXT    NOT NULL DEFAULT '',
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL,
    UNIQUE (company_id, code)
);

-- расчётный счёт плательщика. Своя нумерация платёжек у каждого: счётчик
-- ведётся по ключу (плательщик, счёт, год), у банков она независимая.
CREATE TABLE IF NOT EXISTS payer_accounts (
    id              INTEGER PRIMARY KEY,
    payer_id        INTEGER NOT NULL REFERENCES payers(id),
    metka           TEXT    NOT NULL DEFAULT '',
    account         TEXT    NOT NULL,
    bank_name       TEXT    NOT NULL,
    bank_city       TEXT    NOT NULL DEFAULT '',
    bic             TEXT    NOT NULL,
    corr_account    TEXT    NOT NULL,
    numbering_start INTEGER NOT NULL DEFAULT 900001,
    bank_profile    TEXT    NOT NULL DEFAULT 'default',
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL,
    UNIQUE (payer_id, account)
);

-- справочник получателей: кому эта компания платила и по каким реквизитам.
-- Набирается сам из разобранных счетов. Один контрагент может иметь несколько
-- счетов, поэтому ключ — (компания, ИНН, счёт), а не просто ИНН.
CREATE TABLE IF NOT EXISTS poluchateli (
    id           INTEGER PRIMARY KEY,
    company_id   INTEGER NOT NULL REFERENCES companies(id),
    inn          TEXT    NOT NULL,
    kpp          TEXT    NOT NULL DEFAULT '',
    name         TEXT    NOT NULL DEFAULT '',
    full_name    TEXT    NOT NULL DEFAULT '',
    account      TEXT    NOT NULL,
    bank_name    TEXT    NOT NULL DEFAULT '',
    bank_city    TEXT    NOT NULL DEFAULT '',
    bic          TEXT    NOT NULL DEFAULT '',
    corr_account TEXT    NOT NULL DEFAULT '',
    -- как этот получатель обходится с НДС по последнему разобранному счёту:
    -- 'included' и ставка либо 'none'. Пусто — ещё не знаем.
    vat_status   TEXT    NOT NULL DEFAULT '',
    vat_rate     TEXT    NOT NULL DEFAULT '',
    schetov      INTEGER NOT NULL DEFAULT 0,
    platezhey    INTEGER NOT NULL DEFAULT 0,
    first_at     TEXT    NOT NULL,
    last_at      TEXT    NOT NULL,
    UNIQUE (company_id, inn, account)
);

-- пачка: одна загрузка сотрудника, из неё выйдет один файл для банка
CREATE TABLE IF NOT EXISTS batches (
    id           INTEGER PRIMARY KEY,
    company_id   INTEGER NOT NULL REFERENCES companies(id),
    user_id      INTEGER NOT NULL REFERENCES users(id),
    payer_id     INTEGER NOT NULL REFERENCES payers(id),
    account_id   INTEGER REFERENCES payer_accounts(id),
    status       TEXT    NOT NULL DEFAULT 'processing',
    payment_date TEXT,
    out_name     TEXT,
    created_at   TEXT    NOT NULL,
    exported_at  TEXT
);

-- один загруженный документ внутри пачки
CREATE TABLE IF NOT EXISTS docs (
    id            INTEGER PRIMARY KEY,
    batch_id      INTEGER NOT NULL REFERENCES batches(id),
    filename      TEXT    NOT NULL,
    stored_name   TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending',
    engine        TEXT    NOT NULL DEFAULT '',
    data_json     TEXT    NOT NULL DEFAULT '{}',
    problems_json TEXT    NOT NULL DEFAULT '[]',
    edited        INTEGER NOT NULL DEFAULT 0,
    included      INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL
);

-- счётчик номеров платёжек: ключ (плательщик, счёт, год), как в RESHENIE-numeratsiya.md
CREATE TABLE IF NOT EXISTS counters (
    company_id  INTEGER NOT NULL REFERENCES companies(id),
    key         TEXT    NOT NULL,
    last_number INTEGER NOT NULL,
    PRIMARY KEY (company_id, key)
);

-- номер, закреплённый за конкретным счётом: повторная выгрузка даёт тот же номер,
-- а бухгалтер видит, что этот счёт уже уходил в банк
CREATE TABLE IF NOT EXISTS assigned (
    company_id  INTEGER NOT NULL REFERENCES companies(id),
    identity    TEXT    NOT NULL,
    number      INTEGER NOT NULL,
    payload     TEXT    NOT NULL,
    batch_id    INTEGER REFERENCES batches(id),
    created_at  TEXT    NOT NULL,
    PRIMARY KEY (company_id, identity)
);

CREATE TABLE IF NOT EXISTS exports (
    id         INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    batch_id   INTEGER NOT NULL REFERENCES batches(id),
    user_id    INTEGER NOT NULL REFERENCES users(id),
    file_name  TEXT    NOT NULL,
    numbers    TEXT    NOT NULL,
    amount     TEXT    NOT NULL DEFAULT '0',
    created_at TEXT    NOT NULL
);

-- кто что сделал: речь о платежах, поэтому пишем всё
CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY,
    company_id INTEGER,
    user_id    INTEGER,
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    at         TEXT NOT NULL
);

-- попытки входа: защита от подбора пароля
CREATE TABLE IF NOT EXISTS popytki (
    id      INTEGER PRIMARY KEY,
    login   TEXT NOT NULL,
    ip      TEXT NOT NULL DEFAULT '',
    udachno INTEGER NOT NULL DEFAULT 0,
    at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scheta_payer  ON payer_accounts(payer_id);
CREATE INDEX IF NOT EXISTS idx_poluch_inn    ON poluchateli(company_id, inn);
CREATE INDEX IF NOT EXISTS idx_popytki_login ON popytki(login, at);
CREATE INDEX IF NOT EXISTS idx_popytki_ip    ON popytki(ip, at);
CREATE INDEX IF NOT EXISTS idx_docs_batch     ON docs(batch_id);
CREATE INDEX IF NOT EXISTS idx_batches_comp   ON batches(company_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_user  ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_comp     ON audit(company_id, at);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(path) if path else DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI выполняет обычные (не async) обработчики
    # и их зависимости в пуле потоков, и соединение, созданное зависимостью,
    # попадает в обработчик уже в другом потоке. Соединение здесь живёт один
    # запрос и используется строго по очереди, поэтому это безопасно.
    conn = sqlite3.connect(target, isolation_level=None, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


LEGACY_SCHETA = (
    "account",
    "bank_name",
    "bank_city",
    "bic",
    "corr_account",
    "numbering_start",
    "bank_profile",
)


def _stolbtsy(conn: sqlite3.Connection, tablitsa: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({tablitsa})")}


def dovesti_shemu(conn: sqlite3.Connection) -> None:
    """Довести старую базу до нынешней схемы.

    Единственное изменение, которое требует переноса данных: раньше у плательщика
    был один расчётный счёт, прямо в его строке. Теперь счетов может быть
    несколько, и они живут отдельной таблицей.
    """
    if "account_id" not in _stolbtsy(conn, "batches"):
        conn.execute("ALTER TABLE batches ADD COLUMN account_id INTEGER")

    # Справочник получателей запомнил, кто как работает с НДС
    if "vat_status" not in _stolbtsy(conn, "poluchateli"):
        conn.execute("ALTER TABLE poluchateli ADD COLUMN vat_status TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE poluchateli ADD COLUMN vat_rate TEXT NOT NULL DEFAULT ''")

    if "account" not in _stolbtsy(conn, "payers"):
        return  # схема уже новая

    for payer in conn.execute("SELECT * FROM payers").fetchall():
        if not payer["account"]:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO payer_accounts (payer_id, metka, account, bank_name,"
            " bank_city, bic, corr_account, numbering_start, bank_profile, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                payer["id"],
                payer["bank_name"],
                payer["account"],
                payer["bank_name"],
                payer["bank_city"],
                payer["bic"],
                payer["corr_account"],
                payer["numbering_start"],
                payer["bank_profile"],
                now(),
            ),
        )
    # Пачки, сделанные до переноса, привязываем к перенесённому счёту
    conn.execute(
        "UPDATE batches SET account_id = ("
        " SELECT id FROM payer_accounts WHERE payer_id = batches.payer_id LIMIT 1)"
        " WHERE account_id IS NULL"
    )
    for stolbets in LEGACY_SCHETA:
        try:
            conn.execute(f"ALTER TABLE payers DROP COLUMN {stolbets}")
        except sqlite3.OperationalError:
            pass  # старый SQLite не умеет удалять столбцы — пусть лежат неиспользуемыми


def init(path: str | Path | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        dovesti_shemu(conn)


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Немедленная блокировка на запись — нумерация не терпит гонок.

    IMMEDIATE берёт блокировку сразу, а не при первой записи: два сотрудника,
    нажавшие «Сформировать» одновременно, не получат один и тот же номер.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def log(conn: sqlite3.Connection, company_id, user_id, action: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit (company_id, user_id, action, detail, at) VALUES (?, ?, ?, ?, ?)",
        (company_id, user_id, action, detail, now()),
    )
