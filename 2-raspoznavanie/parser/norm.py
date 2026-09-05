"""Нормализация того, что вытащили из PDF: числа, даты, наименования."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "ма": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}

NBSP = " "
NARROW_NBSP = " "

# 20 знаков, начинается на 301 — корреспондентский счёт
CORR_PREFIX = "301"


def clean(text: str) -> str:
    return " ".join(text.replace(NBSP, " ").replace(NARROW_NBSP, " ").split())


def parse_amount(text: str) -> Decimal | None:
    """'27 500.00', '10000,00', '244 950,00 ₽' -> Decimal."""
    if not text:
        return None
    raw = clean(text).replace(NBSP, "")
    raw = re.sub(r"[^\d.,\-]", "", raw)
    raw = raw.replace(" ", "")
    if not raw or not re.search(r"\d", raw):
        return None
    # последний разделитель считаем десятичным, если за ним ровно 2 цифры
    match = re.search(r"[.,](\d{1,2})$", raw)
    if match:
        head = raw[: match.start()].replace(".", "").replace(",", "")
        tail = match.group(1)
        raw = f"{head}.{tail}"
    else:
        raw = raw.replace(".", "").replace(",", "")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


#  «16 000.0» встречается наравне с «16 000.00» — дробная часть бывает в один знак
AMOUNT_RE = re.compile(r"\d[\d\s ]*(?:[.,]\d{1,2})?")


def amounts_in(text: str) -> list[Decimal]:
    """Все денежные величины в строке, слева направо."""
    out = []
    for chunk in AMOUNT_RE.findall(text):
        value = parse_amount(chunk)
        if value is not None:
            out.append(value)
    return out


def parse_date(text: str) -> date | None:
    """'04 Августа 2026', '13 июля 2026 г.', '22.07.26', '22.07.2026'."""
    if not text:
        return None
    raw = clean(text)

    match = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", raw)
    if match:
        day, month, year = (int(g) for g in match.groups())
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None

    match = re.search(r"(\d{1,2})\s+([А-Яа-яЁё]{3,})\s+(\d{4})", raw)
    if match:
        day = int(match.group(1))
        name = match.group(2).lower()
        year = int(match.group(3))
        for prefix, number in MONTHS.items():
            if name.startswith(prefix):
                try:
                    return date(year, number, day)
                except ValueError:
                    return None
    return None


def looks_broken(name: str) -> bool:
    """Признак разъехавшегося кернинга: 'СО ВРЕМ ЕН Н Ы Й ДОМ'."""
    letters = [t for t in name.split() if t.isalpha()]
    singles = sum(1 for t in letters if len(t) == 1)
    return singles >= 2


def strip_quotes(name: str) -> str:
    return clean(name.strip(" .,;:"))


def account_kind(account: str) -> str:
    return "corr" if account.startswith(CORR_PREFIX) else "settlement"


def digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def account_key_ok(account: str, bic: str, corr: bool = False) -> bool:
    """Контрольный ключ счёта по алгоритму Банка России.

    Тот же расчёт, что в generator.validate.check_account части 1: алгоритм
    задан ЦБ и не меняется, а тянуть часть 1 в парсер ради шести строк не стоит.
    Нужен, чтобы опознать БИК без подписи: на скане «БИК» читается как «вик»
    или «ьик», зато под верным БИК сходятся ключи расчётного и корр. счетов.
    """
    if not (account.isdigit() and len(account) == 20 and bic.isdigit() and len(bic) == 9):
        return False
    prefix = "0" + bic[4:6] if corr else bic[6:9]
    control = prefix + account
    weights = (7, 1, 3)
    return sum(int(c) * weights[i % 3] for i, c in enumerate(control)) % 10 == 0
