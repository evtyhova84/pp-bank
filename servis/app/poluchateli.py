"""Справочник получателей: кому эта компания платит и по каким реквизитам.

Набирается сам. Бухгалтер ничего не заводит руками: каждый разобранный счёт
пополняет справочник, а счётчики показывают, кто из контрагентов постоянный,
а кто появился впервые.

Зачем он нужен, кроме списка:

1. **Дополнить недочитанное.** Если со скана не прочитался КПП или город банка,
   а этому получателю мы уже платили — поле берётся из справочника. Только
   пустое: распознанное значение никогда не затирается, и человек видит отметку.
2. **Заметить подмену реквизитов.** Если у знакомого контрагента вдруг другой
   расчётный счёт, это видно в справочнике: у ИНН стало две записи.
3. **Вспомнить, как получатель работает с НДС.** Счёт ИП на УСН часто не
   говорит про НДС ни слова, и каждый такой счёт шёл бы человеку с вопросом.
   Если по прошлым счетам известно, что получатель без НДС (или со ставкой),
   это подставляется с заметкой — а решает всё равно бухгалтер на экране.

Ключ — (компания, ИНН, счёт), а не просто ИНН: у одного контрагента законно
бывает несколько счетов, и схлопывать их в одну строку нельзя.
"""
from __future__ import annotations

import sqlite3
from decimal import ROUND_HALF_UP, Decimal

from . import db

# Поле в счёте -> столбец справочника
POLYA = {
    "recipient_inn": "inn",
    "recipient_kpp": "kpp",
    "recipient_name": "name",
    "recipient_full": "full_name",
    "recipient_account": "account",
    "recipient_bank_name": "bank_name",
    "recipient_bank_city": "bank_city",
    "recipient_bic": "bic",
    "recipient_corr_account": "corr_account",
}

# Что имеет смысл подставлять из справочника, если распознать не удалось.
# Расчётного счёта и ИНН здесь нет намеренно: это то, по чему уходят деньги,
# и брать их из истории вместо документа нельзя.
DOPOLNYAEM = ("kpp", "bank_city", "bank_name", "corr_account", "full_name")


def _znachenie(data: dict, pole: str) -> str:
    return str(data.get(pole) or "").strip()


def _nds_iz_scheta(data: dict) -> tuple[str, str]:
    """(vat_status, vat_rate) — только когда счёт про НДС высказался определённо."""
    status = _znachenie(data, "vat_status")
    if status == "none":
        return "none", ""
    if status == "included":
        rate = _znachenie(data, "vat_rate")
        return ("included", stavka(rate)) if rate else ("", "")
    return "", ""


def stavka(rate: str) -> str:
    """«20.00» и «20» — одна ставка."""
    try:
        return format(Decimal(rate.replace(",", ".")).normalize(), "f")  # «20», не «2E+1»
    except Exception:
        return rate


