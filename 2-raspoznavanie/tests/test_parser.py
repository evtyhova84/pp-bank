"""Проверки части 2. Запуск: python -m unittest discover -s tests

Строки в тестах взяты с реальных счетов из разных бухгалтерских программ,
поэтому тесты не зависят от наличия самих PDF.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parser.fields import (  # noqa: E402
    BUYER_LABEL_RE,
    find_name_after_label,
    split_columns,
    SUPPLIER_LABEL_RE,
    cut_address,
    find_accounts,
    find_bank_name,
    find_bic,
    find_inn_kpp,
    find_party_by_label,
    find_recipient_name,
    find_title,
    find_totals,
)
from parser.norm import amounts_in, looks_broken, parse_amount, parse_date  # noqa: E402
from parser.ocr import group_by_overlap  # noqa: E402
from parser.ocr_repair import repair_row  # noqa: E402
from parser.rows import _span_text  # noqa: E402

# СБИС / Тензор
SBIS = [
    'ФИЛИАЛ "НИЖЕГОРОДСКИЙ" АО "АЛЬФА-БАНК" БИК 042202824',
    "Банк получателя Корр.Счет № 30101810200000000824",
    "ИНН 1686001984 КПП 168601001 Счет № 40702810929070012754",
    'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "СМАРТ ЛАЙН"',
    "Получатель",
    "Счет на оплату № 1672 от 16 Июля 2026",
    'Поставщик: ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "СМАРТ ЛАЙН", ИНН 1686001984, КПП 168601001,',
    'Покупатель: ООО "ТЕТРАКОМ", ИНН 1661034770, КПП 166101001, Адрес: 420194',
    "Итого: 5 600.00",
    "В том числе НДС: 1 009.84",
    "Всего к оплате: 5 600.00",
]

# 1С:Бухгалтерия
BUH1C = [
    'ООО "СТРОЙ ЛИГА"',
    'Приволжский ф-л ПАО "Банк ПСБ" г. Нижний БИК 042202803',
    "Сч. № 30101810700000000803",
    "Банк получателя Сч. № 40702810503000077957",
    "ИНН 1658214010 КПП 166001001",
    'ООО "СТРОЙ ЛИГА"',
    "Получатель",
    "Счет на оплату № 643 от 10 августа 2026 г.",
    'Поставщик ООО "СТРОЙ ЛИГА", ИНН 1658214010, КПП 166001001, 420140, Татарстан Респ',
    'Покупатель ООО "ТЕТРАКОМ", ИНН 1661034770, КПП 166101001, 420094',
    "Итого: 49 050,00",
    "В том числе НДС 22%: 8 845,08",
    "Всего к оплате: 49 050,00",
]

# Т-Банк: подписи в одной строке, значения в следующей
TBANK = [
    'АО "ТБанк" г. Москва БИК',
    "044525974",
    "Сч. №30101810145250000974",
    "Банк получателя",
    "ИНН КПП Сч. №40702810410000318304",
    "1660250400 166001001",
    'Общество с ограниченной ответственностью "Омега"',
    "Получатель",
    "Счет на оплату № 1664 от 21 июля 2026 г.",
    "Итого: 3 867,00",
    "В том числе НДС 22%: 697,33",
    "Всего к оплате: 3 867,00",
]

# Сумма прописью в строке «Итого к оплате» — настоящая сумма выше
SPELLED = [
    "ИНН 1616030974 КПП 161601001 Р/Счет 40702810662000046360",
    "Спас, ООО",
    "Поставщик (получатель платежа)",
    'отделение "Банк Татарстан" №8610 ПАО Сбербанк БИК 049205603',
    "Корр.счет 30101810600000000603",
    "СЧЕТ № 66 от 07.08.2026",
    "Покупатель:Тетраком, ООО, ИНН 1661034770, КПП166101001",
    "Итого 22 200.00",
    "Итого к оплате: Двадцать две тысячи двести рублей 00 копеек",
    "В том числе НДС: 4 003.28",
]


class TestNumbers(unittest.TestCase):
    def test_thousands_and_separators(self):
        self.assertEqual(parse_amount("12 354,00"), Decimal("12354.00"))
        self.assertEqual(parse_amount("27 500.00"), Decimal("27500.00"))
        self.assertEqual(parse_amount("10000,00"), Decimal("10000.00"))
        self.assertEqual(parse_amount("244 950,00 ₽"), Decimal("244950.00"))

    def test_single_decimal_digit(self):
        """«Итого: 16 000.0» — один знак после точки тоже встречается."""
        self.assertEqual(parse_amount("16 000.0"), Decimal("16000.0"))
        self.assertEqual(amounts_in("Итого: 16 000.0")[-1], Decimal("16000.0"))

    def test_sequence_of_amounts(self):
        self.assertEqual(
            amounts_in("213 час 0% 1 150,00 244 950,00"),
            [Decimal(213), Decimal(0), Decimal("1150.00"), Decimal("244950.00")],
        )


class TestDates(unittest.TestCase):
    def test_russian_month_names(self):
        self.assertEqual(parse_date("16 Июля 2026"), date(2026, 7, 16))
        self.assertEqual(parse_date("10 августа 2026 г."), date(2026, 8, 10))
        self.assertEqual(parse_date("04 Августа 2026"), date(2026, 8, 4))

    def test_numeric(self):
        self.assertEqual(parse_date("22.07.26"), date(2026, 7, 22))
        self.assertEqual(parse_date("07.08.2026"), date(2026, 8, 7))

    def test_garbage(self):
        self.assertIsNone(parse_date("неизвестно когда"))


class TestHeader(unittest.TestCase):
    def test_accounts_split_by_prefix(self):
        for rows in (SBIS, BUH1C, TBANK):
            settlement, corr = find_accounts(rows)
            self.assertTrue(corr.startswith("301"), rows[0])
            self.assertFalse(settlement.startswith("301"), rows[0])
            self.assertEqual(len(settlement), 20)

    def test_bic_on_next_row(self):
        self.assertEqual(find_bic(TBANK), "044525974")
        self.assertEqual(find_bic(SBIS), "042202824")

    def test_inn_kpp_values_on_next_row(self):
        self.assertEqual(find_inn_kpp(TBANK), ("1660250400", "166001001"))

    def test_inn_kpp_inline(self):
        self.assertEqual(find_inn_kpp(SBIS), ("1686001984", "168601001"))

    def test_title(self):
        index, number, when = find_title(BUH1C)
        self.assertEqual(number, "643")
        self.assertEqual(parse_date(when), date(2026, 8, 10))

    def test_title_without_words_na_oplatu(self):
        _, number, when = find_title(SPELLED)
        self.assertEqual(number, "66")
        self.assertEqual(parse_date(when), date(2026, 8, 7))

    def test_bank_name(self):
        self.assertIn("АЛЬФА-БАНК", find_bank_name(SBIS))
        self.assertIn("Банк ПСБ", find_bank_name(BUH1C))

    def test_recipient_name_from_header(self):
        self.assertIn("Омега", find_recipient_name(TBANK))
        self.assertEqual(find_recipient_name(SPELLED), "Спас, ООО")


class TestParties(unittest.TestCase):
    def test_supplier(self):
        name, inn = find_party_by_label(BUH1C, SUPPLIER_LABEL_RE)
        self.assertEqual(inn, "1658214010")
        self.assertEqual(name, 'ООО "СТРОЙ ЛИГА"')

    def test_buyer(self):
        name, inn = find_party_by_label(SBIS, BUYER_LABEL_RE)
        self.assertEqual(inn, "1661034770")

    def test_label_without_requisites_is_ignored(self):
        """«Поставщик (получатель платежа)» — метка, из неё имя брать нельзя."""
        self.assertEqual(find_party_by_label(SPELLED, SUPPLIER_LABEL_RE), ("", ""))

    def test_address_is_cut_off(self):
        self.assertEqual(
            cut_address("ИП Чернов Игорь Юрьевич, улица Семиозерская, д. 22"),
            "ИП Чернов Игорь Юрьевич",
        )
        self.assertEqual(
            cut_address("ООО «Дизайн» 421001, РТ, г.Казань"), "ООО «Дизайн»"
        )
        self.assertEqual(cut_address("Спас, ООО"), "Спас, ООО")


class TestTotals(unittest.TestCase):
    def test_vat_included_with_rate(self):
        total, vat, rate, no_vat = find_totals(BUH1C)
        self.assertEqual((total, vat, rate), (Decimal("49050.00"), Decimal("8845.08"), Decimal(22)))
        self.assertFalse(no_vat)

    def test_rate_derived_from_amounts(self):
        """Ставка не написана — выводим из сумм: 1009.84 от 5600 это 22%."""
        _, _, rate, _ = find_totals(SBIS)
        self.assertEqual(rate, Decimal(22))

    def test_spelled_out_total_is_ignored(self):
        total, vat, _, _ = find_totals(SPELLED)
        self.assertEqual(total, Decimal("22200.00"))
        self.assertEqual(vat, Decimal("4003.28"))

    def test_explicit_no_vat(self):
        rows = ["Итого: 10 000,00", "Без налога (НДС) -", "Всего к оплате: 10 000,00"]
        total, vat, rate, no_vat = find_totals(rows)
        self.assertEqual(total, Decimal("10000.00"))
        self.assertIsNone(vat)
        self.assertIsNone(rate)
        self.assertTrue(no_vat)

    def test_zero_vat_is_explicit_not_missing(self):
        rows = ["Итого: 244 950,00", "Сумма НДС: 0,00", "Всего к оплате: 244 950,00"]
        _, vat, _, no_vat = find_totals(rows)
        self.assertIsNone(vat)
        self.assertTrue(no_vat)


class TestSpacing(unittest.TestCase):
    """Пробелы нулевой ширины внутри слов — артефакт генератора PDF."""

    @staticmethod
    def span(text: str, widths: list[float], size: float = 7.5) -> dict:
        chars, x = [], 0.0
        for symbol, width in zip(text, widths):
            chars.append({"c": symbol, "bbox": (x, 0.0, x + width, size)})
            x += width
        return {"size": size, "chars": chars}

    def test_zero_width_space_dropped(self):
        span = self.span("О БЩ", [4.0, 0.0, 4.0, 4.0])
        self.assertEqual(_span_text(span), "ОБЩ")

    def test_real_space_kept(self):
        span = self.span("ОБ ЩЕ", [4.0, 4.0, 3.0, 4.0, 4.0])
        self.assertEqual(_span_text(span), "ОБ ЩЕ")

    def test_broken_name_detector(self):
        self.assertTrue(looks_broken("ИП Низам ов А йнур Ф анилевич"))
        self.assertFalse(looks_broken('ООО "СМАРТ ЛАЙН"'))


# Счёт самозанятого с платформы НПД ФНС: шапка в две колонки, ФИО без ООО/ИП.
# Ячейки заданы координатами, потому что именно X разделяет продавца и покупателя.
NPD_CELLS = [
    [(56.0, "Оплату необходимо произвести по реквизитам расчетного счета")],
    [(58.0, "Продавец"), (330.0, "Покупатель")],
    [(60.0, "КОШЕЛЕВА АГНЕССА АЛЕКСАНДРОВНА"), (332.0, "ИП НЕМЦЕВА АННА АЛЕКСАНДРОВНА")],
    [(60.0, "Режим НО: НПД"), (332.0, "ИНН 310603421444")],
    [(60.0, "ИНН 972105235683")],
    [(60.0, 'АО "ТИНЬКОФФ БАНК"')],
    [(60.0, "БИК 044525974")],
    [(60.0, "Корр. счет 30101810145250000974")],
    [(60.0, "Расчетный счет 40817810500034555307")],
    [(56.0, "Счёт на оплату №32949527 от 25 августа 2026 г.")],
    [(56.0, "Всего наименований 1 на сумму 57 000,00 руб."), (620.0, "Итого к оплате: 57 000,00")],
]


class TestTwoColumnHeader(unittest.TestCase):
    """Если колонки не разделить, ИНН покупателя уедет в реквизиты получателя."""

    def setUp(self):
        columns = split_columns(NPD_CELLS)
        self.assertIsNotNone(columns)
        self.left, self.right = columns

    def test_seller_column_has_only_seller(self):
        self.assertEqual(find_inn_kpp(self.left), ("972105235683", ""))
        self.assertNotIn("310603421444", " ".join(self.left))

    def test_buyer_column_has_only_buyer(self):
        self.assertEqual(find_inn_kpp(self.right), ("310603421444", ""))

    def test_seller_requisites(self):
        settlement, corr = find_accounts(self.left)
        self.assertEqual(settlement, "40817810500034555307")
        self.assertEqual(corr, "30101810145250000974")
        self.assertEqual(find_bic(self.left), "044525974")

    def test_name_without_ooo_or_ip(self):
        """У самозанятого в наименовании просто ФИО, маркера организации нет."""
        self.assertEqual(find_name_after_label(self.left), "КОШЕЛЕВА АГНЕССА АЛЕКСАНДРОВНА")

    def test_single_column_layout_is_not_split(self):
        cells = [[(56.0, row)] for row in BUH1C]
        self.assertIsNone(split_columns(cells))

    def test_npd_means_no_vat(self):
        rows = [t for row in NPD_CELLS for _, t in row]
        total, vat, rate, no_vat = find_totals(rows)
        self.assertEqual(total, Decimal("57000.00"))
        self.assertIsNone(vat)
        self.assertIsNone(rate)
        self.assertTrue(no_vat, "«Режим НО: НПД» — НДС не бывает по определению")


class TestOcrRepair(unittest.TestCase):
    """OCR путает цифры с похожими буквами — но только в числовых местах."""

    def test_zeros_read_as_cyrillic_o(self):
        self.assertEqual(repair_row("Итого: 54 ооо,оо"), "Итого: 54 000,00")

    def test_letter_in_kpp(self):
        self.assertIn("КПП 532101001", repair_row("ИНН 5321200770, КПП S32101001, 173021"))

    def test_vat_rate_as_letter(self):
        self.assertEqual(repair_row("В том числе НДС S%:"), "В том числе НДС 5%:")

    def test_number_sign(self):
        self.assertEqual(
            repair_row("Счет на оплату NQ 317 от 18 апреля 2026 г."),
            "Счет на оплату № 317 от 18 апреля 2026 г.",
        )

    def test_names_are_left_alone(self):
        """Замены в наименованиях делать нельзя — «о» там настоящая буква."""
        for name in ("ООО «Коммерческий Промышленный Дизайн»", "Спас, ООО", "Услуги смм"):
            self.assertEqual(repair_row(name), name)


class TestOcrRowGrouping(unittest.TestCase):
    def line(self, x, y, h, text):
        return {"x": x, "y": y, "h": h, "text": text}

    def test_close_labels_are_not_merged(self):
        """«В том числе НДС» и «Всего к оплате» идут вплотную; склей их —
        и НДС станет равен итогу."""
        rows = group_by_overlap([
            self.line(525, 548, 15, "В том числе НДС 22%:"),
            self.line(754, 548, 14, "8 295,08"),
            self.line(579, 569, 13, "Всего к оплате:"),
            self.line(754, 569, 13, "46 000,00"),
        ])
        joined = [" ".join(t for _, t in row) for row in rows]
        self.assertEqual(joined, ["В том числе НДС 22%: 8 295,08", "Всего к оплате: 46 000,00"])

    def test_same_row_columns_are_merged(self):
        rows = group_by_overlap([
            self.line(51, 116, 16, "Банк получателя"),
            self.line(477, 118, 15, "БИК 041117601"),
        ])
        self.assertEqual(len(rows), 1)


class TestTotalCrossCheck(unittest.TestCase):
    """Итог напечатан трижды; если один распознан криво, спасают остальные."""

    def test_truncated_total_is_rejected(self):
        rows = [
            "Запчасти 12 580,00",
            "В том числе НДС 5%: 599,05",
            "Всего к оплате: 12",
            "Всего наименований 8, на сумму 12 580,00 руб.",
        ]
        total, vat, rate, _ = find_totals(rows)
        self.assertEqual(total, Decimal("12580.00"))
        self.assertEqual(vat, Decimal("599.05"))
        self.assertEqual(rate, Decimal(5))

    def test_total_below_vat_is_impossible(self):
        rows = ["Итого: 5", "В том числе НДС 22%: 8 295,08", "на сумму 46 000,00 руб."]
        total, _, _, _ = find_totals(rows)
        self.assertEqual(total, Decimal("46000.00"))


if __name__ == "__main__":
    unittest.main()
