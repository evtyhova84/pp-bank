"""Мост к ядру проекта: части 1 (выгрузка) и 2 (распознавание).

Веб-сервис — это оболочка. Вся работа с форматом банка и разбором счетов
живёт в тех же модулях, что и раньше, и покрыта их собственными тестами.
Дублировать логику здесь нельзя: разъедется — и файл в банк уйдёт неверный.

Папки частей названы с цифрой в начале (`1-vygruzka-v-bank`), импортировать их
как пакеты нельзя, поэтому подключаем через путь — так же, как konveyer.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

KOREN = Path(__file__).resolve().parent.parent.parent
for chast in ("1-vygruzka-v-bank", "2-raspoznavanie"):
    put = str(KOREN / chast)
    if put not in sys.path:
        sys.path.insert(0, put)

from generator.cli import load_profile  # noqa: E402
from generator.export import ExportError, Exporter, build_purpose  # noqa: E402
from generator.models import Invoice, Payer, parse_date  # noqa: E402
from generator.validate import check_payment_number  # noqa: E402
from parser.parse import ParsedInvoice, parse_invoice  # noqa: E402

__all__ = [
    "ExportError",
    "Exporter",
    "Invoice",
    "build_purpose",
    "Payer",
    "check_payment_number",
    "load_profile",
    "parse_date",
    "ParsedInvoice",
    "parse_invoice",
    "payer_iz_stroki",
]

# Чем организация является сама по себе
POLYA_ORGANIZATSII = ("name", "full_name", "inn", "kpp")

# Чем она является в конкретном банке
POLYA_SCHETA = (
    "account",
    "bank_name",
    "bank_city",
    "bic",
    "corr_account",
    "numbering_start",
    "bank_profile",
)


def payer_iz_stroki(payer_row, schet_row) -> Payer:
    """Плательщик + его расчётный счёт -> объект, который принимает генератор.

    Генератор знает про одного плательщика с одним счётом: для него «ТЕТРАКОМ
    в Точке» и «ТЕТРАКОМ в Сбербанке» — разные плательщики, и это правильно.
    Нумерация платёжек в них независимая, ключ счётчика включает номер счёта.
    """
    data = {pole: payer_row[pole] for pole in POLYA_ORGANIZATSII}
    data.update({pole: schet_row[pole] for pole in POLYA_SCHETA})
    data["id"] = payer_row["code"]
    return Payer.from_dict(data)