def zapomnit(conn: sqlite3.Connection, company_id: int, data: dict) -> None:
    """Пополнить справочник разобранным счётом."""
    inn = _znachenie(data, "recipient_inn")
    account = _znachenie(data, "recipient_account")
    if not inn or not account:
        return  # без этих двух запись бессмысленна

    stroka = {stolbets: _znachenie(data, pole) for pole, stolbets in POLYA.items()}
    vat_status, vat_rate = _nds_iz_scheta(data)
    seychas = db.now()
    conn.execute(
        "INSERT INTO poluchateli (company_id, inn, kpp, name, full_name, account,"
        " bank_name, bank_city, bic, corr_account, vat_status, vat_rate, schetov,"
        " first_at, last_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)"
        " ON CONFLICT (company_id, inn, account) DO UPDATE SET"
        "   schetov = schetov + 1,"
        "   last_at = excluded.last_at,"
        # НДС — наоборот, последнее известное: получатель мог перейти на УСН
        # или, наоборот, стать плательщиком НДС. Неопределённость не затирает.
        "   vat_status = CASE WHEN excluded.vat_status = '' THEN poluchateli.vat_status"
        "     ELSE excluded.vat_status END,"
        "   vat_rate = CASE WHEN excluded.vat_status = '' THEN poluchateli.vat_rate"
        "     ELSE excluded.vat_rate END,"
        # Пустое в справочнике заполняем, заполненное не трогаем: справочник
        # накапливает, а не переписывается последним счётом.
        "   kpp = CASE WHEN poluchateli.kpp = '' THEN excluded.kpp ELSE poluchateli.kpp END,"
        "   name = CASE WHEN poluchateli.name = '' THEN excluded.name ELSE poluchateli.name END,"
        "   full_name = CASE WHEN poluchateli.full_name = '' THEN excluded.full_name"
        "     ELSE poluchateli.full_name END,"
        "   bank_name = CASE WHEN poluchateli.bank_name = '' THEN excluded.bank_name"
        "     ELSE poluchateli.bank_name END,"
        "   bank_city = CASE WHEN poluchateli.bank_city = '' THEN excluded.bank_city"
        "     ELSE poluchateli.bank_city END,"
        "   bic = CASE WHEN poluchateli.bic = '' THEN excluded.bic ELSE poluchateli.bic END,"
        "   corr_account = CASE WHEN poluchateli.corr_account = '' THEN excluded.corr_account"
        "     ELSE poluchateli.corr_account END",
        (
            company_id,
            stroka["inn"],
            stroka["kpp"],
            stroka["name"],
            stroka["full_name"],
            stroka["account"],
            stroka["bank_name"],
            stroka["bank_city"],
            stroka["bic"],
            stroka["corr_account"],
            vat_status,
            vat_rate,
            seychas,
            seychas,
        ),
    )


def zapomnit_nds(conn: sqlite3.Connection, company_id: int, data: dict) -> None:
    """Бухгалтер выбрал ставку на экране проверки — это и есть знание о получателе."""
    vat_status, vat_rate = _nds_iz_scheta(data)
    if not vat_status:
        return
    conn.execute(
        "UPDATE poluchateli SET vat_status = ?, vat_rate = ?"
        " WHERE company_id = ? AND inn = ? AND account = ?",
        (
            vat_status,
            vat_rate,
            company_id,
            _znachenie(data, "recipient_inn"),
            _znachenie(data, "recipient_account"),
        ),
    )


def otmetit_platezh(conn: sqlite3.Connection, company_id: int, data: dict) -> None:
    """Счёт ушёл в банк — отметить это у получателя."""
    conn.execute(
        "UPDATE poluchateli SET platezhey = platezhey + 1, last_at = ?"
        " WHERE company_id = ? AND inn = ? AND account = ?",
        (
            db.now(),
            company_id,
            _znachenie(data, "recipient_inn"),
            _znachenie(data, "recipient_account"),
        ),
    )


def nayti(conn: sqlite3.Connection, company_id: int, inn: str, account: str = ""):
    """Запись справочника: по счёту, если он известен, иначе по ИНН."""
    if not inn:
        return None
    if account:
        row = conn.execute(
            "SELECT * FROM poluchateli WHERE company_id = ? AND inn = ? AND account = ?",
            (company_id, inn, account),
        ).fetchone()
        if row is not None:
            return row
    rows = conn.execute(
        "SELECT * FROM poluchateli WHERE company_id = ? AND inn = ? ORDER BY platezhey DESC",
        (company_id, inn),
    ).fetchall()
    # Если у ИНН несколько счетов — молча выбирать нельзя, это разные реквизиты
    return rows[0] if len(rows) == 1 else None


