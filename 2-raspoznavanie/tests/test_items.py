"""Табличная часть: наименования позиций для назначения платежа.

Строки собраны по реальным счетам из разных программ (1С, СБИС, Т-Банк),
адреса и названия объектов заменены.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parser.fields import find_buyer_kpp  # noqa: E402
from parser.items import find_items, items_summary  # noqa: E402

HEADER = "№ Товары (работы, услуги) Кол-во Ед. Цена Сумма"


class TestItems(unittest.TestCase):
    def test_several_positions(self):
        rows = [
            "Счет на оплату № 12 от 01.08.2026",
            HEADER,
            "1 Эмаль аэрозольная черная 520мл 10шт 332,01 3 320,10",
            "2 Герметик силиконовый бесцветный 280мл 5шт 272,00 1 360,00",
            "3 Мешок зеленый 55х95см 100шт 12,00 1 200,00",
            "Итого: 5 880,10",
            "Без НДС",
        ]
        self.assertEqual(
            find_items(rows),
            [
                "Эмаль аэрозольная черная 520мл",
                "Герметик силиконовый бесцветный 280мл",
                "Мешок зеленый 55х95см",
            ],
        )

    def test_name_wraps_below_numbers(self):
        """СБИС: хвост наименования печатается строкой ниже сумм."""
        rows = [
            HEADER,
            "изм.",
            "1 Услуги газели 14.07.2026 г перевозка профиля с Лесной 163 на 3 Час 1 400.00 4 200.00",
            "ЖК Речной, Восход 45а",
            "Итого: 4 200.00",
        ]
        self.assertEqual(
            find_items(rows),
            ["Услуги газели 14.07.2026 г перевозка профиля с Лесной 163 на ЖК Речной, Восход 45а"],
        )

    def test_name_above_numbers(self):
        """1С: наименование над строкой с числами, заголовок разъехался по строкам."""
        rows = [
            "Наименование Коли-",
            "№ изме- Цена Сумма",
            "товара чество",
            "рения",
            "1Аренда бытовки строительной р.6х2,4м с",
            "29.06.2026 по 28.07.2026 объект 1771",
            "новая",
            "мес 1 15000,00 15000,00",
            "Итого НДС 22%: 2704,91",
        ]
        self.assertEqual(
            find_items(rows),
            ["Аренда бытовки строительной р.6х2,4м с 29.06.2026 по 28.07.2026 объект 1771 новая"],
        )

    def test_index_glued_to_name_and_garbage_row(self):
        rows = [
            HEADER,
            "1Заправка картриджа Kyocera 3253ci черн.",
            "1шт 3 800,00 3 800,00",
            "2Заправка картриджа Kyocera 3253ci красн. 1шт 3 500,00 3 500,00",
            "3Заправка картриджа Kyocera FS-G8130MFP черн. 1шт 3 600,00",
            "3 600,оо!",
            "4Заправка картриджа Kyocera FS-C8130MFP син. 1шт 3 400,00 3 400,00",
            "Итого: 14 300,00",
        ]
        self.assertEqual(
            find_items(rows),
            [
                "Заправка картриджа Kyocera 3253ci черн.",
                "Заправка картриджа Kyocera 3253ci красн.",
                "Заправка картриджа Kyocera FS-G8130MFP черн.",
                "Заправка картриджа Kyocera FS-C8130MFP син.",
            ],
        )

    def test_house_number_is_not_a_new_position(self):
        """«148 на ЖК…» — продолжение адреса, а не сто сорок восьмая позиция."""
        rows = [
            HEADER,
            "1 Услуги газели 09.07.2026 г перевозка изделий с Мира 2 Час 1 400.00 2 800.00",
            "148 на ЖК Эдельвейс",
            "Итого: 2 800.00",
        ]
        self.assertEqual(
            find_items(rows), ["Услуги газели 09.07.2026 г перевозка изделий с Мира 148 на ЖК Эдельвейс"]
        )

    def test_vat_column_currency_and_units(self):
        rows = [
            "№ Товары (работы, услуги) кол-во Ед. НДС Цена Сумма",
            "Создание вертикального контента (доплата разницы",
            "1 1 усл. ед Без НДС 7 500,00 7 500,00",
            "M-capital)",
            "Всего наименований на сумму 7 500,00 руб.",
        ]
        self.assertEqual(find_items(rows), ["Создание вертикального контента (доплата разницы M-capital)"])
        rows = [HEADER, "1 Маркетинговыеуслуги 1 шт 191 332,00 ₽ Без НДС 191 332,00 ₽", "Итого 191 332,00"]
        self.assertEqual(find_items(rows), ["Маркетинговыеуслуги"])
        rows = [HEADER, "1 Услуги смм 1 57 000,00 57 000,00", "Итого: 57 000,00"]
        self.assertEqual(find_items(rows), ["Услуги смм"])

    def test_model_number_is_not_quantity(self):
        rows = [
            HEADER,
            "1 E09, Щит управления электрический подъемника серии ZLP630 2шт 24 525,00 49 050,00",
            "2 Покраска Заклепка RAL9011 5 000шт 2,00 10 000,00",
            "Итого: 59 050,00",
        ]
        self.assertEqual(
            find_items(rows),
            ["E09, Щит управления электрический подъемника серии ZLP630", "Покраска Заклепка RAL9011"],
        )

    def test_footer_is_dropped(self):
        rows = [
            HEADER,
            "1 Сверло HSS-Co 5.2 mm 100 шт Без НДС 120,00 12 000,00",
            "Создано при помощи business.tbank.ru",
            "Итого: 12 000,00",
        ]
        self.assertEqual(find_items(rows), ["Сверло HSS-Co 5.2 mm"])

    def test_no_table(self):
        self.assertEqual(find_items(["Счет на оплату № 1 от 01.01.2026", "Итого: 100,00"]), [])
        self.assertEqual(items_summary([]), "")

    def test_summary_joins_with_semicolon(self):
        rows = [HEADER, "1 Дюбель-гвоздь 6х40 гриб (шт) 2 200шт 1,04 2 277,00",
                "2 Дюбель-гвоздь 8х80 потай борт 500шт 3,18 1 590,00", "Итого: 3 867,00"]
        self.assertEqual(
            items_summary(rows), "Дюбель-гвоздь 6х40 гриб (шт); Дюбель-гвоздь 8х80 потай борт"
        )


class TestBuyerKpp(unittest.TestCase):
    def test_inline(self):
        rows = ['Покупатель: ООО "ТЕТРАКОМ", ИНН 1661034770, КПП 166101001, Адрес: 420194']
        self.assertEqual(find_buyer_kpp(rows, "1661034770"), "166101001")

    def test_next_row(self):
        rows = ["Покупатель ООО «Ромашка», ИНН 7719617469,", "КПП 771901001, 105064, Москва"]
        self.assertEqual(find_buyer_kpp(rows, "7719617469"), "771901001")

    def test_bare_values(self):
        rows = ["ИНН КПП", "7719617469 771901001"]
        self.assertEqual(find_buyer_kpp(rows, "7719617469"), "771901001")

    def test_supplier_kpp_is_not_taken(self):
        rows = [
            'Поставщик: ООО "СМАРТ ЛАЙН", ИНН 1686001984, КПП 168601001,',
            "Покупатель: ИП Иванов И.И., ИНН 166100000000",
        ]
        self.assertEqual(find_buyer_kpp(rows, "166100000000"), "")
        self.assertEqual(find_buyer_kpp(rows, ""), "")


if __name__ == "__main__":
    unittest.main()


# --- сканы: искажённые метки, потерянные знаки, город банка ---

from parser.fields import (  # noqa: E402
    BUYER_LABEL_RE as _BUYER,
    SUPPLIER_LABEL_RE as _SUPPLIER,
    find_bank_name as _find_bank_name,
    find_name_after_label as _find_name_after_label,
    find_party_by_label as _find_party_by_label,
    find_title as _find_title,
    split_bank_city,
)
from parser.ocr_repair import repair_row as _repair_row  # noqa: E402


class TestSkanShapka(unittest.TestCase):
    def test_title_without_number_sign(self):
        """Со скана «№» теряется: «Счет на оплату 172 от 28 марта 2026 г.»."""
        _, number, when = _find_title(["Счет на оплату 172 от 28 марта 2026 г."])
        self.assertEqual((number, when), ("172", "28 марта 2026"))
        self.assertIsNone(_find_title(["Счет на оплату от 28 марта 2026 г."]))

    def test_garbled_party_labels(self):
        rows = ['Покупатеть ООО "ПОСЛЕДНЯЯ МИЛЯ", ИНН 5029282981, КПП 502901001, 141033']
        self.assertEqual(_find_party_by_label(rows, _BUYER)[1], "5029282981")
        rows = ['Покупатить ООО "ПОСЛЕДНЯЯ МИЛЯ", ИНН 5029282981']
        self.assertEqual(_find_party_by_label(rows, _BUYER)[1], "5029282981")
        rows = ['Поставщик. ООО "СУБ-Строй", ИНН 7806521139, КПП 780601001']
        self.assertEqual(_find_party_by_label(rows, _SUPPLIER), ('ООО "СУБ-Строй"', "7806521139"))

    def test_supplier_name_survives_broken_inn(self):
        """ИНН в строке «Поставщик» прочитался с ошибкой — название всё равно берём."""
        rows = ['Поставщик ООО ТОПАЗ“ ИНН 701777545, КПП 781101001, 192029, Город Санкт-Петербург,']
        self.assertEqual(_find_party_by_label(rows, _SUPPLIER), ("ООО ТОПАЗ“", ""))
        self.assertEqual(_find_party_by_label(["Поставщик (получатель платежа)"], _SUPPLIER), ("", ""))

    def test_address_row_in_brackets_is_not_a_name(self):
        rows = [
            "Поставщик ИНН 7723905004, КПП 772101001, 111674, город",
            "(Иттолнитель): Москва, пр-кт Защитников Москвы, дом 10",
        ]
        self.assertEqual(_find_name_after_label(rows), "")

    def test_ooo_as_zeros_is_repaired_only_before_name(self):
        self.assertEqual(_repair_row('000 “СИГМА-ОПТ“'), 'ООО “СИГМА-ОПТ“')
        self.assertEqual(_repair_row("000 СЕРВИСТРАНСАВТО"), "ООО СЕРВИСТРАНСАВТО")
        self.assertEqual(_repair_row("Итого: 1 000 Штук"), "Итого: 1 000 Штук")
        self.assertEqual(_repair_row("Счет на оплату N9 172 от 28 марта"), "Счет на оплату № 172 от 28 марта")

    def test_bank_label_and_wrapped_city(self):
        rows = [
            "АРХАНГЕЛЬСКОЕ ОТДЕЛЕНИЕ N 8637 ПАО СБЕРБАНК г. БИК 041117601",
            "Архангельск",
            "Сч. 30101810100000000601",
            "Банк",
            "ИНН 5321200770 КПП 532101001 Сч. № 40702810543000004155",
            'ООО "СИГМА-ОПТ"',
            "Счет на оплату № 317 от 18 апреля 2026 г.",
        ]
        self.assertEqual(
            _find_bank_name(rows), "АРХАНГЕЛЬСКОЕ ОТДЕЛЕНИЕ N 8637 ПАО СБЕРБАНК г. Архангельск"
        )
        rows = ['ООО «Банк Точка» г. Москва БИК 044525104', "Банк п теля", "Счет на оплату № 1 от 01.01.2026"]
        self.assertEqual(_find_bank_name(rows), "ООО «Банк Точка» г. Москва")

    def test_split_bank_city(self):
        self.assertEqual(
            split_bank_city("АРХАНГЕЛЬСКОЕ ОТДЕЛЕНИЕ N 8637 ПАО СБЕРБАНК г. Архангельск"),
            ("АРХАНГЕЛЬСКОЕ ОТДЕЛЕНИЕ N 8637 ПАО СБЕРБАНК", "г. Архангельск"),
        )
        self.assertEqual(
            split_bank_city('ФИЛИАЛ "ЦЕНТРАЛЬНЫЙ" БАНКА ВТБ (ПАО) г. Москва'),
            ('ФИЛИАЛ "ЦЕНТРАЛЬНЫЙ" БАНКА ВТБ (ПАО)', "г. Москва"),
        )
        self.assertEqual(split_bank_city("АО ОТП БАНК"), ("АО ОТП БАНК", ""))
        self.assertEqual(split_bank_city("г. Москва"), ("г. Москва", ""))
