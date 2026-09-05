"""Проверки части 1. Запуск: python -m unittest discover -s tests"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from generator.export import ExportError, Exporter, build_purpose, fit_description  # noqa: E402
from generator.models import BankProfile, Invoice, Payer  # noqa: E402
from generator.numbering import NumberRegistry  # noqa: E402
from generator.validate import (  # noqa: E402
    check_account,
    check_inn,
    check_payment_number,
    validate_invoice,
)

PAYER = {
    "id": "romashka",
    "name": "ООО Ромашка",
    "inn": "7719617469",
    "kpp": "771901001",
    "account": "40702810300180001774",
    "bank_name": "АО ОТП БАНК",
    "bank_city": "Г. МОСКВА",
    "bic": "044525311",
    "corr_account": "30101810000000000311",
    "numbering_start": 900001,
}

INVOICE = {
    "invoice_number": "1274",
    "invoice_date": "2026-08-20",
    "recipient_name": "ООО Прогресс Парк",
    "recipient_inn": "7701325465",
    "recipient_account": "40702810123111111114",
    "recipient_bank_name": "ПАО БАНК ПЕТРОКОММЕРЦ",
    "recipient_bank_city": "Г. МОСКВА",
    "recipient_bic": "044525352",
    "recipient_corr_account": "30101810700000000352",
    "total_amount": "12 354,00",
    "vat_rate": "20",
    "vat_amount": "2059.00",
}

PAY_DATE = date(2026, 8, 26)


def payer(**overrides) -> Payer:
    return Payer.from_dict({**PAYER, **overrides})


def invoice(**overrides) -> Invoice:
    return Invoice.from_dict({**INVOICE, **overrides})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry_path = Path(self.tmp.name) / "reestr.json"
        self.profile = BankProfile()

    def tearDown(self):
        self.tmp.cleanup()

    def exporter(self, p: Payer | None = None) -> Exporter:
        return Exporter(p or payer(), self.profile, NumberRegistry(self.registry_path))


class TestParsing(Base):
    def test_amount_formats(self):
        self.assertEqual(invoice(total_amount="12 354,00").total_amount, Decimal("12354.00"))
        self.assertEqual(invoice(total_amount="5000-50").total_amount, Decimal("5000.50"))
        self.assertEqual(invoice(total_amount=1200.5).total_amount, Decimal("1200.5"))

    def test_original_strings_are_kept(self):
        inv = invoice(total_amount="12 354,00")
        self.assertEqual(inv.total_amount_str, "12 354,00")
        self.assertEqual(inv.total_amount, Decimal("12354.00"))


class TestValidation(Base):
    def test_inn_checksum(self):
        self.assertTrue(check_inn("7719617469"))
        self.assertFalse(check_inn("7719617460"))
        self.assertFalse(check_inn("77196174"))

    def test_account_checksum(self):
        self.assertTrue(check_account("40702810300180001774", "044525311"))
        self.assertFalse(check_account("40702810300180001775", "044525311"))
        self.assertTrue(check_account("30101810000000000311", "044525311", corr=True))

    def test_broken_recipient_account_is_error(self):
        problems = validate_invoice(invoice(recipient_account="40702810123111111115"))
        self.assertTrue(any(p.level == "error" for p in problems))

    def test_export_refuses_broken_data(self):
        with self.assertRaises(ExportError):
            self.exporter().build([invoice(recipient_inn="1234567890")], payment_date=PAY_DATE)

    def test_payment_number_rules(self):
        self.assertEqual(check_payment_number(900001), [])
        self.assertTrue(check_payment_number(0))
        self.assertTrue(check_payment_number(1234567))
        self.assertTrue(check_payment_number(900000))


class TestNumbering(Base):
    def test_starts_at_configured_number(self):
        _, orders, _ = self.exporter().build([invoice()], payment_date=PAY_DATE)
        self.assertEqual(orders[0].number, 900001)

    def test_sequential_within_batch(self):
        batch = [invoice(), invoice(invoice_number="1275")]
        _, orders, _ = self.exporter().build(batch, payment_date=PAY_DATE)
        self.assertEqual([o.number for o in orders], [900001, 900002])

    def test_repeat_export_reuses_same_numbers(self):
        """Повторное нажатие «сформировать» не должно порождать новые номера."""
        out = Path(self.tmp.name) / "1.txt"
        _, first, _ = self.exporter().write([invoice()], out, payment_date=PAY_DATE)
        _, second, _ = self.exporter().write([invoice()], out, payment_date=PAY_DATE)
        self.assertEqual([o.number for o in first], [o.number for o in second])

    def test_repeat_export_warns_about_double_payment(self):
        out = Path(self.tmp.name) / "1.txt"
        self.exporter().write([invoice()], out, payment_date=PAY_DATE)
        _, _, problems = self.exporter().build([invoice()], payment_date=PAY_DATE)
        self.assertTrue(any("уже выгружался" in p.message for p in problems))

    def test_counter_continues_across_exports(self):
        out = Path(self.tmp.name) / "1.txt"
        self.exporter().write([invoice()], out, payment_date=PAY_DATE)
        _, orders, _ = self.exporter().write(
            [invoice(invoice_number="1275")], out, payment_date=PAY_DATE
        )
        self.assertEqual(orders[0].number, 900002)

    def test_counter_is_per_year(self):
        out = Path(self.tmp.name) / "1.txt"
        self.exporter().write([invoice()], out, payment_date=PAY_DATE)
        _, orders, _ = self.exporter().write(
            [invoice(invoice_number="1275")], out, payment_date=date(2027, 1, 15)
        )
        self.assertEqual(orders[0].number, 900001)

    def test_forbidden_number_is_skipped(self):
        """Номер с тремя нулями на конце запрещён ЦБ — счётчик его перешагивает."""
        _, orders, _ = self.exporter(payer(numbering_start=899999)).build(
            [invoice(), invoice(invoice_number="1275")], payment_date=PAY_DATE
        )
        self.assertEqual([o.number for o in orders], [899999, 900001])

    def test_counter_is_per_payer(self):
        out = Path(self.tmp.name) / "1.txt"
        self.exporter().write([invoice()], out, payment_date=PAY_DATE)
        other = payer(id="vasilek", numbering_start=500001)
        _, orders, _ = self.exporter(other).write([invoice()], out, payment_date=PAY_DATE)
        self.assertEqual(orders[0].number, 500001)


class TestFileFormat(Base):
    def build(self, invoices=None, **kwargs):
        text, orders, problems = self.exporter().build(
            invoices or [invoice()], payment_date=PAY_DATE, **kwargs
        )
        return text

    def test_markers_and_structure(self):
        text = self.build([invoice(), invoice(invoice_number="1275")])
        lines = text.split("\r\n")
        self.assertEqual(lines[0], "1CClientBankExchange")
        self.assertEqual(lines[-2], "КонецФайла")
        self.assertEqual(text.count("СекцияДокумент=Платежное поручение"), 2)
        self.assertEqual(text.count("КонецДокумента"), 2)
        self.assertNotIn("СекцияРасчСчет", text)  # это выписка, не наш случай

    def test_crlf_only(self):
        text = self.build()
        self.assertNotIn("\n", text.replace("\r\n", ""))

    def test_cp1251_encodable(self):
        self.build().encode("cp1251")

    def test_amount_and_date_format(self):
        text = self.build()
        self.assertIn("Сумма=12354.00", text)
        self.assertIn("Дата=26.08.2026", text)
        self.assertIn("ДатаНачала=26.08.2026", text)

    def test_header_dates_span_the_batch(self):
        text = self.build()
        self.assertIn("ДатаНачала=26.08.2026", text)
        self.assertIn("ДатаКонца=26.08.2026", text)

    def test_purpose_is_duplicated(self):
        text = self.build()
        purpose = "Оплата по счету № 1274 от 20.08.2026. В т.ч. НДС 20% - 2059.00 руб."
        self.assertIn(f"НазначениеПлатежа={purpose}", text)
        self.assertIn(f"НазначениеПлатежа1={purpose}", text)

    def test_no_empty_lines_by_default(self):
        text = self.build([invoice(recipient_kpp="")])
        self.assertNotIn("ПолучательКПП=\r\n", text)

    def test_written_file_is_cp1251(self):
        out = Path(self.tmp.name) / "1c_to_kl.txt"
        self.exporter().write([invoice()], out, payment_date=PAY_DATE)
        raw = out.read_bytes()
        self.assertIn("Платежное поручение".encode("cp1251"), raw)
        self.assertNotIn(b"\n\n", raw)
        raw.decode("cp1251")


class TestPurpose(Base):
    def test_vat_line(self):
        self.assertIn("В т.ч. НДС 20% - 2059.00 руб.", build_purpose(invoice(), 210))

    def test_without_vat(self):
        text = build_purpose(invoice(vat_rate=None, vat_amount=None), 210)
        self.assertTrue(text.endswith("Без НДС."))

    def test_explicit_purpose_wins(self):
        text = build_purpose(invoice(payment_purpose="Аванс по договору 7"), 210)
        self.assertEqual(text, "Аванс по договору 7")

    def test_truncation(self):
        text = build_purpose(invoice(payment_purpose="я" * 400), 210)
        self.assertEqual(len(text), 210)
        self.assertTrue(text.endswith("..."))

    def test_typographic_chars_are_sanitized(self):
        text = build_purpose(invoice(payment_purpose="Оплата – счёт «7»"), 210)
        text.encode("cp1251")
        self.assertIn("-", text)

    # --- за что платим ---

    def test_description_goes_between_number_and_vat(self):
        text = build_purpose(
            invoice(payment_description="Дюбель-гвоздь 6х40; Эмаль аэрозольная черная"), 210
        )
        self.assertEqual(
            text,
            "Оплата по счету № 1274 от 20.08.2026 за дюбель-гвоздь 6х40, "
            "Эмаль аэрозольная черная. В т.ч. НДС 20% - 2059.00 руб.",
        )

    def test_vat_tail_survives_long_description(self):
        """Обрезается описание, а не хвост про НДС: его читают банк и получатель."""
        text = build_purpose(invoice(payment_description="Услуги по договору " * 30), 210)
        self.assertLessEqual(len(text), 210)
        self.assertTrue(text.endswith("В т.ч. НДС 20% - 2059.00 руб."), text)
        self.assertNotIn("...", text)

    def test_common_prefix_is_generalized(self):
        items = "; ".join(
            f"Заправка картриджа Kyocera {model}"
            for model in ("3253ci черн.", "3253ci красн.", "3253ci син.", "FS-G8130MFP черн.",
                          "FS-C8130MFP син.", "M2040 черн.")
        )
        self.assertEqual(
            fit_description(items, 60), "заправка картриджа Kyocera (6 позиций)"
        )

    def test_positions_are_dropped_with_i_dr(self):
        items = "Шнур 100м на катушке; Круг отрезной по металлу; Сверло двустороннее; Рулетка 5м"
        self.assertEqual(
            fit_description(items, 60), "шнур 100м на катушке, Круг отрезной по металлу и др."
        )

    def test_single_position_is_cut_by_word(self):
        text = fit_description("Услуги газели 14.07.2026 г перевозка профиля с", 40)
        self.assertLessEqual(len(text), 40)
        self.assertEqual(text, "услуги газели 14.07.2026 г перевозка")

    def test_abbreviations_keep_their_case(self):
        self.assertEqual(fit_description("ООО Ромашка; E09 щит", 100), "ООО Ромашка, E09 щит")
        self.assertEqual(fit_description("   ", 100), "")
        self.assertEqual(fit_description("Что-то", 5), "")

    def test_description_fits_in_bank_limit_exactly(self):
        for budget in range(12, 211, 7):
            text = build_purpose(invoice(payment_description="Профиль крепежный горизонтальный " * 20), budget)
            self.assertLessEqual(len(text), budget)


if __name__ == "__main__":
    unittest.main()