def dopolnit(conn: sqlite3.Connection, company_id: int, data: dict) -> list[str]:
    """Заполнить пустые поля из справочника. Возвращает заметки о подстановке.

    Распознанное не трогаем никогда: справочник только дописывает то, чего
    в счёте прочитать не удалось.
    """
    inn = _znachenie(data, "recipient_inn")
    account = _znachenie(data, "recipient_account")
    izvestny = nayti(conn, company_id, inn, account)
    if izvestny is None:
        return []

    obratno = {stolbets: pole for pole, stolbets in POLYA.items()}
    podstavleno = []
    for stolbets in DOPOLNYAEM:
        pole = obratno[stolbets]
        if _znachenie(data, pole) or not (izvestny[stolbets] or "").strip():
            continue
        data[pole] = izvestny[stolbets]
        podstavleno.append(stolbets)

    # Наименование — единственное поле, которое справочник вправе ЗАМЕНИТЬ,
    # а не только дописать: со скана оно приезжает мусором («(Иттолнитель):
    # Москва, пр-кт…»), а ИНН и счёт при этом читаются верно. Раз этому
    # получателю уже платили по тем же реквизитам, его имя мы знаем точно.
    if data.get("_name_unreliable") and (izvestny["name"] or "").strip():
        data["recipient_name"] = izvestny["name"]
        if izvestny["full_name"]:
            data["recipient_full"] = izvestny["full_name"]
        data["_name_unreliable"] = False
        podstavleno.append("name")

    zametki = []
    if podstavleno:
        nazvaniya = {
            "kpp": "КПП",
            "bank_city": "город банка",
            "bank_name": "банк",
            "corr_account": "корр. счёт",
            "full_name": "полное наименование",
            "name": "наименование (со скана прочиталось ненадёжно)",
        }
        perechen = ", ".join(nazvaniya[s] for s in podstavleno)
        zametki.append(
            f"из справочника получателей подставлено: {perechen} — "
            f"сверьте со счётом, реквизиты могли смениться"
        )
    zametki += dopolnit_nds(data, izvestny)
    return zametki


def dopolnit_nds(data: dict, izvestny) -> list[str]:
    """НДС из истории получателя — когда сам счёт про НДС молчит.

    Счёт с `vat_status = unknown` иначе уходит человеку с вопросом. Если по
    прошлым счетам известно, что получатель без НДС, ставим «без НДС»; если
    известна ставка — считаем сумму НДС от итога. Оба случая помечаются
    заметкой, и на экране проверки ставку можно поменять одним выбором.
    """
    if _znachenie(data, "vat_status") != "unknown" or _znachenie(data, "vat_amount"):
        return []
    izvestny_status = (izvestny["vat_status"] or "").strip()
    if izvestny_status == "none":
        data["vat_status"] = "none"
        data["vat_rate"] = None
        data["vat_amount"] = None
        data["_vat_iz_spravochnika"] = True
        return [
            "НДС в счёте не найден; по прошлым счетам этот получатель работает без НДС — "
            "поставлено «Без НДС», при необходимости выберите ставку"
        ]
    if izvestny_status == "included" and (izvestny["vat_rate"] or "").strip():
        rate = stavka(izvestny["vat_rate"])
        summa = nds_ot_itoga(_znachenie(data, "total_amount"), rate)
        if summa is None:
            return []
        data["vat_status"] = "included"
        data["vat_rate"] = rate
        data["vat_amount"] = summa
        data["_vat_iz_spravochnika"] = True
        return [
            f"НДС в счёте не найден; по прошлым счетам у этого получателя ставка {rate}% — "
            f"сумма НДС {summa} рассчитана от итога, сверьте со счётом"
        ]
    return []


def nds_ot_itoga(total: str, rate: str) -> str | None:
    """Сумма НДС «в том числе»: итог × ставка / (100 + ставка)."""
    try:
        itog = Decimal(total.replace(" ", "").replace(",", "."))
        stavka_ = Decimal(rate.replace(",", "."))
    except Exception:
        return None
    if itog <= 0 or stavka_ < 0:
        return None
    summa = (itog * stavka_ / (100 + stavka_)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{summa:.2f}"


def spisok(conn: sqlite3.Connection, company_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM poluchateli WHERE company_id = ?"
        " ORDER BY platezhey DESC, schetov DESC, name",
        (company_id,),
    ).fetchall()


def dvoyniki(rows: list[sqlite3.Row]) -> set[str]:
    """ИНН, у которых в справочнике больше одного счёта, — повод присмотреться."""
    vstrecheno: dict[str, int] = {}
    for row in rows:
        vstrecheno[row["inn"]] = vstrecheno.get(row["inn"], 0) + 1
    return {inn for inn, skolko in vstrecheno.items() if skolko > 1}
