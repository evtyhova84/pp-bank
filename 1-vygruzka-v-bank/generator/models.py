"""Модели данных части 1: что генератор принимает на вход."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any


def _s(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_date(value: Any) -> date:
    """Принимает date, 'дд.мм.гггг' или 'гггг-мм-дд'."""
    if isinstance(value, date):
        return value
    text = _s(value)
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return date.fromisoformat(text) if fmt == "%Y-%m-%d" else _strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"не удалось разобрать дату: {value!r}")


def _strptime(text: str, fmt: str) -> date:
    from datetime import datetime

    return datetime.strptime(text, fmt).date()


def parse_amount(value: Any) -> Decimal:
    """Принимает число или строку из счёта: '12 354,00', '12354.00', '12354-00'."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = _s(value).replace(" ", "").replace(" ", "")
    text = text.replace("-", ".").replace(",", ".")
    if not text:
        raise ValueError("пустая сумма")
    return Decimal(text)


@dataclass
class Payer:
    """Реквизиты плательщика. Данные организации, не константы в коде."""

    id: str
    name: str
    inn: str
    account: str
    bank_name: str
    bic: str
    corr_account: str
    full_name: str = ""
    kpp: str = ""
    bank_city: str = ""
    numbering_start: int = 900001
    bank_profile: str = "default"

    @classmethod
    def from_dict(cls, data: dict) -> "Payer":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"неизвестные поля плательщика: {sorted(unknown)}")
        return cls(**{k: (int(v) if k == "numbering_start" else _s(v)) for k, v in data.items()})


PERSON_KINDS = ("self_employed", "person")
PERSON_ACCOUNT_PREFIXES = ("40817", "40820")
ORG_OR_IP_RE = re.compile(
    r"\b(ООО|ОАО|ЗАО|ПАО|АО|НАО|ИП)\b|Общество\s+с\s+ограниченной|"
    r"Индивидуальн\w+\s+предпринимател|Акционерное\s+общество",
    re.IGNORECASE,
)


@dataclass
class Invoice:
    """Счёт от поставщика — то, что отдаёт часть 2 (распознавание)."""

    invoice_number: str
    invoice_date: date
    recipient_name: str
    recipient_inn: str
    recipient_account: str
    recipient_bank_name: str
    recipient_bic: str
    recipient_corr_account: str
    total_amount: Decimal
    recipient_full: str = ""
    recipient_kpp: str = ""
    # org | ip | self_employed | person — кто получатель (ставит часть 2).
    # Физлицу в поле 20 идёт код вида дохода, КПП у него не бывает.
    recipient_kind: str = ""
    recipient_bank_city: str = ""
    vat_rate: Decimal | None = None
    vat_amount: Decimal | None = None
    payment_purpose: str = ""
    # За что платим — наименования позиций через «; ». Из них собирается
    # назначение платежа, если готового текста в payment_purpose нет.
    payment_description: str = ""
    # "included" | "none" | "unknown" — знает ли часть 2, что НДС нет
    vat_status: str = "unknown"
    # исходные строки из счёта — не теряем оригинал при ошибке распознавания
    total_amount_str: str = ""
    vat_amount_str: str = ""
    vat_rate_str: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Invoice":
        d = dict(data)
        known = {f.name for f in cls.__dataclass_fields__.values()}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"неизвестные поля счёта: {sorted(unknown)}")
        d.setdefault("total_amount_str", _s(d.get("total_amount")))
        d.setdefault("vat_amount_str", _s(d.get("vat_amount")))
        d.setdefault("vat_rate_str", _s(d.get("vat_rate")))
        d["invoice_date"] = parse_date(d["invoice_date"])
        d["total_amount"] = parse_amount(d["total_amount"])
        for key in ("vat_rate", "vat_amount"):
            raw = d.get(key)
            d[key] = None if _s(raw) == "" else parse_amount(raw)
        for key in known:
            if key not in d:
                continue
            if isinstance(d[key], str):
                d[key] = _s(d[key])
        return cls(**d)

    @property
    def is_person(self) -> bool:
        """Получатель — физлицо (самозанятый в том числе), а не организация или ИП."""
        if self.recipient_kind:
            return self.recipient_kind in PERSON_KINDS
        # Счета, разобранные до появления признака: судим по счёту и ИНН.
        if self.recipient_account.startswith(PERSON_ACCOUNT_PREFIXES):
            return True
        return len(self.recipient_inn) == 12 and not ORG_OR_IP_RE.search(self.recipient_name)

    def identity(self, payer: Payer) -> str:
        """Ключ счёта для идемпотентной нумерации."""
        return "|".join(
            [
                payer.id,
                payer.account,
                self.recipient_inn,
                self.recipient_account,
                self.invoice_number,
                self.invoice_date.isoformat(),
                f"{self.total_amount:.2f}",
            ]
        )


@dataclass
class BankProfile:
    """Придирки конкретного банка. Ядро генератора одно, различия — здесь."""

    name: str = "default"
    format_version: str = "1.02"
    encoding: str = "Windows"  # Windows -> cp1251, DOS -> cp866
    sender: str = "scheta-platezhki"
    receiver: str = ""
    emit_empty_fields: bool = False
    uin_when_absent: str = "0"
    purpose_max_length: int = 210
    payment_kind: str = ""  # ВидПлатежа: пусто = электронно
    payment_type: str = "01"  # ВидОплаты
    priority: str = "5"  # Очередность
    duplicate_purpose_line: bool = True
    # КодНазПлатежа (поле 20 «Наз. пл.») при выплате физлицу, самозанятому
    # в том числе: код вида дохода 1 по 229-ФЗ. Пусто — не ставить.
    income_code_for_persons: str = "1"

    CODECS = {"Windows": "cp1251", "DOS": "cp866"}

    @classmethod
    def from_dict(cls, data: dict) -> "BankProfile":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"неизвестные поля профиля банка: {sorted(unknown)}")
        return cls(**data)

    @property
    def codec(self) -> str:
        try:
            return self.CODECS[self.encoding]
        except KeyError:
            raise ValueError(
                f"кодировка {self.encoding!r} не поддерживается стандартом, "
                f"допустимо: {sorted(self.CODECS)}"
            ) from None


@dataclass
class PaymentOrder:
    """Готовая платёжка: счёт + присвоенный номер и дата."""

    number: int
    date: date
    invoice: Invoice
    purpose: str = ""
