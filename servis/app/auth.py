"""Пользователи, пароли, сессии.

Пароль хранится как scrypt-хеш из стандартной библиотеки: отдельная библиотека
для этого не нужна, а меньше зависимостей — меньше поводов не обновиться.

Сессия живёт в базе, а не в подписанной куке. Так администратор может выкинуть
уволенного сотрудника немедленно; с подписанной кукой пришлось бы ждать
истечения срока.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from . import db

SESSION_COOKIE = "pp_session"
SESSION_DAYS = 12
SCRYPT = {"n": 2**14, "r": 8, "p": 1}
MIN_PASSWORD = 8

# Оператор грузит счета и забирает файл. Бухгалтер — то же плюс справочники:
# плательщики, их счета, получатели. Люди и пароли остаются за администратором,
# потому что это уже не работа с платежами, а управление доступом.
ROLES = {"operator": "Оператор", "buhgalter": "Бухгалтер", "admin": "Администратор"}

# Кому можно править справочники — плательщиков, их расчётные счета, получателей
ROLI_SPRAVOCHNIKOV = ("buhgalter", "admin")

# Защита от подбора пароля. Считаем в скользящем окне: столько-то неудач подряд —
# и вход по этому логину закрыт, пока окно не отойдёт. Отдельный, более широкий
# счётчик по адресу — на случай перебора логинов с одной машины.
OKNO_MINUT = 15
POPYTOK_NA_LOGIN = 5
POPYTOK_NA_ADRES = 20
HRANIT_POPYTKI_SUTOK = 7


class AuthError(RuntimeError):
    pass


class SlishkomMnogo(AuthError):
    """Вход временно закрыт: слишком много неудачных попыток."""


# --- пароли ---


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if algorithm != "scrypt":
        return False
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=bytes.fromhex(salt_hex), dklen=32, **SCRYPT
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


def check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"пароль короче {MIN_PASSWORD} знаков")


# --- пользователи ---


def create_user(
    conn: sqlite3.Connection,
    company_id: int,
    login: str,
    password: str,
    full_name: str = "",
    role: str = "operator",
) -> int:
    login = login.strip().lower()
    if not login:
        raise AuthError("пустой логин")
    if role not in ROLES:
        raise AuthError(f"неизвестная роль: {role}")
    check_password_rules(password)
    try:
        cursor = conn.execute(
            "INSERT INTO users (company_id, login, password_hash, full_name, role, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (company_id, login, hash_password(password), full_name.strip(), role, db.now()),
        )
    except sqlite3.IntegrityError as exc:
        raise AuthError(f"логин «{login}» уже занят") from exc
    return int(cursor.lastrowid)


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    check_password_rules(password)
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id)
    )
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))  # старые входы обнуляем


# --- защита от подбора пароля ---


def _okno() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=OKNO_MINUT)).isoformat(
        timespec="seconds"
    )


def _neudach(conn: sqlite3.Connection, stolbets: str, znachenie: str) -> tuple[int, str | None]:
    """Сколько неудач в окне и когда была последняя."""
    row = conn.execute(
        f"SELECT COUNT(*) AS n, MAX(at) AS posledn FROM popytki"
        f" WHERE {stolbets} = ? AND udachno = 0 AND at > ?",
        (znachenie, _okno()),
    ).fetchone()
    return int(row["n"]), row["posledn"]


def _skolko_zhdat(posledn: str | None) -> int:
    """Сколько минут осталось до конца блокировки, минимум одна."""
    if not posledn:
        return OKNO_MINUT
    try:
        bylo = datetime.fromisoformat(posledn)
    except ValueError:
        return OKNO_MINUT
    proshlo = (datetime.now(timezone.utc) - bylo).total_seconds() / 60
    return max(1, int(OKNO_MINUT - proshlo) + 1)


def proverit_popytki(conn: sqlite3.Connection, login_name: str, ip: str = "") -> None:
    """Не пора ли перестать пускать. Вызывается до проверки пароля."""
    login_name = login_name.strip().lower()
    neudach, posledn = _neudach(conn, "login", login_name)
    if neudach >= POPYTOK_NA_LOGIN:
        raise SlishkomMnogo(
            f"слишком много неудачных попыток входа — попробуйте через "
            f"{_skolko_zhdat(posledn)} мин."
        )
    if ip:
        neudach, posledn = _neudach(conn, "ip", ip)
        if neudach >= POPYTOK_NA_ADRES:
            raise SlishkomMnogo(
                f"слишком много неудачных попыток входа с этого адреса — "
                f"попробуйте через {_skolko_zhdat(posledn)} мин."
            )


def zapisat_popytku(
    conn: sqlite3.Connection, login_name: str, ip: str, udachno: bool
) -> None:
    conn.execute(
        "INSERT INTO popytki (login, ip, udachno, at) VALUES (?, ?, ?, ?)",
        (login_name.strip().lower(), ip, 1 if udachno else 0, db.now()),
    )


def sbrosit_popytki(conn: sqlite3.Connection, login_name: str, ip: str = "") -> None:
    """После удачного входа счётчик обнуляется — иначе пять опечаток за день
    закрывают вход человеку, который в итоге вспомнил пароль."""
    conn.execute(
        "DELETE FROM popytki WHERE login = ? AND udachno = 0",
        (login_name.strip().lower(),),
    )
    if ip:
        conn.execute("DELETE FROM popytki WHERE ip = ? AND udachno = 0", (ip,))


# --- вход и сессии ---


def login(conn: sqlite3.Connection, login_name: str, password: str, ip: str = "") -> str:
    proverit_popytki(conn, login_name, ip)
    row = conn.execute(
        "SELECT u.*, c.active AS company_active FROM users u"
        " JOIN companies c ON c.id = u.company_id WHERE u.login = ?",
        (login_name.strip().lower(),),
    ).fetchone()

    # Пароль проверяем даже для несуществующего логина: иначе по времени ответа
    # видно, какие логины в системе есть.
    stored = row["password_hash"] if row else hash_password(secrets.token_hex(8))
    if not verify_password(password, stored) or row is None:
        zapisat_popytku(conn, login_name, ip, udachno=False)
        raise AuthError("неверный логин или пароль")
    if not row["active"] or not row["company_active"]:
        zapisat_popytku(conn, login_name, ip, udachno=False)
        raise AuthError("доступ отключён, обратитесь к администратору")

    zapisat_popytku(conn, login_name, ip, udachno=True)
    sbrosit_popytki(conn, login_name, ip)
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, row["id"], db.now(), expires.isoformat(timespec="seconds")),
    )
    db.log(conn, row["company_id"], row["id"], "вход")
    return token


def logout(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def user_by_token(conn: sqlite3.Connection, token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT u.*, c.name AS company_name FROM sessions s"
        " JOIN users u ON u.id = s.user_id"
        " JOIN companies c ON c.id = u.company_id"
        " WHERE s.token = ? AND s.expires_at > ? AND u.active = 1 AND c.active = 1",
        (token, db.now()),
    ).fetchone()
    return row


def drop_expired(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (db.now(),))
    porog = (datetime.now(timezone.utc) - timedelta(days=HRANIT_POPYTKI_SUTOK)).isoformat(
        timespec="seconds"
    )
    conn.execute("DELETE FROM popytki WHERE at < ?", (porog,))
