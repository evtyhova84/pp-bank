"""Счета самозанятых и физлиц, чужие документы в пачке. Правила — PRAVILA-samozanyatye.md.

Строки взяты с настоящих счетов из пачки «немцева» (платформа НПД ФНС,
СберБизнес, Точка) и с OCR-текста скана.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

KOREN = Path(__file__).resolve().parent.parent
if str(KOREN) not in sys.path:
    sys.path.insert(0, str(KOREN))

from parser.fields import (  # noqa: E402
    ORG_WORD_RE,
    SUPPLIER_LABEL_RE,
    cut_address,
    find_accounts,
    find_bank_name,
    find_party_by_label,
    foreign_document,
    recipient_kind,
)

# Платформа НПД ФНС: ФИО без ИП, «Режим НО: НПД», текущий счёт 40817
NPD_ROWS = [
    "Оплату необходимо произвести по реквизитам расчетного счета",
    "Продавец",
    "БУЛОЙЧИК ИВАН АЛЕКСАНДРОВИЧ",
    "Режим НО: НПД",
    "ИНН 390514677445",
    "ФИЛИАЛ № 7806 БАНКА ВТБ (ПАО)",
    "БИК 044030707",
    "Корр. счет 30101810240300000707",
    "Расчетный счет 40817810126906007042",
    "Счёт на оплату №33236992 от 1 сентября 2026 г.",
]

# СберБизнес: счета вразрядку, ИП в две строки, «Счёт №» через ё
SBER_ROWS = [
    "Оплату необходимо произвести до 04 сентября 2026",
    "ВОЛГО-ВЯТСКИЙ БАНК ПАО СБЕРБАНК",
    "БИК 042202603",
    "Банк получателя Счёт № 30101 810 9 0000 0000603",
    "ИНН 595702540208",
    "ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ АРСЕНТЬЕВА",
    "ЕКАТЕРИНА АЛЕКСЕЕВНА",
    "Получатель Счёт № 40802 810 9 4971 0010144",
    "Счёт на оплату № 31 от 01.09.2026",
    "Поставщик: ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ АРСЕНТЬЕВА ЕКАТЕРИНА АЛЕКСЕЕВНА, ИНН",
    "595702540208",
    "Покупатель: Индивидуальный предприниматель Немцева Анна Александровна, ИНН 310603421444",
]


class KtoPoluchatelTest(unittest.TestCase):
    def test_npd_platforma_eto_samozanyatyy(self):
        self.assertEqual(
            recipient_kind("БУЛОЙЧИК ИВАН АЛЕКСАНДРОВИЧ", "390514677445", "40817810126906007042", NPD_ROWS),
            "self_employed",
        )

    def test_ip_s_priznakom_npd_ostayotsya_ip(self):
        """ИП на НПД платят на расчётный счёт как бизнесу, поле 20 ему не нужно."""
        rows = ["ИП Иванов Иван Иванович", "Самозанятый, ИНН 500100732259", "Расчетный счет 40802810200001418054"]
        self.assertEqual(recipient_kind("ИП Иванов Иван Иванович", "500100732259", "40802810200001418054", rows), "ip")
        self.assertEqual(
            recipient_kind("Индивидуальный предприниматель Иванов И. И.", "500100732259", "40802810200001418054", []),
            "ip",
        )

    def test_fizlitso_bez_priznaka_npd(self):
        self.assertEqual(recipient_kind("ИВАНОВ ИВАН ИВАНОВИЧ", "500100732259", "40817810126906007042", []), "person")
        self.assertEqual(recipient_kind("ИВАНОВ ИВАН ИВАНОВИЧ", "500100732259", "", []), "person")

    def test_organizatsiya(self):
        self.assertEqual(recipient_kind('ООО "СМАРТ ЛАЙН"', "7701325465", "40702810123111111114", []), "org")
        self.assertEqual(recipient_kind("Прогресс Парк", "7701325465", "", []), "org", "ИНН из 10 знаков — юрлицо")

    def test_ip_vnutri_familii_ne_marker(self):
        """«АНТИПИНА», «ФИЛИППОВ» — не ИП и не ООО, имя резать нельзя."""
        self.assertIsNone(ORG_WORD_RE.search("АНТИПИНА ЕЛЕНА ВАСИЛЬЕВНА"))
        self.assertIsNone(ORG_WORD_RE.search("ФИЛИППОВ ИППОЛИТ ПАОЛОВИЧ"))
        self.assertEqual(ORG_WORD_RE.search("СЕРГЕЕВА Н. Н. ИП Немцева").group(0), "ИП")
        self.assertEqual(
            recipient_kind("АНТИПИНА ЕЛЕНА ВАСИЛЬЕВНА", "591909773113", "40817810300011553205", ["Режим НО: НПД"]),
            "self_employed",
        )

    def test_pokupatel_ip_v_stroke_s_fio_ne_delaet_prodavtsa_ip(self):
        """Скан без колонок: «СЕРГЕЕВА Н. Н. ИП Немцева А. А.» — ИП здесь покупатель."""
        rows = ["СЕРГЕЕВА НАТАЛЬЯ НИКОЛАЕВНА ИП Немцева Анна Александровна", "Режим НО: НПД инн з [0603421444"]
        self.assertEqual(
            recipient_kind("СЕРГЕЕВА НАТАЛЬЯ НИКОЛАЕВНА ИП Немцева Анна Александровна", "", "40817810300147870810", rows),
            "self_employed",
        )


class SberBiznesTest(unittest.TestCase):
    def test_schet_vrazryadku(self):
        settlement, corr = find_accounts(SBER_ROWS)
        self.assertEqual(settlement, "40802810949710010144")
        self.assertEqual(corr, "30101810900000000603")

    def test_nazvanie_banka_bez_hvosta_ot_schyota(self):
        self.assertEqual(find_bank_name(SBER_ROWS), "ВОЛГО-ВЯТСКИЙ БАНК ПАО СБЕРБАНК")

    def test_postavshchik_s_inn_na_sleduyushchey_stroke(self):
        name, inn = find_party_by_label(SBER_ROWS, SUPPLIER_LABEL_RE)
        self.assertEqual(name, "ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ АРСЕНТЬЕВА ЕКАТЕРИНА АЛЕКСЕЕВНА")
        self.assertEqual(inn, "595702540208")


class HvostSummyVImeniTest(unittest.TestCase):
    def test_summa_scheta_otrezaetsya(self):
        """Точка печатает сумму в одной строке с наименованием получателя."""
        self.assertEqual(cut_address("ИП Сенотрусова Валерия Вячеславовна 191 332,00 ₽"), "ИП Сенотрусова Валерия Вячеславовна")
        self.assertEqual(cut_address("ООО Ромашка 1 500.00 руб."), "ООО Ромашка")

    def test_nomer_v_nazvanii_ne_trogaem(self):
        self.assertEqual(cut_address("ФИЛИАЛ № 7806 БАНКА ВТБ"), "ФИЛИАЛ № 7806 БАНКА ВТБ")
        self.assertEqual(cut_address("ООО Строй 2000"), "ООО Строй 2000")


class ChuzhoyDokumentTest(unittest.TestCase):
    def test_platyozhka(self):
        rows = ["0401060", "04.09.2026 04.09.2026", "Поступ. в банк плат. Списано со сч. плат.",
                "ПЛАТЕЖНОЕ ПОРУЧЕНИЕ № 58 04.09.2026 Электронно"]
        self.assertEqual(foreign_document(rows), "платёжное поручение")

    def test_akt(self):
        self.assertEqual(foreign_document(["Акт № 191 от 31 августа 2026 г.", "Исполнитель: ИП НЕФЕДОВ"]), "акт")

    def test_schyot_ne_chuzhoy(self):
        self.assertEqual(foreign_document(NPD_ROWS), "")
        self.assertEqual(foreign_document(SBER_ROWS), "")


if __name__ == "__main__":
    unittest.main()
