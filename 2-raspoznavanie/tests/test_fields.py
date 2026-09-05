"""Поиск реквизитов в строках шапки счёта."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

KOREN = Path(__file__).resolve().parent.parent
if str(KOREN) not in sys.path:
    sys.path.insert(0, str(KOREN))

from parser.fields import find_bank_name, find_bic  # noqa: E402

class BikNadPodpisyuTest(unittest.TestCase):
    """«Образец заполнения платёжного поручения»: БИК напечатан над подписью.

    Так свёрстан счёт ООО «БК АРЕНДА»: значение в своей клетке выше строки
    «Банк получателя БИК». Поиск «после подписи» его не видит, и три счёта
    подряд уходили человеку с пустым БИК.
    """

    ROWS = [
        'Общество с ограниченной ответственностью "БК АРЕНДА"',
        "Образец заполнения платежного поручения",
        "ИНН 1684017026 КПП 168401001",
        "Получатель",
        'ООО "БК АРЕНДА" Сч. №40702810129370003742',
        "042202824",
        "Банк получателя БИК",
        'ФИЛИАЛ "НИЖЕГОРОДСКИЙ" Сч. №30101810200000000824',
        'АО "АЛЬФА-БАНК"',
        "СЧЕТ № 255 от 05 августа 2026 г.",
    ]

    def test_bik_nad_podpisyu_nahoditsya(self):
        self.assertEqual(find_bic(self.ROWS), "042202824")

    def test_odinokaya_podpis_bik_ne_popadaet_v_nazvanie_banka(self):
        bank = find_bank_name(self.ROWS)
        self.assertNotIn("БИК", bank)
        self.assertIn("АЛЬФА-БАНК", bank)

    def test_sluchaynoe_devyatiznachnoe_ne_beryotsya_za_bik(self):
        """Без подписи «БИК» по соседству одинокое число — не БИК."""
        rows = ["ООО Ромашка", "042202824", "Итого 100"]
        self.assertEqual(find_bic(rows), "")


if __name__ == "__main__":
    unittest.main()
