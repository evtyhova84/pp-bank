"""Платёжка физлицу и самозанятому: поле 20, КПП, ИНН. Правила — PRAVILA-samozanyatye.md."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from generator.export import Exporter  # noqa: E402
from generator.models import BankProfile, Invoice, Payer  # noqa: E402
from generator.numbering import NumberRegistry  # noqa: E402
from generator.validate import validate_invoice  # noqa: E402

PAYER = Payer.from_dict(
    {
        "id": "ip-test",
        "name": "ИП Тестова",
        "inn": "310603421444",
        "kpp": "",
        "account": "40802810920000993714",
        "bank_name": "ООО Банк Точка",
        "bank_city": "г. Москва",
        "bic": "044525104",
        "corr_account": "30101810745374525104",
        "numbering_start": 58,
    }
)

# Настоящий счёт с платформы НПД ФНС: ФИО без ИП, счёт 40817, «Режим НО: НПД».
SELF_EMPLOYED = {
    "invoice_number": "33236992",
    "invoice_date": "2026-09-01",
    "recipient_name": "БУЛОЙЧИК ИВАН АЛЕКСАНДРОВИЧ",
    "recipient_inn": "390514677445",
    "recipient_kpp": "",
    "recipient_kind": "self_employed",
    "recipient_account": "40817810126906007042",
    "recipient_bank_name": "ФИЛИАЛ № 7806 БАНКА ВТБ (ПАО)",
    "recipient_bic": "044030707",
    "recipient_corr_account": "30101810240300000707",
    "total_amount": "15000.00",
    "vat_status": "none",
    "payment_description": "Съёмка reels (10 видео)",
}

ORG = {
    "invoice_number": "1274",
    "invoice_date": "2026-08-20",
    "recipient_name": "ООО Прогресс Парк",
    "recipient_inn": "7701325465",
    "recipient_kpp": "770101001",
    "recipient_kind": "org",
    "recipient_account": "40702810123111111114",
    "recipient_bank_name": "ПАО БАНК ПЕТРОКОММЕРЦ",
    "recipient_bic": "044525352",
    "recipient_corr_account": "30101810700000000352",
    "total_amount": "12354.00",
    "vat_rate": "20",
    "vat_amount": "2059.00",
}

PAY_DATE = date(2026, 9, 4)


def build(invoices: list[Invoice], profile: BankProfile | None = None) -> tuple[str, list[str]]:
    with tempfile.TemporaryDirectory() as tmp:
        exporter = Exporter(PAYER, profile or BankProfile(), NumberRegistry(Path(tmp) / "reg.json"))
        text, _orders, problems = exporter.build(invoices, payment_date=PAY_DATE)
    return text, [str(p) for p in problems]


def section(text: str) -> list[str]:
    start = text.index("СекцияДокумент")
    return text[start:].splitlines()


class TestIsPerson(unittest.TestCase):
    def test_kind_from_parser_wins(self):
        self.assertTrue(Invoice.from_dict(SELF_EMPLOYED).is_person)
        self.assertTrue(Invoice.from_dict({**SELF_EMPLOYED, "recipient_kind": "person"}).is_person)
        self.assertFalse(Invoice.from_dict(ORG).is_person)
        self.assertFalse(Invoice.from_dict({**SELF_EMPLOYED, "recipient_kind": "ip"}).is_person)

    def test_legacy_invoice_judged_by_account_and_inn(self):
        """Счета, разобранные до появления признака: 40817 или ИНН из 12 знаков без ИП."""
        legacy = {k: v for k, v in SELF_EMPLOYED.items() if k != "recipient_kind"}
        self.assertTrue(Invoice.from_dict(legacy).is_person)
        ip = {**legacy, "recipient_name": "ИП Нефедов Сергей Владимирович",
              "recipient_account": "40802810200001418054"}
        self.assertFalse(Invoice.from_dict(ip).is_person)
        person_with_settlement = {**legacy, "recipient_account": "40802810200001418054"}
        self.assertTrue(Invoice.from_dict(person_with_settlement).is_person,
                        "ИНН из 12 знаков и в имени нет ИП — физлицо")


class TestIncomeCode(unittest.TestCase):
    def test_person_gets_code_1_and_no_kpp(self):
        text, problems = build([Invoice.from_dict(SELF_EMPLOYED)])
        lines = section(text)
        self.assertIn("КодНазПлатежа=1", lines)
        self.assertEqual(problems, [])
        self.assertFalse(any(line.startswith("ПолучательКПП") for line in lines))
        self.assertIn("Получатель=ИНН 390514677445 БУЛОЙЧИК ИВАН АЛЕКСАНДРОВИЧ", lines)
        # порядок реквизитов платежа: Код, затем поле 20, затем назначение
        self.assertLess(lines.index("Код=0"), lines.index("КодНазПлатежа=1"))
        self.assertLess(lines.index("КодНазПлатежа=1"),
                        next(i for i, l in enumerate(lines) if l.startswith("НазначениеПлатежа=")))

    def test_organisation_has_no_income_code(self):
        """Организации и ИП код не ставится: иначе банк примет платёж за зарплату."""
        text, _ = build([Invoice.from_dict(ORG)])
        self.assertNotIn("КодНазПлатежа", text)
        text, _ = build([Invoice.from_dict({**ORG, "recipient_kind": "ip", "recipient_kpp": "",
                                            "recipient_name": "ИП Прогресс П. П.",
                                            "recipient_inn": "500100732259"})])
        self.assertNotIn("КодНазПлатежа", text)

    def test_profile_can_switch_code_off(self):
        text, _ = build([Invoice.from_dict(SELF_EMPLOYED)],
                        BankProfile(income_code_for_persons=""))
        self.assertNotIn("КодНазПлатежа", text)

    def test_purpose_says_no_vat(self):
        text, _ = build([Invoice.from_dict(SELF_EMPLOYED)])
        purpose = next(l for l in section(text) if l.startswith("НазначениеПлатежа="))
        self.assertTrue(purpose.endswith("Без НДС."), purpose)
        self.assertIn("33236992", purpose)


class TestPersonValidation(unittest.TestCase):
    def test_kpp_on_person_is_error(self):
        """КПП у физлица — это КПП покупателя, распознанный не там."""
        problems = validate_invoice(Invoice.from_dict({**SELF_EMPLOYED, "recipient_kpp": "310601001"}))
        self.assertTrue(any("КПП не бывает" in p.message and p.level == "error" for p in problems), problems)

    def test_ten_digit_inn_on_person_is_error(self):
        problems = validate_invoice(Invoice.from_dict({**SELF_EMPLOYED, "recipient_inn": "7701325465"}))
        self.assertTrue(any("12 знаков" in p.message and p.level == "error" for p in problems), problems)

    def test_clean_self_employed_passes(self):
        self.assertEqual(validate_invoice(Invoice.from_dict(SELF_EMPLOYED)), [])


if __name__ == "__main__":
    unittest.main()
