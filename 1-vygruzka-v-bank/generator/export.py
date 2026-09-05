"""Сборка файла 1c_to_kl.txt формата 1CClientBankExchange.

Спецификация разобрана в FORMAT-1CClientBankExchange.md.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .models import BankProfile, Invoice, Payer, PaymentOrder
from .numbering import NumberRegistry
from .validate import Problem, check_encodable, has_errors, validate_batch

CRLF = "\r\n"
DOC_KIND = "Платежное поручение"

# символы, которых нет в cp1251 — приводим к ближайшему допустимому
SANITIZE = {
    " ": " ",  # неразрывный пробел
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "−": "-",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "…": "...",
    "﻿": "",
}


class ExportError(RuntimeError):
    def __init__(self, problems: list[Problem]):
        super().__init__("\n".join(str(p) for p in problems))
        self.problems = problems


def sanitize(text: str) -> str:
    for bad, good in SANITIZE.items():
        text = text.replace(bad, good)
    # значение всегда в одну строку
    return " ".join(text.split())


def money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def rate(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _vat_phrase(invoice: Invoice) -> str:
    if invoice.vat_amount is not None:
        if invoice.vat_rate is not None:
            return f" В т.ч. НДС {rate(invoice.vat_rate)}% - {money(invoice.vat_amount)} руб."
        return f" В т.ч. НДС - {money(invoice.vat_amount)} руб."
    return " Без НДС."


# Первое слово описания — обычное русское слово с заглавной («Услуги», «Дюбель»):
# после «за» его можно писать со строчной. Аббревиатуры и марки не трогаем.
_CAPITALIZED_WORD_RE = re.compile(r"^[А-ЯЁ][а-яё]+(?=[\s,.;:\-]|$)")
_COMMON_PREFIX_MIN_WORDS = 2


def _plural_positions(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "позиция"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "позиции"
    return "позиций"


def _cut_words(text: str, limit: int) -> str:
    """Обрезать по границе слова, не оставляя висящей запятой или предлога."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    cut = cut.rstrip(" ,;:-—(")
    words = cut.split(" ")
    while words and len(words[-1]) <= 2 and words[-1].isalpha():
        words.pop()  # «… перевозка профиля с» -> без предлога на конце
    return " ".join(words)


def fit_description(description: str, budget: int) -> str:
    """Уложить описание «за что платим» в отведённое число знаков.

    Позиции приходят через «; ». Целиком влезли — перечисляем через запятую.
    Не влезли — обобщаем: у позиций вида «Заправка картриджа Kyocera 3253ci
    черн.» × 6 берём общее начало и пишем «заправка картриджа Kyocera
    (6 позиций)»; иначе перечисляем, сколько влезает, и добавляем «и др.».
    Одну позицию, которая не влезает сама, обрезаем по слову.
    """
    items = [i.strip(" .;,") for i in description.split(";")]
    items = [" ".join(i.split()) for i in items if i.strip()]
    if not items or budget < 12:
        return ""

    if _CAPITALIZED_WORD_RE.match(items[0]):
        items[0] = items[0][0].lower() + items[0][1:]

    full = ", ".join(items)
    if len(full) <= budget:
        return full

    if len(items) > 1:
        words = [i.split(" ") for i in items]
        prefix: list[str] = []
        for column in zip(*words):
            if len({w.lower() for w in column}) == 1:
                prefix.append(column[0])
            else:
                break
        tail = f" ({len(items)} {_plural_positions(len(items))})"
        if len(prefix) >= _COMMON_PREFIX_MIN_WORDS:
            text = " ".join(prefix).rstrip(" ,-—") + tail
            if len(text) <= budget:
                return text

        for keep in range(len(items) - 1, 0, -1):
            text = ", ".join(items[:keep]) + " и др."
            if len(text) <= budget:
                return text

    return _cut_words(items[0], budget)


def build_purpose(invoice: Invoice, max_length: int) -> str:
    """Назначение платежа. Готовый текст из счёта имеет приоритет.

    Без готового текста собирается по шаблону «Оплата по счету № N от Д
    за <описание>. В т.ч. НДС …». Описание ужимается так, чтобы хвост про НДС
    уцелел: обрезать назначение с конца нельзя — банк и получатель смотрят
    именно на сумму НДС.
    """
    if invoice.payment_purpose:
        base = sanitize(invoice.payment_purpose)
        if len(base) > max_length:
            base = base[: max_length - 3].rstrip() + "..."
        return base

    number = invoice.invoice_number or "б/н"
    head = f"Оплата по счету № {number} от {invoice.invoice_date:%d.%m.%Y}"
    vat = _vat_phrase(invoice)
    description = ""
    if invoice.payment_description:
        budget = max_length - len(sanitize(head)) - len(" за ") - len(".") - len(vat)
        description = fit_description(sanitize(invoice.payment_description), budget)
    base = f"{head} за {description}.{vat}" if description else f"{head}.{vat}"
    base = sanitize(base)
    if len(base) > max_length:
        base = base[: max_length - 3].rstrip() + "..."
    return base


class Exporter:
    def __init__(self, payer: Payer, profile: BankProfile, registry: NumberRegistry):
        self.payer = payer
        self.profile = profile
        self.registry = registry

    # --- сборка строк ---

    def _put(self, lines: list[str], key: str, value: str | None) -> None:
        text = sanitize("" if value is None else str(value))
        if not text and not self.profile.emit_empty_fields:
            return
        lines.append(f"{key}={text}")

    def _header(self, orders: list[PaymentOrder], now: datetime) -> list[str]:
        dates = [o.date for o in orders]
        lines = ["1CClientBankExchange"]
        self._put(lines, "ВерсияФормата", self.profile.format_version)
        self._put(lines, "Кодировка", self.profile.encoding)
        self._put(lines, "Отправитель", self.profile.sender)
        lines.append(f"Получатель={sanitize(self.profile.receiver)}")
        self._put(lines, "ДатаСоздания", f"{now:%d.%m.%Y}")
        self._put(lines, "ВремяСоздания", f"{now:%H:%M:%S}")
        lines.append(f"ДатаНачала={min(dates):%d.%m.%Y}")
        lines.append(f"ДатаКонца={max(dates):%d.%m.%Y}")
        lines.append(f"РасчСчет={self.payer.account}")
        lines.append(f"Документ={DOC_KIND}")
        return lines

    def _document(self, order: PaymentOrder) -> list[str]:
        inv = order.invoice
        payer = self.payer
        lines = [f"СекцияДокумент={DOC_KIND}"]
        put = self._put

        lines.append(f"Номер={order.number}")
        lines.append(f"Дата={order.date:%d.%m.%Y}")
        lines.append(f"Сумма={money(inv.total_amount)}")

        # плательщик
        lines.append(f"ПлательщикСчет={payer.account}")
        lines.append(f"Плательщик=ИНН {payer.inn} {sanitize(payer.name)}")
        lines.append(f"ПлательщикИНН={payer.inn}")
        put(lines, "Плательщик1", payer.full_name or payer.name)
        lines.append(f"ПлательщикРасчСчет={payer.account}")
        put(lines, "ПлательщикБанк1", payer.bank_name)
        put(lines, "ПлательщикБанк2", payer.bank_city)
        lines.append(f"ПлательщикБИК={payer.bic}")
        lines.append(f"ПлательщикКорсчет={payer.corr_account}")
        put(lines, "ПлательщикКПП", payer.kpp)

        # получатель
        lines.append(f"ПолучательСчет={inv.recipient_account}")
        lines.append(f"Получатель=ИНН {inv.recipient_inn} {sanitize(inv.recipient_name)}")
        lines.append(f"ПолучательИНН={inv.recipient_inn}")
        put(lines, "Получатель1", inv.recipient_full or inv.recipient_name)
        lines.append(f"ПолучательРасчСчет={inv.recipient_account}")
        put(lines, "ПолучательБанк1", inv.recipient_bank_name)
        put(lines, "ПолучательБанк2", inv.recipient_bank_city)
        lines.append(f"ПолучательБИК={inv.recipient_bic}")
        put(lines, "ПолучательКорсчет", inv.recipient_corr_account)
        put(lines, "ПолучательКПП", inv.recipient_kpp)

        # реквизиты платежа
        put(lines, "ВидПлатежа", self.profile.payment_kind)
        put(lines, "ВидОплаты", self.profile.payment_type)
        put(lines, "Очередность", self.profile.priority)
        put(lines, "Код", self.profile.uin_when_absent)
        # Поле 20 «Наз. пл.»: при выплате физлицу (самозанятому в том числе)
        # обязателен код вида дохода — 229-ФЗ, ст. 8 ч. 5.1. Организации и ИП —
        # пусто, иначе банк примет платёж за зарплату.
        if inv.is_person:
            put(lines, "КодНазПлатежа", self.profile.income_code_for_persons)
        lines.append(f"НазначениеПлатежа={order.purpose}")
        if self.profile.duplicate_purpose_line:
            lines.append(f"НазначениеПлатежа1={order.purpose}")

        lines.append("КонецДокумента")
        return lines

    # --- публичный интерфейс ---

    def build(
        self,
        invoices: list[Invoice],
        payment_date: date | None = None,
        now: datetime | None = None,
    ) -> tuple[str, list[PaymentOrder], list[Problem]]:
        now = now or datetime.now()
        payment_date = payment_date or now.date()

        problems = validate_batch(self.payer, invoices, self.profile)
        if has_errors(problems):
            raise ExportError(problems)

        for invoice in invoices:
            previous = self.registry.already_paid(self.payer, invoice)
            if previous is not None:
                problems.append(
                    Problem(
                        "warning",
                        f"счёт № {invoice.invoice_number}",
                        f"уже выгружался ранее как платёжка № {previous['number']} "
                        f"от {previous['date']} — номер будет тот же, повторной оплаты не будет",
                    )
                )

        orders = []
        for invoice in invoices:
            number = self.registry.assign(self.payer, invoice, payment_date)
            orders.append(
                PaymentOrder(
                    number=number,
                    date=payment_date,
                    invoice=invoice,
                    purpose=build_purpose(invoice, self.profile.purpose_max_length),
                )
            )

        lines = self._header(orders, now)
        for order in orders:
            lines.extend(self._document(order))
        lines.append("КонецФайла")

        text = CRLF.join(lines) + CRLF

        encoding_problems = check_encodable(text, self.profile.codec, "файл выгрузки")
        if encoding_problems:
            raise ExportError(encoding_problems)

        return text, orders, problems

    def write(
        self,
        invoices: list[Invoice],
        out_path: str | Path,
        payment_date: date | None = None,
        now: datetime | None = None,
    ) -> tuple[Path, list[PaymentOrder], list[Problem]]:
        text, orders, problems = self.build(invoices, payment_date=payment_date, now=now)
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode(self.profile.codec))
        self.registry.record_export(path.name, [o.number for o in orders], self.payer)
        self.registry.save()
        return path, orders, problems
