"""Склейка полей из нескольких движков распознавания.

Движки ошибаются по-разному. На фото счёта встроенный движок Windows потерял
клетку с расчётным счётом и БИК, а Tesseract прочитал их, но испортил ИНН.
Порознь ни один разбор не сходится — вместе сходятся оба.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

KOREN = Path(__file__).resolve().parent.parent.parent
if str(KOREN) not in sys.path:
    sys.path.insert(0, str(KOREN))

from servis.app import razbor  # noqa: E402

# Реквизиты вымышленные, контрольные суммы сходятся (те же, что в test_servis)
POLNY = {
    "invoice_number": "317",
    "invoice_date": "2026-04-18",
    "recipient_name": "ООО Прогресс Парк",
    "recipient_full": 'Общество с ограниченной ответственностью "Прогресс Парк"',
    "recipient_inn": "7701325465",
    "recipient_kpp": "770101001",
    "recipient_account": "40702810123111111114",
    "recipient_bank_name": "ПАО БАНК ПЕТРОКОММЕРЦ",
    "recipient_bank_city": "Г. МОСКВА",
    "recipient_bic": "044525352",
    "recipient_corr_account": "30101810700000000352",
    "total_amount": "54000.00",
    "vat_rate": "22",
    "vat_amount": "9737.70",
    "vat_status": "included",
    "payment_description": "Транспортные услуги по маршруту",
    "_buyer_inn": "7719617469",
    "_buyer_name": "ООО Ромашка",
    "_buyer_kpp": "771901001",
    "_name_unreliable": False,
}


def kandidat(dvizhok: str, data: dict, iz_parsera: list[str]) -> razbor.Razobrano:
    return razbor.Razobrano(
        fayl="foto.jpg",
        data=data,
        problems=iz_parsera + razbor.proverit_summy(data),
        dvizhok=dvizhok,
        iz_parsera=list(iz_parsera),
    )


class SkleykaTest(unittest.TestCase):
    def test_polya_dobirayutsya_iz_vtorogo_dvizhka(self):
        # Windows: нет счёта, БИК и корр. счёта, «№» в заголовке потерян
        windows = kandidat(
            "windows",
            {**POLNY, "recipient_account": "", "recipient_bic": "", "recipient_corr_account": "",
             "invoice_number": "", "invoice_date": "", "recipient_kpp": ""},
            ["не найдена строка «Счёт на оплату № … от …»", "не найден расчётный счёт получателя",
             "не найден корреспондентский счёт", "не найден БИК"],
        )
        # Tesseract: реквизиты банка есть, но ИНН с ошибкой и без покупателя
        tesseract = kandidat(
            "tesseract",
            {**POLNY, "recipient_inn": "7701325466", "_buyer_inn": "", "_buyer_name": ""},
            [],
        )
        itog = razbor.skleit([windows, tesseract], payer_inn="7719617469")
        self.assertEqual(itog.problems, [], itog.problems)
        self.assertEqual(itog.data["recipient_inn"], "7701325465")
        self.assertEqual(itog.data["recipient_account"], "40702810123111111114")
        self.assertEqual(itog.data["recipient_bic"], "044525352")
        self.assertEqual(itog.data["recipient_kpp"], "770101001")
        self.assertEqual(itog.data["invoice_number"], "317")
        self.assertEqual(itog.data["_buyer_inn"], "7719617469", "покупателя из основы нельзя терять")
        self.assertIn("+", itog.dvizhok)
        # За основу берётся разбор с меньшим числом замечаний — здесь tesseract,
        # а покупатель и ИНН добираются из windows
        self.assertTrue(any(n.startswith("из движка") for n in itog.notes), itog.notes)

    def test_krivoe_iz_vtorogo_ne_beryotsya(self):
        """Второй движок предлагает счёт, не проходящий ключ, — он не подставляется."""
        osnova = kandidat(
            "windows", {**POLNY, "recipient_account": ""}, ["не найден расчётный счёт получателя"]
        )
        # у второго счёт с ошибкой в ключе, а ещё нет корр. счёта и банка —
        # замечаний у него больше, основой остаётся windows
        drugoy = kandidat(
            "tesseract",
            {**POLNY, "recipient_account": "40702810123111111115", "recipient_corr_account": "",
             "recipient_bank_name": ""},
            ["не найден корреспондентский счёт", "не найдено наименование банка получателя"],
        )
        itog = razbor.skleit([osnova, drugoy])
        self.assertEqual(itog.data["recipient_account"], "")
        self.assertIn("не найден расчётный счёт получателя", itog.problems)
        self.assertEqual(itog.data["recipient_corr_account"], POLNY["recipient_corr_account"])

    def test_chuzhoy_pokupatel_iz_vtorogo_dvizhka_ne_teryaetsya(self):
        """Основа покупателя не прочитала, второй прочитал чужой ИНН — замечание появляется."""
        osnova = kandidat(
            "windows",
            {**POLNY, "recipient_kpp": "", "recipient_bank_name": "", "_buyer_inn": "",
             "_buyer_name": ""},
            ["не найдено наименование банка получателя"],
        )
        drugoy = kandidat(
            "tesseract",
            {**POLNY, "recipient_corr_account": "", "_buyer_inn": "1661034770", "_buyer_name": "ООО Т"},
            ["не найден корреспондентский счёт"],
        )
        itog = razbor.skleit([osnova, drugoy], payer_inn="7719617469")
        self.assertEqual(itog.data["recipient_bank_name"], POLNY["recipient_bank_name"])
        self.assertTrue(any(p.startswith("счёт выставлен не нам") for p in itog.problems), itog.problems)

    def test_chisty_razbor_ne_trogaetsya(self):
        chisty = kandidat("windows", dict(POLNY), [])
        drugoy = kandidat("tesseract", {**POLNY, "recipient_name": "Другое"}, [])
        itog = razbor.skleit([chisty, drugoy])
        self.assertIs(itog, chisty)

    def test_nenadyozhnoe_imya_zamenyaetsya_godnym(self):
        osnova = kandidat(
            "windows",
            {**POLNY, "recipient_name": "(Иттолнитель): Москва", "recipient_full": "(Иттолнитель): Москва",
             "_name_unreliable": True},
            ["наименование получателя распознано ненадёжно: '(Иттолнитель): Москва'"],
        )
        drugoy = kandidat("tesseract", dict(POLNY), [])
        itog = razbor.skleit([osnova, drugoy])
        self.assertEqual(itog.data["recipient_name"], "ООО Прогресс Парк")
        self.assertFalse(itog.data["_name_unreliable"])
        self.assertEqual(itog.problems, [])


if __name__ == "__main__":
    unittest.main()
