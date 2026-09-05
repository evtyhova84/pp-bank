"""Чтение реквизитов плательщика из старой платёжки."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

KOREN = Path(__file__).resolve().parent.parent.parent
if str(KOREN) not in sys.path:
    sys.path.insert(0, str(KOREN))

from servis.app import iz_platezhki, yadro  # noqa: E402,F401  (yadro подключает ядро к пути)

# Так выглядит платёжное поручение формы 0401060, когда из него вынули текст:
# подпись клетки идёт ПОД её содержимым, а реквизиты получателя — ниже плательщика.
PLATEZHKA = [
    "Поступ. в банк плат. Списано со сч. плат.",
    "ПЛАТЕЖНОЕ ПОРУЧЕНИЕ № 41 12.08.2026 Электронно",
    "Дата Вид платежа",
    "Сумма прописью Одна тысяча рублей 00 копеек",
    "ИНН 7719617469 КПП 771901001 Сумма 1000-00",
    'ООО "Ромашка"',
    "Сч. № 40702810300180001774",
    "Плательщик",
    "АО ОТП БАНК Г. МОСКВА",
    "БИК 044525311",
    "Сч. № 30101810000000000311",
    "Банк плательщика",
    "ПАО БАНК ПЕТРОКОММЕРЦ Г. МОСКВА",
    "БИК 044525352",
    "Сч. № 30101810700000000352",
    "Банк получателя",
    "ИНН 7701325465 КПП 770101001 Сч. № 40702810123111111114",
    "ООО Прогресс Парк",
    "Получатель",
]


class RazborDokumenta:
    """Разбор платёжки-документа. Сам файл не нужен: подменяем чтение строк."""

    def razobrat(self, stroki: list[str]) -> dict:
        from parser import rows

        bylo = rows.read_rows
        rows.read_rows = lambda put: stroki
        try:
            return iz_platezhki.iz_dokumenta("platezhka.pdf")
        finally:
            rows.read_rows = bylo


class DokumentTest(RazborDokumenta, unittest.TestCase):
    """Платёжка юридического лица."""

    def test_berutsya_rekvizity_platelshchika_a_ne_poluchatelya(self):
        itog = self.razobrat(PLATEZHKA)
        self.assertEqual(itog["inn"], "7719617469")
        self.assertEqual(itog["kpp"], "771901001")
        self.assertEqual(itog["account"], "40702810300180001774")
        self.assertEqual(itog["bic"], "044525311")
        self.assertEqual(itog["corr_account"], "30101810000000000311")

    def test_naimenovanie_i_bank_iz_svoih_kletok(self):
        itog = self.razobrat(PLATEZHKA)
        self.assertEqual(itog["name"], 'ООО "Ромашка"')
        # В файле для банка название и город — разные поля, Банк1 и Банк2
        self.assertEqual(itog["bank_name"], "АО ОТП БАНК")
        self.assertEqual(itog["bank_city"], "Г. МОСКВА")

    def test_ne_platezhka_ne_vydumyvaet_rekvizity(self):
        with self.assertRaises(iz_platezhki.NeRazobrano):
            self.razobrat(["Акт выполненных работ", "без всяких реквизитов"])

    def test_rekvizity_prohodyat_kontrolnye_summy(self):
        """Прочитанное должно сходиться так же, как сходится в банке."""
        from generator.validate import check_account, check_bic, check_inn

        itog = self.razobrat(PLATEZHKA)
        self.assertTrue(check_inn(itog["inn"]))
        self.assertTrue(check_bic(itog["bic"]))
        self.assertTrue(check_account(itog["account"], itog["bic"]))
        self.assertTrue(check_account(itog["corr_account"], itog["bic"], corr=True))


# Настоящая платёжка ИП: ИНН из двенадцати знаков, КПП напечатан нулём,
# наименование не влезло в строку, а название банка стоит в одной строке с БИК.
# На ней разбор спотыкался трижды, поэтому она осталась в тестах.
PLATEZHKA_IP = [
    "0401060",
    "27.08.2026 27.08.2026",
    "Поступ. в банк плат. Списано со сч. плат.",
    "ПЛАТЕЖНОЕ ПОРУЧЕНИЕ № 88 27.08.2026 Электронно",
    "Дата Вид платежа",
    "Сумма прописью Двадцать две тысячи триста двадцать три рубля 00 копеек",
    "ИНН 616125855326 КПП 0 Сумма 22323-00",
    "Индивидуальный предприниматель Шульмина Надежда",
    "Алексеевна",
    "Сч. № 40802810003500013642",
    "Плательщик",
    'ООО "Банк Точка" г. Москва БИК 044525104',
    "Сч. № 30101810745374525104",
    "Банк плательщика",
    'Филиал "Центральный" Банка ВТБ (ПАО) г. Москва БИК 044525411',
    "Сч. № 30101810145250000411",
    "Банк получателя",
    "ИНН 7716509296 КПП 772701001 Сч. № 40702810801010000677",
    "ЭР СОФТ, ООО",
    "Получатель",
]


class PlatezhkaIPTest(RazborDokumenta, unittest.TestCase):
    """Платёжка индивидуального предпринимателя."""

    def test_inn_iz_dvenadtsati_znakov_ne_obrezaetsya(self):
        """У ООО ИНН из десяти знаков, у ИП и физлица — из двенадцати."""
        itog = self.razobrat(PLATEZHKA_IP)
        self.assertEqual(itog["inn"], "616125855326")

    def test_kpp_nolyom_eto_otsutstvie_kpp(self):
        """У ИП КПП нет; в форме печатается «0», но это не значение."""
        itog = self.razobrat(PLATEZHKA_IP)
        self.assertEqual(itog["kpp"], "")

    def test_naimenovanie_sobiraetsya_iz_dvuh_strok(self):
        itog = self.razobrat(PLATEZHKA_IP)
        self.assertEqual(
            itog["full_name"],
            "Индивидуальный предприниматель Шульмина Надежда Алексеевна",
        )
        self.assertEqual(itog["name"], "ИП Шульмина Надежда Алексеевна")

    def test_bank_otdelyaetsya_ot_bika_i_goroda(self):
        itog = self.razobrat(PLATEZHKA_IP)
        self.assertEqual(itog["bank_name"], 'ООО "Банк Точка"')
        self.assertEqual(itog["bank_city"], "г. Москва")

    def test_scheta_platelshchika_a_ne_poluchatelya(self):
        itog = self.razobrat(PLATEZHKA_IP)
        self.assertEqual(itog["account"], "40802810003500013642")
        self.assertEqual(itog["corr_account"], "30101810745374525104")
        self.assertEqual(itog["bic"], "044525104")

    def test_slipshiesya_kletki_ne_popadayut_v_nazvanie(self):
        """Соседняя клетка на той же высоте приезжает в ту же строку.

        Насколько слипнется — зависит от вёрстки конкретного банка, поэтому
        хвосты срезаются по меткам, а не по расстоянию.
        """
        slipshiesya = [
            "ИНН 616125855326 КПП 0 Сумма 22323-00",
            "Индивидуальный предприниматель Шульмина Надежда",
            "Алексеевна Сч. № 40802810003500013642",
            "Плательщик",
            'ООО "Банк Точка" г. Москва БИК 044525104 Сч. № 30101810745374525104',
            "Банк плательщика",
            "Банк получателя",
            "ЭР СОФТ, ООО",
            "Получатель",
        ]
        itog = self.razobrat(slipshiesya)
        self.assertEqual(itog["name"], "ИП Шульмина Надежда Алексеевна")
        self.assertEqual(itog["bank_name"], 'ООО "Банк Точка"')
        self.assertEqual(itog["account"], "40802810003500013642")

    def test_forma_prinimaet_ip_bez_kpp(self):
        from servis.app import spravochniki

        itog = self.razobrat(PLATEZHKA_IP)
        oshibki = spravochniki._proverit({**itog, "code": "shulmina"})
        self.assertEqual(oshibki, [], "ИП с пустым КПП должен проходить проверку")


# Та же платёжка, но номера напечатаны вразрядку — так их верстают, чтобы
# попасть в клетки бланка. При чтении цифры разъезжаются по ячейкам и
# склеиваются через пробел: «616 125 855 326». Из-за этого не находилось
# вообще ни одного ИНН.
PLATEZHKA_VRAZRYADKU = [
    "0401060",
    "27.08.2026 27.08.2026",
    "ПЛАТЕЖНОЕ ПОРУЧЕНИЕ № 88 27.08.2026 Электронно",
    "ИНН 616 125 855 326 КПП 0 Сумма 22323-00",
    "Индивидуальный предприниматель Шульмина Надежда",
    "Алексеевна",
    "Сч. № 4080 2810 0035 0001 3642",
    "Плательщик",
    'ООО "Банк Точка" г. Москва БИК 044 525 104',
    "Сч. № 3010 1810 7453 7452 5104",
    "Банк плательщика",
    "Банк получателя",
    "ЭР СОФТ, ООО",
    "Получатель",
]


class VrazryadkuTest(RazborDokumenta, unittest.TestCase):
    """Номера, напечатанные вразрядку."""

    def test_chisla_s_probelami_vnutri_chitayutsya(self):
        itog = self.razobrat(PLATEZHKA_VRAZRYADKU)
        self.assertEqual(itog["inn"], "616125855326")
        self.assertEqual(itog["account"], "40802810003500013642")
        self.assertEqual(itog["bic"], "044525104")
        self.assertEqual(itog["corr_account"], "30101810745374525104")

    def test_prochitannoe_shoditsya_po_kontrolnym_summam(self):
        from generator.validate import check_account, check_bic, check_inn

        itog = self.razobrat(PLATEZHKA_VRAZRYADKU)
        self.assertTrue(check_inn(itog["inn"]))
        self.assertTrue(check_bic(itog["bic"]))
        self.assertTrue(check_account(itog["account"], itog["bic"]))
        self.assertTrue(check_account(itog["corr_account"], itog["bic"], corr=True))


# Платёжка ООО «ТЕТРАКОМ» из Точки: подписи приклеены к числам без пробела
# («ИНН1661034770», «КПП166101001»), а «Сумма прописью» напечатана двумя
# отдельными строками. В наименование заезжало «прописью ИНН1661034770 КПП ООО…»,
# а КПП не находился вовсе — граница слова между буквой и цифрой не стоит.
PLATEZHKA_PRIKLEENNYE_PODPISI = [
    "0401060",
    "27.08.2026 27.08.2026",
    "ПЛАТЕЖНОЕ ПОРУЧЕНИЕ № 12 27.08.2026 Электронно",
    "Сумма",
    "прописью",
    "Двенадцать тысяч рублей 00 копеек",
    "ИНН1661034770 КПП166101001 Сумма 12000-00",
    'ООО "ТЕТРАКОМ"',
    "Сч. № 40702810220000116689",
    "Плательщик",
    'ООО "Банк Точка" г. Москва БИК044525104',
    "Сч. № 30101810745374525104",
    "Банк плательщика",
    "Банк получателя",
    "ЭР СОФТ, ООО",
    "Получатель",
]


class PrikleennyePodpisiTest(RazborDokumenta, unittest.TestCase):
    """Подписи без пробела перед числом."""

    def test_naimenovanie_bez_obryvkov_sosednih_kletok(self):
        itog = self.razobrat(PLATEZHKA_PRIKLEENNYE_PODPISI)
        self.assertEqual(itog["name"], 'ООО "ТЕТРАКОМ"')
        self.assertEqual(itog["full_name"], 'ООО "ТЕТРАКОМ"')

    def test_kpp_prikleenny_k_podpisi_chitaetsya(self):
        itog = self.razobrat(PLATEZHKA_PRIKLEENNYE_PODPISI)
        self.assertEqual(itog["inn"], "1661034770")
        self.assertEqual(itog["kpp"], "166101001")

    def test_bik_prikleenny_k_podpisi_chitaetsya(self):
        itog = self.razobrat(PLATEZHKA_PRIKLEENNYE_PODPISI)
        self.assertEqual(itog["bic"], "044525104")
        self.assertEqual(itog["bank_name"], 'ООО "Банк Точка"')

    def test_kod_podskazyvaetsya_iz_nastoyashchego_imeni(self):
        itog = self.razobrat(PLATEZHKA_PRIKLEENNYE_PODPISI)
        self.assertEqual(iz_platezhki.kod_iz_imeni(itog["name"]), "tetrakom")


# Платёжка, которой заплатили НАМ: её выгрузил наш банк как подтверждение
# поступления. Плательщик — чужая организация, наша — получатель. Внизу отметки
# банка, выдавшего документ: его БИК совпадает с банком получателя.
# Так выглядят платёжки из Альфа-Банка; реквизиты здесь вымышленные.
PLATEZHKA_VHODYASHCHAYA = [
    "04.09.2026 04.09.2026 0401060",
    "Поступ. в банк плат. Списано со сч. плат.",
    "ПЛАТЁЖНОЕ ПОРУЧЕНИЕ 312 04.09.2026 электронно",
    "Дата Вид платежа",
    "Сумма Одна тысяча рублей 00 копеек",
    "прописью",
    "Сумма 1000-00",
    "ИНН 7719617469 КПП 771901001",
    'ООО "Ромашка"',
    "Сч. № 40702810300180001774",
    "Плательщик",
    "АО ОТП БАНК г БИК 044525311",
    "Москва",
    "Сч. № 30101810000000000311",
    "Банк плательщика",
    "ПАО БАНК ПЕТРОКОММЕРЦ г БИК 044525352",
    "Москва",
    "Сч. № 30101810700000000352",
    "Банк получателя",
    "Сч. № 40702810123111111114",
    "ИНН 7701325465 КПП 770101001",
    "ООО Прогресс Парк",
    "Вид оп. 01 Срок плат.",
    "Очер. 5",
    "Наз. пл.",
    "Код Рез.поле",
    "Получатель",
    "ОПЛАТА ПО СЧЕТУ №910 ОТ 24.08.2026 Г. ЗА ТРАНСПОРТНЫЕ УСЛУГИ В ТОМ ЧИСЛЕ НДС 22 % - 180.33 РУБЛЕЙ.",
    "Назначение платежа",
    "Подписи ПАО «БАНК ПЕТРОКОММЕРЦ»",
    "к\\сч 30101810700000000352 в ГУ Банка России",
    "М.П.",
    "БИК 044525352 ИНН 7705123452",
    "ИСПОЛНЕНО",
]


class DveStoronyTest(RazborDokumenta, unittest.TestCase):
    """В платёжке две стороны; какая наша — решают признаки, а не порядок в бланке."""

    def test_vhodyashchaya_platezhka_beryot_poluchatelya(self):
        """Документ выдан банком получателя — значит деньги получили мы."""
        itog = self.razobrat(PLATEZHKA_VHODYASHCHAYA)
        self.assertEqual(itog["_storona"], "получатель")
        self.assertEqual(itog["inn"], "7701325465")
        self.assertEqual(itog["kpp"], "770101001")
        self.assertEqual(itog["account"], "40702810123111111114")
        self.assertEqual(itog["bic"], "044525352")
        self.assertEqual(itog["corr_account"], "30101810700000000352")
        self.assertEqual(itog["bank_name"], "ПАО БАНК ПЕТРОКОММЕРЦ")
        self.assertEqual(itog["bank_city"], "г Москва")
        self.assertEqual(itog["name"], "ООО Прогресс Парк", "подписи клеток попали в название")
        self.assertIn("выдан банком получателя", itog["_pochemu"])

    def test_vtoraya_storona_dostupna_dlya_perekluchenia(self):
        drugaya = self.razobrat(PLATEZHKA_VHODYASHCHAYA)["_drugaya_storona"]
        self.assertEqual(drugaya["_storona"], "плательщик")
        self.assertEqual(drugaya["inn"], "7719617469")
        self.assertEqual(drugaya["account"], "40702810300180001774")
        self.assertEqual(drugaya["bic"], "044525311")
        self.assertEqual(drugaya["name"], 'ООО "Ромашка"')

    def test_inn_banka_iz_shtampa_ne_beryotsya(self):
        """В штампе внизу — ИНН и БИК самого банка. Это не реквизиты сторон."""
        itog = self.razobrat(PLATEZHKA_VHODYASHCHAYA)
        self.assertNotEqual(itog["inn"], "7705123452")
        self.assertNotEqual(itog["_drugaya_storona"]["inn"], "7705123452")

    def test_bez_otmetok_banka_beryotsya_platelshchik(self):
        """Признаков нет — обычный случай: наша платёжка, мы плательщик."""
        bez_podvala = PLATEZHKA_VHODYASHCHAYA[: PLATEZHKA_VHODYASHCHAYA.index("Подписи ПАО «БАНК ПЕТРОКОММЕРЦ»")]
        itog = self.razobrat(bez_podvala)
        self.assertEqual(itog["_storona"], "плательщик")
        self.assertEqual(itog["inn"], "7719617469")
        self.assertEqual(itog["_drugaya_storona"]["inn"], "7701325465")

    def test_shtamp_banka_platelshchika_ostavlyaet_platelshchika(self):
        so_shtampom_otp = [
            s.replace("044525352", "044525311").replace("30101810700000000352 в ГУ", "30101810000000000311 в ГУ")
            if "Подписи" in s or "к\\сч" in s or s.startswith("БИК") else s
            for s in PLATEZHKA_VHODYASHCHAYA
        ]
        itog = self.razobrat(so_shtampom_otp)
        self.assertEqual(itog["_storona"], "плательщик")
        self.assertEqual(itog["inn"], "7719617469")

    def test_forma_sobstvennosti_v_kontse_nazvaniya_ne_rezhet_ego(self):
        itog = self.razobrat(PLATEZHKA)
        self.assertEqual(itog["_drugaya_storona"]["name"], "ООО Прогресс Парк")
        s_formoy_v_kontse = [s.replace("ООО Прогресс Парк", "ЭР СОФТ, ООО") for s in PLATEZHKA]
        itog = self.razobrat(s_formoy_v_kontse)
        self.assertEqual(itog["_drugaya_storona"]["name"], "ЭР СОФТ, ООО")


class OpoznanieTest(unittest.TestCase):
    """Опознание реквизитов по контрольным суммам, без опоры на подписи."""

    TEXT = (
        "6 1 6 1 2 5 8 5 5 3 2 6 0 Сумма 22323-00\n"
        "ИНН КПП\n"
        "4 0 8 0 2 8 1 0 0 0 3 5 0 0 0 1 3 6 4 2\n"
        "Сч. №\n"
        'ООО "Банк Точка" г. Москва 0 4 4 5 2 5 1 0 4\n'
        "БИК\n"
        "3 0 1 0 1 8 1 0 7 4 5 3 7 4 5 2 5 1 0 4\n"
        "Сч. №\n"
    )

    def test_inn_slipshiysya_s_kpp_vsyo_ravno_opoznayotsya(self):
        """ИНН и КПП стоят вплотную: 616125855326 + 0 = тринадцать цифр."""
        itog = iz_platezhki.po_kontrolnym_summam(self.TEXT)
        self.assertEqual(itog["inn"], "616125855326")

    def test_lozhny_bik_ne_pobezhdaet(self):
        """Первые девять цифр ИНН проходят проверку БИК — но счета под ними не сходятся.

        Проверка БИК слабая, поэтому одинокого совпадения мало: настоящий БИК
        тот, под которым сходятся ключи расчётного и корр. счетов.
        """
        from generator.validate import check_bic

        self.assertTrue(check_bic("616125855"), "проверка БИК перестала быть слабой")
        itog = iz_platezhki.po_kontrolnym_summam(self.TEXT)
        self.assertEqual(itog["bic"], "044525104")
        self.assertEqual(itog["account"], "40802810003500013642")
        self.assertEqual(itog["corr_account"], "30101810745374525104")

    def test_nomer_vrazryadku_ne_ostayotsya_v_nazvanii_banka(self):
        """Подпись «БИК» уехала в другую строку, и цифры прилипли к банку."""
        self.assertEqual(
            iz_platezhki._bez_hvostov('ООО "Банк Точка" г. Москва 0 4 4 5 2 5 1 0 4'),
            'ООО "Банк Точка" г. Москва',
        )


class DiagnostikaTest(unittest.TestCase):
    """Отказ должен объяснять, что программа увидела.

    Проверяем последнюю попытку разбора — чтение как счёта: до неё доходит
    всё, что не оказалось ни файлом обмена, ни платёжкой.
    """

    def razobrat(self, stroki: list[str]) -> dict:
        from parser import parse as parse_modul
        from parser import rows

        class _Pusto:
            data: dict = {}

        bylo_rows, bylo_parse = rows.read_rows, parse_modul.parse_invoice
        rows.read_rows = lambda put, dvizhok=None: stroki
        parse_modul.parse_invoice = lambda put, **kw: _Pusto()
        try:
            return iz_platezhki.iz_scheta("x.pdf")
        finally:
            rows.read_rows, parse_modul.parse_invoice = bylo_rows, bylo_parse

    def test_govorit_chto_prochital(self):
        with self.assertRaises(iz_platezhki.NeRazobrano) as poymano:
            self.razobrat(["Справка о расчётах", "Сальдо на 01.09.2026 — 15000 руб"])
        soobshchenie = str(poymano.exception)
        self.assertIn("прочитано строк: 2", soobshchenie)
        self.assertIn("Справка о расчётах", soobshchenie)
        self.assertEqual(len(poymano.exception.stroki), 2, "строки не приложены к отказу")

    def test_pustoy_pdf_sovetuet_kartinku(self):
        with self.assertRaises(iz_platezhki.NeRazobrano) as poymano:
            self.razobrat(["", "  "])
        self.assertIn("скан", str(poymano.exception))
        self.assertIn("JPG", str(poymano.exception))


SCHET = [
    "Счет на оплату № 1669 от 20 июля 2026 г.",
    'Поставщик: ООО "Омега", ИНН 1660250400, КПП 166001001, г. Казань',
    'Покупатель (Заказчик): ООО "ТЕТРАКОМ", ИНН 1661034770, КПП 166101001, г. Казань',
    'АО "ТБанк" г. Москва БИК 044525974',
    "Сч. № 30101810145250000974",
    "ИНН 1660250400 КПП 166001001 Сч. № 40702810410000318304",
    "Итого: 22562.08",
    "В том числе НДС 22%: 4068.57",
    "Всего к оплате: 22562.08",
]


class SchetTest(unittest.TestCase):
    """Из счёта видно, кто платит, но не с какого счёта."""

    def razobrat(self) -> dict:
        from parser import parse as parse_modul
        from parser import rows

        class _Razobrano:
            data = {"_buyer_inn": "1661034770", "_buyer_name": 'ООО "ТЕТРАКОМ"'}

        bylo_rows, bylo_parse = rows.read_rows, parse_modul.parse_invoice
        rows.read_rows = lambda put, dvizhok=None: SCHET
        parse_modul.parse_invoice = lambda put, **kw: _Razobrano()
        try:
            return iz_platezhki.iz_scheta("schet.pdf")
        finally:
            rows.read_rows, parse_modul.parse_invoice = bylo_rows, bylo_parse

    def test_beryotsya_pokupatel_a_ne_postavshchik(self):
        """Плательщик — это покупатель. Поставщик получает деньги, а не платит."""
        itog = self.razobrat()
        self.assertEqual(itog["inn"], "1661034770")
        self.assertEqual(itog["kpp"], "166101001")
        self.assertEqual(itog["name"], 'ООО "ТЕТРАКОМ"')

    def test_bankovskie_rekvizity_ne_vydumyvayutsya(self):
        """В счёте напечатан счёт ПОСТАВЩИКА — записать его плательщику значит
        отправить деньги самому себе, поэтому эти поля остаются пустыми."""
        itog = self.razobrat()
        for pole in ("account", "bic", "corr_account", "bank_name"):
            self.assertEqual(itog[pole], "", f"поле {pole} взято из чужих реквизитов")
        self.assertEqual(
            iz_platezhki.chego_ne_hvataet(itog),
            ["расчётный счёт", "банк", "БИК", "корр. счёт"],
        )


class ObmenTest(unittest.TestCase):
    """Разбор файла обмена с клиент-банком."""

    def fayl(self, *dopolnitelno: str) -> bytes:
        stroki = [
            "1CClientBankExchange",
            "ВерсияФормата=1.02",
            "РасчСчет=40702810300180001774",
            "СекцияДокумент=Платежное поручение",
            "ПлательщикСчет=40702810300180001774",
            "ПлательщикИНН=7719617469",
            'Плательщик1=Общество с ограниченной ответственностью "Ромашка"',
            "ПлательщикКПП=771901001",
            "ПлательщикБанк1=АО ОТП БАНК",
            "ПлательщикБанк2=Г. МОСКВА",
            "ПлательщикБИК=044525311",
            "ПлательщикКорсчет=30101810000000000311",
            "ПолучательСчет=40702810123111111114",
            "ПолучательИНН=7701325465",
            *dopolnitelno,
            "КонецДокумента",
            "КонецФайла",
        ]
        return "\r\n".join(stroki).encode("cp1251")

    def test_chitaet_svoyu_storonu(self):
        itog = iz_platezhki.iz_obmena(self.fayl())
        self.assertEqual(itog["inn"], "7719617469")
        self.assertEqual(itog["account"], "40702810300180001774")
        self.assertEqual(itog["bic"], "044525311")
        self.assertEqual(itog["bank_city"], "Г. МОСКВА")
        self.assertEqual(itog["name"], 'ООО "Ромашка"')

    def test_v_vypiske_svoyu_storonu_uznayot_po_schetu(self):
        """В выписке наша организация бывает получателем — берём её, а не плательщика."""
        stroki = [
            "1CClientBankExchange",
            "РасчСчет=40702810123111111114",
            "СекцияДокумент=Платежное поручение",
            "ПлательщикСчет=40702810300180001774",
            "ПлательщикИНН=7719617469",
            "ПолучательСчет=40702810123111111114",
            "ПолучательИНН=7701325465",
            "Получатель1=ООО Прогресс Парк",
            "ПолучательБанк1=ПАО БАНК ПЕТРОКОММЕРЦ",
            "ПолучательБИК=044525352",
            "ПолучательКорсчет=30101810700000000352",
            "КонецДокумента",
        ]
        itog = iz_platezhki.iz_obmena("\r\n".join(stroki).encode("cp1251"))
        self.assertEqual(itog["inn"], "7701325465")
        self.assertEqual(itog["account"], "40702810123111111114")

    def test_chuzhoy_fayl_otvergaetsya(self):
        with self.assertRaises(iz_platezhki.NeRazobrano):
            iz_platezhki.iz_obmena("какой-то текст".encode("cp1251"))

    def test_kod_podskazyvaetsya_latinicey(self):
        self.assertEqual(iz_platezhki.kod_iz_imeni('ООО "Ромашка"'), "romashka")
        self.assertEqual(iz_platezhki.kod_iz_imeni("ООО ТЕТРАКОМ"), "tetrakom")


if __name__ == "__main__":
    unittest.main()
