"""Платёжка насквозь: настоящий PDF -> реквизиты в форме.

Остальные проверки разбора платёжки подменяют чтение файла заранее готовыми
строками. Это удобно, но именно в подменённом шаге — «PDF превращается
в строки» — и жила ошибка с разрядкой: номера в бланке печатают по клеткам,
и `616125855326` приезжает как `6 1 6 1 2 5 8 5 5 3 2 6`. Подменённые строки
такого не показывают, поэтому здесь читаются настоящие файлы.

Образцы лежат в `obraztsy/`, сделаны скриптом `obraztsy/sdelat.py`.
Реквизиты в них — ИП из открытой платёжки, суммы и назначение вымышлены.
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

KOREN = Path(__file__).resolve().parent.parent.parent
if str(KOREN) not in sys.path:
    sys.path.insert(0, str(KOREN))

from servis.app import auth, db, iz_platezhki, yadro  # noqa: E402,F401  (yadro — пути к ядру)

OBRAZTSY = Path(__file__).resolve().parent / "obraztsy"

# Что должно прочитаться из образцов
ETALON = {
    "inn": "616125855326",
    "kpp": "",
    "account": "40802810003500013642",
    "bic": "044525104",
    "corr_account": "30101810745374525104",
    "bank_name": 'ООО "Банк Точка"',
    "bank_city": "г. Москва",
    "name": "ИП Шульмина Надежда Алексеевна",
}

FAYLY = (
    "platezhka-0401060.pdf",
    "platezhka-vrazryadku.pdf",
    "platezhka-podpisi-otdelno.pdf",
)


class NaskvozTest(unittest.TestCase):
    """Чтение настоящего файла нашим обычным движком."""

    def test_obraztsy_na_meste(self):
        for imya in FAYLY:
            self.assertTrue((OBRAZTSY / imya).exists(), f"нет образца {imya}")

    def test_razryadka_v_obraztse_deystvitelno_est(self):
        """Образец обязан воспроизводить поломку, иначе он ничего не проверяет."""
        from parser.rows import read_rows

        stroki = read_rows(OBRAZTSY / "platezhka-vrazryadku.pdf")
        s_innom = next((s for s in stroki if s.startswith("ИНН")), "")
        self.assertNotIn("616125855326", s_innom, "цифры не разъехались — образец бесполезен")
        self.assertIn("6 1 6", s_innom, "ожидалась разрядка по клеткам")

    def test_podpisi_v_obraztse_deystvitelno_otorvany(self):
        """Подпись клетки уехала в свою строку — искать значение рядом бесполезно."""
        from parser.rows import read_rows

        stroki = read_rows(OBRAZTSY / "platezhka-podpisi-otdelno.pdf")
        self.assertTrue(
            any(s.strip().startswith("ИНН КПП") for s in stroki),
            "подписи не оторвались от значений — образец не воспроизводит поломку",
        )

    def test_rekvizity_chitayutsya_iz_vseh_obraztsov(self):
        for imya in FAYLY:
            put = OBRAZTSY / imya
            with self.subTest(fayl=imya):
                itog = iz_platezhki.razobrat(imya, put.read_bytes(), put)
                for pole, zhdyom in ETALON.items():
                    self.assertEqual(itog[pole], zhdyom, f"{imya}: поле {pole}")

    def test_prochitannoe_shoditsya_po_kontrolnym_summam(self):
        from generator.validate import check_account, check_bic, check_inn

        put = OBRAZTSY / "platezhka-podpisi-otdelno.pdf"
        itog = iz_platezhki.razobrat(put.name, put.read_bytes(), put)
        self.assertTrue(check_inn(itog["inn"]))
        self.assertTrue(check_bic(itog["bic"]))
        self.assertTrue(check_account(itog["account"], itog["bic"]))
        self.assertTrue(check_account(itog["corr_account"], itog["bic"], corr=True))


class CherezBrauzerTest(unittest.TestCase):
    """То же самое, но как это делает человек: загрузка файла в форму."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        koren = Path(cls._tmp.name)
        db.DATA_DIR = koren
        db.DB_PATH = koren / "naskvoz.db"
        from servis.app import veb

        veb.SEKRET_FAYL = koren / "sekret.key"
        db.init()
        conn = db.connect()
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO companies (name, created_at) VALUES (?, ?)", ("Тест", db.now())
            )
            auth.create_user(conn, 1, "anna", "parol12345", "Анна", "admin")
        conn.close()

        from fastapi.testclient import TestClient

        from servis.app.main import app

        cls.client = TestClient(app)
        cls.client.post("/vhod", data={"login": "anna", "parol": "parol12345"})

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        cls._tmp.cleanup()

    def token(self) -> str:
        return re.search(
            r'name="token" value="([^"]+)"', self.client.get("/platelshchiki").text
        ).group(1)

    def test_zagruzka_platezhki_zapolnyaet_formu(self):
        put = OBRAZTSY / "platezhka-podpisi-otdelno.pdf"
        otvet = self.client.post(
            "/platelshchiki/iz-platezhki",
            data={"token": self.token()},
            files=[("fayl", (put.name, put.read_bytes(), "application/pdf"))],
        )
        self.assertEqual(otvet.status_code, 200)
        self.assertIn("Реквизиты прочитаны", otvet.text)
        for pole in ("inn", "account", "bic", "corr_account"):
            self.assertIn(
                f'value="{ETALON[pole]}"', otvet.text, f"поле {pole} не подставилось в форму"
            )
        # ИНН получателя на странице есть — скрытым полем кнопки «это не мы»,
        # но в форму плательщика он попасть не должен
        self.assertNotIn('name="inn" value="7716509296"', otvet.text, "подставился ИНН получателя")
        self.assertIn("взять получателя", otvet.text, "нет переключения на вторую сторону")

    def test_posle_chteniya_platelshchik_sohranyaetsya(self):
        """Форма заполнилась — значит её можно сохранить не правя руками."""
        put = OBRAZTSY / "platezhka-podpisi-otdelno.pdf"
        stranitsa = self.client.post(
            "/platelshchiki/iz-platezhki",
            data={"token": self.token()},
            files=[("fayl", (put.name, put.read_bytes(), "application/pdf"))],
        ).text

        polya = dict(re.findall(r'name="(\w+)" value="([^"]*)"', stranitsa))
        polya["token"] = self.token()
        otvet = self.client.post("/platelshchiki", data=polya, follow_redirects=False)
        self.assertEqual(otvet.status_code, 303, "плательщик не сохранился")

        conn = db.connect()
        schet = conn.execute("SELECT * FROM payer_accounts").fetchone()
        payer = conn.execute("SELECT * FROM payers").fetchone()
        conn.close()
        self.assertEqual(payer["inn"], ETALON["inn"])
        self.assertEqual(schet["account"], ETALON["account"])
        self.assertEqual(schet["bic"], ETALON["bic"])


if __name__ == "__main__":
    unittest.main()
