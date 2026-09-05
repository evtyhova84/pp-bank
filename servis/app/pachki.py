"""Пачка счетов: загрузка -> распознавание -> проверка человеком -> файл для банка.

Распознавание идёт в отдельном потоке: скан на локальном OCR занимает секунды,
а с эскалацией в модель — и того больше. Держать браузер сотрудника всё это
время на висящем запросе нельзя, поэтому страница пачки сама обновляется,
пока файлы разбираются.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse

from . import db, nomera, poluchateli, veb
from .razbor import proverit_summy, razobrat
from .yadro import (
    ExportError,
    Exporter,
    Invoice,
    build_purpose,
    load_profile,
    parse_date,
    payer_iz_stroki,
)

router = APIRouter()

ZAGRUZKI = db.DATA_DIR / "zagruzki"
VYGRUZKI = db.DATA_DIR / "vygruzki"

RASSHIRENIYA = {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
PREDEL_FAYLA = 25 * 1024 * 1024  # 25 МБ: фото счёта с телефона крупнее не бывает

# Сколько счетов принимаем за раз. Предел не в самом разборе, а в экране проверки:
# просмотреть глазами тысячу счетов за один присест человек всё равно не сможет.
PREDEL_PACHKI = int(os.environ.get("PP_PREDEL_PACHKI", "500"))
PREDEL_OBSHCHIY = int(os.environ.get("PP_PREDEL_PACHKI_MB", "600")) * 1024 * 1024

# Распознавание — это в основном ожидание движка, поэтому потоки помогают.
POTOKOV = int(os.environ.get("PP_POTOKOV", "0")) or min(4, (os.cpu_count() or 2))

# Поля, которые сотрудник может поправить руками на экране проверки
POLYA = (
    "invoice_number",
    "invoice_date",
    "recipient_name",
    "recipient_full",
    "recipient_inn",
    "recipient_kpp",
    "recipient_account",
    "recipient_bank_name",
    "recipient_bank_city",
    "recipient_bic",
    "recipient_corr_account",
    "total_amount",
    "vat_rate",
    "vat_amount",
    "payment_description",
)

PODPISI = {
    "invoice_number": "Номер счёта",
    "invoice_date": "Дата счёта",
    "recipient_name": "Получатель",
    "recipient_full": "Полное наименование",
    "recipient_inn": "ИНН",
    "recipient_kpp": "КПП",
    "recipient_account": "Расчётный счёт",
    "recipient_bank_name": "Банк",
    "recipient_bank_city": "Город банка",
    "recipient_bic": "БИК",
    "recipient_corr_account": "Корр. счёт",
    "total_amount": "Сумма",
    "vat_rate": "Ставка НДС",
    "vat_amount": "Сумма НДС",
    "payment_description": "За что платим (позиции счёта)",
}

# Ставки НДС, из которых бухгалтер выбирает, когда счёт про НДС молчит.
# Это не реквизиты организации, а закон, поэтому список живёт в коде.
STAVKI_NDS = ("22", "20", "10", "7", "5")
BEZ_NDS = "none"  # значение выбора «Без НДС»


# --- вспомогательное ---


def _pachka(conn: sqlite3.Connection, pachka_id: int, polzovatel) -> sqlite3.Row:
    row = conn.execute(
        "SELECT b.*, p.name AS payer_name, p.code AS payer_code, p.inn AS payer_inn,"
        " pa.account AS payer_account, pa.metka AS payer_metka, pa.bank_name AS payer_bank,"
        " pa.bank_profile AS payer_bank_profile,"
        " u.full_name AS avtor, u.login AS avtor_login FROM batches b"
        " JOIN payers p ON p.id = b.payer_id"
        " LEFT JOIN payer_accounts pa ON pa.id = b.account_id"
        " JOIN users u ON u.id = b.user_id"
        " WHERE b.id = ? AND b.company_id = ?",
        (pachka_id, polzovatel["company_id"]),
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "пачка не найдена")
    return row


def _dokumenty(conn: sqlite3.Connection, pachka_id: int) -> list[dict]:
    docs = []
    for row in conn.execute(
        "SELECT * FROM docs WHERE batch_id = ? ORDER BY id", (pachka_id,)
    ).fetchall():
        doc = dict(row)
        doc["data"] = json.loads(row["data_json"] or "{}")
        doc["problems"] = json.loads(row["problems_json"] or "[]")
        doc["nds"] = _vybor_nds(doc["data"])
        docs.append(doc)
    return docs


def _vybor_nds(data: dict) -> str:
    """Что показать выбранным в списке ставок: «Без НДС», ставка или ничего."""
    if data.get("vat_status") == "none":
        return BEZ_NDS
    rate = str(data.get("vat_rate") or "").strip()
    if not rate:
        return ""
    return poluchateli.stavka(rate)


def _naznachenie(doc: dict, max_length: int) -> str:
    """Назначение платежа, каким оно уйдёт в файл, — показать до выгрузки."""
    try:
        invoice = Invoice.from_dict(
            {k: v for k, v in doc["data"].items() if not k.startswith("_")}
        )
    except (ValueError, TypeError, KeyError):
        return ""
    return build_purpose(invoice, max_length)


def _primenit_nds(data: dict, vybor: str) -> None:
    """Выбор бухгалтера на экране проверки -> поля НДС в счёте.

    «Без НДС» — статус none, ставки и суммы нет. Ставка — статус included; если
    сумма НДС не вписана руками, она считается от итога. Пусто — как было.
    """
    vybor = (vybor or "").strip()
    if vybor == BEZ_NDS:
        data["vat_status"] = "none"
        data["vat_rate"] = None
        data["vat_amount"] = None
        return
    if vybor:
        data["vat_status"] = "included"
        data["vat_rate"] = poluchateli.stavka(vybor)
        if not data.get("vat_amount"):
            data["vat_amount"] = poluchateli.nds_ot_itoga(
                str(data.get("total_amount") or ""), data["vat_rate"]
            )
        return
    data["vat_rate"] = None
    if data.get("vat_amount"):
        data["vat_status"] = "included"
    elif data.get("vat_status") != "none":
        data["vat_status"] = "unknown"


def _bezopasnoe_imya(imya: str) -> str:
    imya = Path(imya or "").name
    return re.sub(r"[^\w .()-]", "_", imya, flags=re.UNICODE)[:120] or "bez-imeni"


def _summa(docs: list[dict]) -> str:
    from decimal import Decimal

    itog = Decimal("0")
    for doc in docs:
        if doc["status"] == "ok" and doc["included"]:
            try:
                itog += Decimal(str(doc["data"].get("total_amount") or 0))
            except Exception:
                pass
    return f"{itog:.2f}"


# --- фоновое распознавание ---


def _razobrat_odin(pachka_id: int, doc: sqlite3.Row, payer_inn: str, company_id: int) -> None:
    """Один документ. Своё соединение: задача выполняется в чужом потоке."""
    conn = db.connect()
    try:
        put = ZAGRUZKI / str(pachka_id) / doc["stored_name"]
        try:
            itog = razobrat(put, payer_inn=payer_inn)
            data, problems, notes, dvizhok = itog.data, itog.problems, itog.notes, itog.dvizhok
            if data:
                # Чего не прочиталось — доберём из справочника получателей,
                # и после этого проверим заново: подстановка меняет данные.
                notes += poluchateli.dopolnit(conn, company_id, data)
                # Замечания парсера сохраняем — раньше они здесь терялись,
                # и счёт, выставленный на другую организацию, уходил в файл
                # как чистый. Отпускаем только упрёк к имени, если справочник
                # его уже заменил.
                problems = [
                    p
                    for p in itog.iz_parsera
                    if not (
                        p.startswith("наименование получателя распознано ненадёжно")
                        and not data.get("_name_unreliable")
                    )
                    # ...и вопрос про НДС, если на него ответил справочник
                    and not (
                        p.startswith("НДС не найден") and data.get("_vat_iz_spravochnika")
                    )
                ] + proverit_summy(data)
            sostoyanie = "failed" if not data else ("ok" if not problems else "problem")
        except Exception as exc:  # движок упал на этом файле — остальные разберём
            data, problems, notes = {}, [f"не удалось прочитать файл: {exc}"], []
            dvizhok, sostoyanie = "", "failed"

        with db.transaction(conn):
            if sostoyanie == "ok":
                poluchateli.zapomnit(conn, company_id, data)
            conn.execute(
                "UPDATE docs SET status = ?, engine = ?, data_json = ?, problems_json = ?"
                " WHERE id = ?",
                (
                    sostoyanie,
                    dvizhok,
                    json.dumps(data, ensure_ascii=False),
                    json.dumps(problems + [f"заметка: {n}" for n in notes], ensure_ascii=False),
                    doc["id"],
                ),
            )
    finally:
        conn.close()


def _kluch_scheta(data: dict) -> str:
    """Чем один счёт отличается от другого: получатель, его счёт, номер, дата, сумма."""
    return "|".join(
        str(data.get(pole) or "").strip()
        for pole in (
            "recipient_inn",
            "recipient_account",
            "invoice_number",
            "invoice_date",
            "total_amount",
        )
    )


def _otmetit_dubli(conn: sqlite3.Connection, pachka_id: int, company_id: int, payer) -> None:
    """Один счёт — одна платёжка.

    В пачке на сотню документов один и тот же счёт легко оказывается дважды:
    его отсканировали повторно, прислали и почтой, и в мессенджере. Без этой
    проверки он попадёт в файл двумя платёжками — с одинаковым номером, но
    оплатой в двойном размере.

    Такие документы не удаляются, а исключаются из выгрузки с пометкой: решает
    человек, вдруг это два разных счёта с совпавшими реквизитами.
    """
    from .nomera import SqlRegistry
    from .yadro import Invoice

    registry = SqlRegistry(conn, company_id=company_id)
    vstrecheno: dict[str, int] = {}

    for doc in conn.execute(
        "SELECT * FROM docs WHERE batch_id = ? AND status = 'ok' ORDER BY id", (pachka_id,)
    ).fetchall():
        data = json.loads(doc["data_json"] or "{}")
        problems = json.loads(doc["problems_json"] or "[]")
        kluch = _kluch_scheta(data)

        pometka = None
        if kluch in vstrecheno:
            pometka = (
                f"дубль: тот же счёт уже есть в этой пачке (документ № {vstrecheno[kluch]}) "
                "— из выгрузки исключён, чтобы не заплатить дважды"
            )
        else:
            vstrecheno[kluch] = doc["id"]
            try:
                ranshe = registry.already_paid(payer, Invoice.from_dict(
                    {k: v for k, v in data.items() if not k.startswith("_")}
                ))
            except (ValueError, TypeError):
                ranshe = None
            if ranshe:
                pometka = (
                    f"этот счёт уже выгружался — платёжка № {ranshe['number']} "
                    f"от {ranshe['date']}. Из выгрузки исключён"
                )

        if pometka:
            conn.execute(
                "UPDATE docs SET included = 0, problems_json = ? WHERE id = ?",
                (json.dumps(problems + [pometka], ensure_ascii=False), doc["id"]),
            )


def _razobrat_pachku(
    pachka_id: int, payer_inn: str, company_id: int, payer_id: int, account_id: int
) -> None:
    """Разобрать всю пачку.

    Документы читаются параллельно: на сотне сканов последовательный разбор
    занял бы десятки минут, а распознавание — это в основном ожидание внешнего
    движка, которое хорошо раскладывается по потокам. Записи в базу при этом
    короткие, каждая в своей транзакции.
    """
    conn = db.connect()
    try:
        docs = conn.execute(
            "SELECT id, stored_name FROM docs WHERE batch_id = ? AND status = 'pending'"
            " ORDER BY id",
            (pachka_id,),
        ).fetchall()

        if docs:
            with ThreadPoolExecutor(max_workers=min(POTOKOV, len(docs))) as pul:
                zadachi = [
                    pul.submit(_razobrat_odin, pachka_id, doc, payer_inn, company_id)
                    for doc in docs
                ]
                for zadacha in zadachi:
                    zadacha.result()  # исключения уже пойманы внутри, это на всякий случай

        # Дубли ищем, когда разобраны все: до этого сравнивать не с чем
        platelshchik = conn.execute("SELECT * FROM payers WHERE id = ?", (payer_id,)).fetchone()
        schet = conn.execute(
            "SELECT * FROM payer_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        with db.transaction(conn):
            if platelshchik is not None and schet is not None:
                _otmetit_dubli(
                    conn, pachka_id, company_id, payer_iz_stroki(platelshchik, schet)
                )
            conn.execute(
                "UPDATE batches SET status = 'review' WHERE id = ? AND status = 'processing'",
                (pachka_id,),
            )
    finally:
        conn.close()


# --- плательщик по счетам ---


def pokupatel_pachki(conn: sqlite3.Connection, company_id: int, docs: list[dict], payer_inn: str):
    """На кого выставлено большинство счетов пачки, если не на её плательщика.

    Пачку легко загрузить не под той организацией: сорок четыре счёта на
    ООО «ТЕТРАКОМ» под плательщиком-ИП. Каждый из них парсер честно помечает
    «выставлен не нам», но лечить их по одному бессмысленно — надо переназначить
    пачку. Здесь считаем, чей ИНН стоит в графе «Покупатель» чаще всего, и если
    это не наш плательщик — сервис предложит одну кнопку вместо перезагрузки.
    """
    pokupateli = Counter()
    svoih = 0
    for doc in docs:
        inn = str(doc["data"].get("_buyer_inn") or "").strip()
        if not inn:
            continue
        if inn == payer_inn:
            svoih += 1
        else:
            pokupateli[inn] += 1
    if not pokupateli:
        return None
    inn, skolko = pokupateli.most_common(1)[0]
    if skolko <= svoih:
        return None  # чужих меньше, чем своих: это отдельные счета, не пачка

    def chashche(pole: str) -> str:
        znacheniya = Counter(
            str(d["data"].get(pole) or "").strip()
            for d in docs
            if str(d["data"].get("_buyer_inn") or "").strip() == inn
            and str(d["data"].get(pole) or "").strip()
        )
        return znacheniya.most_common(1)[0][0] if znacheniya else ""

    imya = chashche("_buyer_name")
    payer = conn.execute(
        "SELECT * FROM payers WHERE company_id = ? AND inn = ? AND active = 1 ORDER BY id",
        (company_id, inn),
    ).fetchone()
    scheta = []
    if payer is not None:
        scheta = conn.execute(
            "SELECT * FROM payer_accounts WHERE payer_id = ? AND active = 1 ORDER BY metka",
            (payer["id"],),
        ).fetchall()
    return {
        "inn": inn,
        "kpp": chashche("_buyer_kpp"),
        "name": imya,
        "skolko": skolko,
        "vsego": sum(1 for d in docs if d["data"]),
        "payer": payer,
        "scheta": scheta,
    }


@router.post("/pachki/{pachka_id}/perenaznachit")
def perenaznachit(
    request: Request,
    pachka_id: int,
    token: str = Form(...),
    schet_id: int = Form(...),
    polzovatel=Depends(veb.tekushchiy),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    """Переназначить пачку на другой счёт плательщика и разобрать её заново.

    Разбор повторяется целиком: замечания «выставлен не нам», проверка дублей
    и подстановки из справочника зависят от того, кто платит. Дешевле
    перечитать сорок сканов, чем вычищать эти следы по одному.
    """
    veb.proverit_formu(request, token)
    row = _pachka(conn, pachka_id, polzovatel)
    if row["status"] == "exported":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "пачка уже выгружена в банк")

    schet = conn.execute(
        "SELECT pa.*, p.inn AS payer_inn, p.id AS payer_id, p.name AS payer_name"
        " FROM payer_accounts pa JOIN payers p ON p.id = pa.payer_id"
        " WHERE pa.id = ? AND p.company_id = ? AND p.active = 1 AND pa.active = 1",
        (schet_id, polzovatel["company_id"]),
    ).fetchone()
    if schet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "расчётный счёт плательщика не найден")

    with db.transaction(conn):
        conn.execute(
            "UPDATE batches SET payer_id = ?, account_id = ?, status = 'processing'"
            " WHERE id = ?",
            (schet["payer_id"], schet_id, pachka_id),
        )
        conn.execute(
            "UPDATE docs SET status = 'pending', engine = '', data_json = '{}',"
            " problems_json = '[]', edited = 0, included = 1"
            " WHERE batch_id = ? AND status != 'skipped'",
            (pachka_id,),
        )
        db.log(
            conn,
            polzovatel["company_id"],
            polzovatel["id"],
            "пачка переназначена",
            f"пачка {pachka_id}: {row['payer_name']} -> {schet['payer_name']} ({schet['account']})",
        )

    threading.Thread(
        target=_razobrat_pachku,
        args=(
            pachka_id,
            schet["payer_inn"],
            polzovatel["company_id"],
            schet["payer_id"],
            schet_id,
        ),
        daemon=True,
    ).start()
    return veb.tuda(f"/pachki/{pachka_id}")


# --- список пачек ---


@router.get("/pachki", response_class=HTMLResponse)
def spisok(
    request: Request,
    polzovatel=Depends(veb.tekushchiy),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    pachki = conn.execute(
        "SELECT b.*, p.name AS payer_name, u.full_name AS avtor, u.login AS avtor_login,"
        " (SELECT COUNT(*) FROM docs d WHERE d.batch_id = b.id) AS vsego,"
        " (SELECT COUNT(*) FROM docs d WHERE d.batch_id = b.id AND d.status = 'ok') AS chisto"
        " FROM batches b JOIN payers p ON p.id = b.payer_id JOIN users u ON u.id = b.user_id"
        " WHERE b.company_id = ? ORDER BY b.id DESC LIMIT 100",
        (polzovatel["company_id"],),
    ).fetchall()
    return veb.stranitsa(request, "pachki.html", polzovatel=polzovatel, pachki=pachki)


@router.get("/pachki/nova", response_class=HTMLResponse)
def nova_forma(
    request: Request,
    polzovatel=Depends(veb.tekushchiy),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    scheta = conn.execute(
        "SELECT pa.id, pa.account, pa.metka, pa.bank_name, p.name AS payer_name"
        " FROM payer_accounts pa JOIN payers p ON p.id = pa.payer_id"
        " WHERE p.company_id = ? AND p.active = 1 AND pa.active = 1"
        " ORDER BY p.name, pa.metka",
        (polzovatel["company_id"],),
    ).fetchall()
    return veb.stranitsa(
        request,
        "nova.html",
        polzovatel=polzovatel,
        scheta=scheta,
        segodnya=date.today().strftime("%Y-%m-%d"),
        oshibka=None,
    )


@router.post("/pachki/nova")
async def nova(
    request: Request,
    token: str = Form(...),
    schet_id: int = Form(...),
    data_platezha: str = Form(...),
    fayly: list[UploadFile] = File(...),
    polzovatel=Depends(veb.tekushchiy),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    veb.proverit_formu(request, token)

    schet = conn.execute(
        "SELECT pa.*, p.inn AS payer_inn, p.id AS payer_id FROM payer_accounts pa"
        " JOIN payers p ON p.id = pa.payer_id"
        " WHERE pa.id = ? AND p.company_id = ? AND p.active = 1 AND pa.active = 1",
        (schet_id, polzovatel["company_id"]),
    ).fetchone()
    if schet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "расчётный счёт плательщика не найден")

    try:
        payment_date = parse_date(data_platezha)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "не разобрана дата платежа") from None

    prigodnye = [f for f in fayly if f.filename and Path(f.filename).suffix.lower() in RASSHIRENIYA]
    if not prigodnye:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "не выбрано ни одного файла PDF или снимка счёта",
        )
    if len(prigodnye) > PREDEL_PACHKI:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"в одной пачке не больше {PREDEL_PACHKI} счетов"
        )

    with db.transaction(conn):
        cursor = conn.execute(
            "INSERT INTO batches (company_id, user_id, payer_id, account_id, status,"
            " payment_date, created_at) VALUES (?, ?, ?, ?, 'processing', ?, ?)",
            (
                polzovatel["company_id"],
                polzovatel["id"],
                schet["payer_id"],
                schet_id,
                payment_date.isoformat(),
                db.now(),
            ),
        )
        pachka_id = int(cursor.lastrowid)

    papka = ZAGRUZKI / str(pachka_id)
    papka.mkdir(parents=True, exist_ok=True)
    vsego_bayt = 0
    ne_prinyato = 0
    for fayl in prigodnye:
        soderzhimoe = await fayl.read()
        # Файл, который не приняли, всё равно заводится строкой в пачке.
        # Раньше он пропускался молча: человек грузил полсотни счетов, получал
        # сорок восемь и не мог понять, каких двух не хватает.
        prichina = ""
        if len(soderzhimoe) > PREDEL_FAYLA:
            prichina = (
                f"файл не принят: {len(soderzhimoe) / 1024 / 1024:.1f} МБ при пределе"
                f" {PREDEL_FAYLA / 1024 / 1024:.0f} МБ на файл."
                " Пересохраните счёт с меньшим разрешением или снимите заново"
            )
        elif vsego_bayt + len(soderzhimoe) > PREDEL_OBSHCHIY:
            prichina = (
                f"файл не принят: вся пачка не должна быть больше"
                f" {PREDEL_OBSHCHIY // 1024 // 1024} МБ. Разделите её на несколько"
            )
        if prichina:
            ne_prinyato += 1
            with db.transaction(conn):
                conn.execute(
                    "INSERT INTO docs (batch_id, filename, stored_name, status,"
                    " problems_json, included, created_at)"
                    " VALUES (?, ?, '', 'skipped', ?, 0, ?)",
                    (
                        pachka_id,
                        _bezopasnoe_imya(fayl.filename),
                        json.dumps([prichina], ensure_ascii=False),
                        db.now(),
                    ),
                )
            continue
        vsego_bayt += len(soderzhimoe)
        imya = f"{secrets.token_hex(8)}{Path(fayl.filename).suffix.lower()}"
        (papka / imya).write_bytes(soderzhimoe)
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO docs (batch_id, filename, stored_name, created_at)"
                " VALUES (?, ?, ?, ?)",
                (pachka_id, _bezopasnoe_imya(fayl.filename), imya, db.now()),
            )

    with db.transaction(conn):
        db.log(
            conn,
            polzovatel["company_id"],
            polzovatel["id"],
            "загружена пачка",
            f"пачка {pachka_id}, файлов {len(prigodnye)}"
            + (f", не принято {ne_prinyato}" if ne_prinyato else ""),
        )

    threading.Thread(
        target=_razobrat_pachku,
        args=(
            pachka_id,
            schet["payer_inn"],
            polzovatel["company_id"],
            schet["payer_id"],
            schet_id,
        ),
        daemon=True,
    ).start()
    return veb.tuda(f"/pachki/{pachka_id}")


# --- экран проверки ---


@router.get("/pachki/{pachka_id}", response_class=HTMLResponse)
def pachka(
    request: Request,
    pachka_id: int,
    polzovatel=Depends(veb.tekushchiy),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    row = _pachka(conn, pachka_id, polzovatel)
    docs = _dokumenty(conn, pachka_id)
    zhdyom = row["status"] == "processing" or any(d["status"] == "pending" for d in docs)

    predel = load_profile(row["payer_bank_profile"] or "default").purpose_max_length
    for doc in docs:
        doc["naznachenie"] = _naznachenie(doc, predel) if doc["data"] else ""

    pokupatel = None
    if not zhdyom and row["status"] != "exported":
        pokupatel = pokupatel_pachki(conn, polzovatel["company_id"], docs, row["payer_inn"])

    return veb.stranitsa(
        request,
        "pachka.html",
        polzovatel=polzovatel,
        pachka=row,
        docs=docs,
        polya=POLYA,
        podpisi=PODPISI,
        stavki=STAVKI_NDS,
        bez_nds=BEZ_NDS,
        predel_naznacheniya=predel,
        pokupatel=pokupatel,
        zhdyom=zhdyom,
        gotovo=sum(1 for d in docs if d["status"] == "ok" and d["included"]),
        razobrano=sum(1 for d in docs if d["status"] not in ("pending", "skipped")),
        trebuyut=sum(1 for d in docs if d["status"] in ("problem", "failed")),
        ne_prinyato=sum(1 for d in docs if d["status"] == "skipped"),
        isklyucheno=sum(1 for d in docs if d["status"] == "ok" and not d["included"]),
        summa=_summa(docs),
    )


@router.post("/pachki/{pachka_id}/dokument/{doc_id}")
async def pravka(
    request: Request,
    pachka_id: int,
    doc_id: int,
    token: str = Form(...),
    deystvie: str = Form("sohranit"),
    polzovatel=Depends(veb.tekushchiy),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    veb.proverit_formu(request, token)
    _pachka(conn, pachka_id, polzovatel)
    doc = conn.execute(
        "SELECT * FROM docs WHERE id = ? AND batch_id = ?", (doc_id, pachka_id)
    ).fetchone()
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "документ не найден")

    if deystvie in ("vklyuchit", "isklyuchit"):
        with db.transaction(conn):
            conn.execute(
                "UPDATE docs SET included = ? WHERE id = ?",
                (1 if deystvie == "vklyuchit" else 0, doc_id),
            )
        return veb.tuda(f"/pachki/{pachka_id}")

    # Сохранение правки: перечитываем поля из формы и проверяем заново
    forma = await request.form()
    data = json.loads(doc["data_json"] or "{}")
    for pole in POLYA:
        if pole in forma and pole != "vat_rate":
            data[pole] = (forma.get(pole) or "").strip()
    if not data.get("vat_amount"):
        data["vat_amount"] = None
    # Ставка — выбор из списка: «Без НДС», ставка или пусто. Человек посмотрел
    # на счёт и решил; это единственное место, где НДС ставится не из документа.
    _primenit_nds(data, forma.get("vat_rate") if "vat_rate" in forma else _vybor_nds(data))
    data.pop("_vat_iz_spravochnika", None)

    problems = proverit_summy(data)
    if data.get("vat_status") == "unknown":
        problems.append(
            "НДС не определён: выберите ставку или «Без НДС» — иначе в назначении "
            "платежа будет написано «Без НДС» наугад"
        )
    with db.transaction(conn):
        if not problems:
            # Счёт, который парсер не осилил, а человек поправил, — тоже знание
            # о получателе; и выбранная ставка НДС запоминается за ним.
            if doc["status"] != "ok":
                poluchateli.zapomnit(conn, polzovatel["company_id"], data)
            poluchateli.zapomnit_nds(conn, polzovatel["company_id"], data)
        conn.execute(
            "UPDATE docs SET data_json = ?, problems_json = ?, status = ?, edited = 1"
            " WHERE id = ?",
            (
                json.dumps(data, ensure_ascii=False),
                json.dumps(problems, ensure_ascii=False),
                "ok" if not problems else "problem",
                doc_id,
            ),
        )
        db.log(
            conn,
            polzovatel["company_id"],
            polzovatel["id"],
            "правка счёта",
            f"пачка {pachka_id}, документ {doc['filename']}",
        )
    return veb.tuda(f"/pachki/{pachka_id}")


# --- выгрузка ---


@router.post("/pachki/{pachka_id}/vygruzka")
def vygruzka(
    request: Request,
    pachka_id: int,
    token: str = Form(...),
    polzovatel=Depends(veb.tekushchiy),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    veb.proverit_formu(request, token)
    row = _pachka(conn, pachka_id, polzovatel)
    if row["status"] == "exported":
        return veb.tuda(f"/pachki/{pachka_id}")

    platelshchik = conn.execute("SELECT * FROM payers WHERE id = ?", (row["payer_id"],)).fetchone()
    schet = conn.execute(
        "SELECT * FROM payer_accounts WHERE id = ?", (row["account_id"],)
    ).fetchone()
    if schet is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "у пачки не указан расчётный счёт плательщика"
        )
    payer = payer_iz_stroki(platelshchik, schet)
    profile = load_profile(payer.bank_profile)
    payment_date = parse_date(row["payment_date"])

    docs = [d for d in _dokumenty(conn, pachka_id) if d["status"] == "ok" and d["included"]]
    if not docs:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "нечего выгружать: все счета либо с замечаниями, либо исключены",
        )

    invoices = [
        Invoice.from_dict({k: v for k, v in d["data"].items() if not k.startswith("_")})
        for d in docs
    ]

    VYGRUZKI.mkdir(parents=True, exist_ok=True)
    imya = f"1c_to_kl_{payment_date:%Y-%m-%d}_{payer.id}_{pachka_id}.txt"
    put = VYGRUZKI / imya

    registry = nomera.SqlRegistry(
        conn, company_id=polzovatel["company_id"], batch_id=pachka_id, user_id=polzovatel["id"]
    )
    registry.summa = _summa(docs)
    exporter = Exporter(payer, profile, registry)

    try:
        with db.transaction(conn):
            put, orders, zamechaniya = exporter.write(invoices, put, payment_date=payment_date)
            conn.execute(
                "UPDATE batches SET status = 'exported', out_name = ?, exported_at = ?"
                " WHERE id = ?",
                (imya, db.now(), pachka_id),
            )
            for doc in docs:  # отмечаем платёж у получателей в справочнике
                poluchateli.otmetit_platezh(conn, polzovatel["company_id"], doc["data"])
            db.log(
                conn,
                polzovatel["company_id"],
                polzovatel["id"],
                "выгрузка в банк",
                f"пачка {pachka_id}, платёжек {len(orders)} на {registry.summa}, файл {imya}",
            )
    except ExportError as exc:
        # Реквизиты не прошли проверку — файл не пишется вообще
        return veb.stranitsa(
            request,
            "oshibka.html",
            polzovatel=polzovatel,
            zagolovok="Выгрузка остановлена",
            pachka_id=pachka_id,
            problemy=[str(p) for p in exc.problems],
        )

    return veb.tuda(f"/pachki/{pachka_id}")


@router.get("/pachki/{pachka_id}/fayl")
def skachat(
    pachka_id: int,
    polzovatel=Depends(veb.tekushchiy),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    row = _pachka(conn, pachka_id, polzovatel)
    if not row["out_name"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "файл ещё не сформирован")
    put = VYGRUZKI / row["out_name"]
    if not put.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "файл выгрузки не найден на диске")
    return FileResponse(put, filename="1c_to_kl.txt", media_type="text/plain")


@router.get("/pachki/{pachka_id}/dokument/{doc_id}/ishodnik")
def ishodnik(
    pachka_id: int,
    doc_id: int,
    polzovatel=Depends(veb.tekushchiy),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    """Показать исходный счёт — сверить распознанное с бумагой."""
    _pachka(conn, pachka_id, polzovatel)
    doc = conn.execute(
        "SELECT * FROM docs WHERE id = ? AND batch_id = ?", (doc_id, pachka_id)
    ).fetchone()
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "документ не найден")
    put = ZAGRUZKI / str(pachka_id) / doc["stored_name"]
    if not doc["stored_name"] or not put.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "исходный файл не найден")
    return FileResponse(put, filename=doc["filename"])
