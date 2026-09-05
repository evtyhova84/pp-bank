"""Проверка реквизитов до выгрузки.

Важно для части 2: реквизиты приезжают из распознавания, и контрольные суммы
ловят типичные ошибки OCR (перепутанные цифры) до того, как файл уйдёт в банк.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import BankProfile, Invoice, Payer


@dataclass
class Problem:
    level: str  # "error" | "warning"
    where: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.where}: {self.message}"


def _digits_only(value: str) -> bool:
    return bool(value) and value.isdigit()


def check_inn(inn: str) -> bool:
    """Контрольная сумма ИНН (10 или 12 знаков)."""
    if not _digits_only(inn):
        return False
    d = [int(c) for c in inn]
    if len(d) == 10:
        w = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        return sum(a * b for a, b in zip(w, d[:9])) % 11 % 10 == d[9]
    if len(d) == 12:
        w1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        w2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        n11 = sum(a * b for a, b in zip(w1, d[:10])) % 11 % 10
        n12 = sum(a * b for a, b in zip(w2, d[:11])) % 11 % 10
        return n11 == d[10] and n12 == d[11]
    return False


def check_kpp(kpp: str) -> bool:
    return len(kpp) == 9 and kpp[:4].isdigit() and kpp[4:6].isalnum() and kpp[6:].isdigit()


def check_bic(bic: str) -> bool:
    return _digits_only(bic) and len(bic) == 9


def check_account(account: str, bic: str, *, corr: bool = False) -> bool:
    """Контрольный ключ счёта по алгоритму ЦБ РФ."""
    if not (_digits_only(account) and len(account) == 20 and check_bic(bic)):
        return False
    prefix = "0" + bic[4:6] if corr else bic[6:9]
    control = prefix + account
    weights = [7, 1, 3]
    total = sum(int(c) * weights[i % 3] for i, c in enumerate(control))
    return total % 10 == 0


def check_payment_number(number: int) -> list[str]:
    """Ограничения ЦБ на поле 3 платёжного поручения."""
    problems = []
    text = str(number)
    if number <= 0:
        problems.append("номер должен быть больше нуля")
    if len(text) > 6:
        problems.append(f"номер длиннее 6 знаков: {text}")
    if len(text) >= 3 and text[-3:] == "000":
        problems.append(
            f"три последних разряда номера {text} равны '000' — "
            "запрещено при переводе через Банк России"
        )
    return problems


def check_encodable(text: str, codec: str, where: str) -> list[Problem]:
    problems = []
    for ch in set(text):
        try:
            ch.encode(codec)
        except UnicodeEncodeError:
            problems.append(
                Problem("error", where, f"символ {ch!r} (U+{ord(ch):04X}) не кодируется в {codec}")
            )
    return problems


def validate_payer(payer: Payer) -> list[Problem]:
    p: list[Problem] = []
    where = f"плательщик {payer.name or payer.id}"
    if not check_inn(payer.inn):
        p.append(Problem("error", where, f"ИНН {payer.inn!r} не проходит проверку"))
    if payer.kpp and not check_kpp(payer.kpp):
        p.append(Problem("error", where, f"КПП {payer.kpp!r} некорректен"))
    if not check_bic(payer.bic):
        p.append(Problem("error", where, f"БИК {payer.bic!r} некорректен"))
    else:
        if not check_account(payer.account, payer.bic):
            p.append(
                Problem("error", where, f"расчётный счёт {payer.account!r} не сходится с БИК")
            )
        if not check_account(payer.corr_account, payer.bic, corr=True):
            p.append(
                Problem("error", where, f"корр. счёт {payer.corr_account!r} не сходится с БИК")
            )
    if not payer.name:
        p.append(Problem("error", where, "не задано наименование плательщика"))
    if not payer.bank_city:
        p.append(Problem("warning", where, "не указан город банка (ПлательщикБанк2)"))
    return p


def validate_invoice(invoice: Invoice) -> list[Problem]:
    p: list[Problem] = []
    where = f"счёт № {invoice.invoice_number or '?'} от {invoice.invoice_date:%d.%m.%Y}"
    if not check_inn(invoice.recipient_inn):
        p.append(Problem("error", where, f"ИНН получателя {invoice.recipient_inn!r} не проходит проверку"))
    if invoice.recipient_kpp and not check_kpp(invoice.recipient_kpp):
        p.append(Problem("error", where, f"КПП получателя {invoice.recipient_kpp!r} некорректен"))
    if invoice.is_person:
        # Физлицо: ИНН 12 знаков, КПП не бывает. Чужой КПП в платёжке физлицу —
        # это КПП покупателя, распознанный не там; банк такую платёжку вернёт.
        if len(invoice.recipient_inn) != 12:
            p.append(Problem("error", where, f"у получателя-физлица ИНН из 12 знаков, а не {invoice.recipient_inn!r}"))
        if invoice.recipient_kpp:
            p.append(Problem("error", where, f"у получателя-физлица КПП не бывает, а указан {invoice.recipient_kpp!r}"))
    if not check_bic(invoice.recipient_bic):
        p.append(Problem("error", where, f"БИК получателя {invoice.recipient_bic!r} некорректен"))
    else:
        if not check_account(invoice.recipient_account, invoice.recipient_bic):
            p.append(
                Problem("error", where, f"счёт получателя {invoice.recipient_account!r} не сходится с БИК")
            )
        if invoice.recipient_corr_account and not check_account(
            invoice.recipient_corr_account, invoice.recipient_bic, corr=True
        ):
            p.append(
                Problem("error", where, f"корр. счёт получателя {invoice.recipient_corr_account!r} не сходится с БИК")
            )
    if invoice.total_amount <= 0:
        p.append(Problem("error", where, f"сумма платежа {invoice.total_amount} должна быть больше нуля"))
    if invoice.vat_amount is not None and invoice.vat_amount > invoice.total_amount:
        p.append(Problem("error", where, "сумма НДС больше суммы платежа"))
    if invoice.vat_amount is None and invoice.vat_rate is None and invoice.vat_status != "none":
        p.append(Problem("warning", where, "НДС не распознан — в назначении будет 'Без НДС'"))
    if not invoice.recipient_name:
        p.append(Problem("error", where, "не задано наименование получателя"))
    if not invoice.invoice_number:
        p.append(Problem("warning", where, "не распознан номер счёта — попадёт в назначение платежа"))
    return p


def validate_batch(payer: Payer, invoices: list[Invoice], profile: BankProfile) -> list[Problem]:
    problems = validate_payer(payer)
    for invoice in invoices:
        problems.extend(validate_invoice(invoice))
    if not invoices:
        problems.append(Problem("error", "пачка", "нет ни одного счёта для выгрузки"))
    seen: dict[str, str] = {}
    for invoice in invoices:
        key = f"{invoice.recipient_inn}|{invoice.invoice_number}|{invoice.invoice_date}"
        if key in seen:
            problems.append(
                Problem(
                    "warning",
                    "пачка",
                    f"счёт № {invoice.invoice_number} от {invoice.invoice_date:%d.%m.%Y} "
                    f"встречается в пачке дважды — проверьте, не двойная ли оплата",
                )
            )
        seen[key] = key
    return problems


def has_errors(problems: list[Problem]) -> bool:
    return any(p.level == "error" for p in problems)
