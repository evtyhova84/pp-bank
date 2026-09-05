"""Первый запуск: завести себя в браузере и поднять сервис из окна программы.

База здесь пустая намеренно — проверяется именно то, что видит человек,
установивший программу и ни разу её не открывавший.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

KOREN = Path(__file__).resolve().parent.parent.parent
if str(KOREN) not in sys.path:
    sys.path.insert(0, str(KOREN))

from servis.app import db  # noqa: E402


class PervyZapuskTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        koren = Path(cls._tmp.name)
        db.DATA_DIR = koren
        db.DB_PATH = koren / "pusto.db"
        from servis.app import veb

        veb.SEKRET_FAYL = koren / "sekret.key"
        db.init()

        from fastapi.testclient import TestClient

        from servis.app.main import app

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        cls._tmp.cleanup()

    def test_01_pustaya_baza_vedyot_na_nachalo(self):
        otvet = self.client.get("/vhod", follow_redirects=False)
        self.assertEqual(otvet.status_code, 303)
        self.assertEqual(otvet.headers["location"], "/nachalo")

    def test_02_pro_organizatsiyu_ne_sprashivaem(self):
        stranitsa = self.client.get("/nachalo").text
        self.assertIn("Первый запуск", stranitsa)
        for pole in ("imya", "login", "parol", "parol2"):
            self.assertIn(f'name="{pole}"', stranitsa)
        self.assertNotIn("Название организации", stranitsa)
        self.assertNotIn('name="organizatsiya"', stranitsa)

    def test_03_paroli_ne_sovpali(self):
        otvet = self.client.post(
            "/nachalo",
            data={"login": "anna", "imya": "Анна", "parol": "parol12345", "parol2": "drugoy12345"},
        )
        self.assertIn("пароли не совпали", otvet.text)
        self.assertIn('value="anna"', otvet.text, "введённое стёрлось")

    def test_04_korotky_parol_ne_prohodit(self):
        otvet = self.client.post(
            "/nachalo", data={"login": "anna", "imya": "Анна", "parol": "123", "parol2": "123"}
        )
        self.assertIn("8", otvet.text)

    def test_05_zavodim_sebya_i_srazu_vhodim(self):
        otvet = self.client.post(
            "/nachalo",
            data={"login": "anna", "imya": "Анна", "parol": "parol12345", "parol2": "parol12345"},
            follow_redirects=False,
        )
        self.assertEqual(otvet.status_code, 303, otvet.text[:300])
        self.assertEqual(otvet.headers["location"], "/platelshchiki")
        self.assertIn("pp_session", otvet.headers.get("set-cookie", ""), "сессия не выдана")

        # уже вошли: справочник открывается без отдельного входа
        self.assertEqual(self.client.get("/platelshchiki").status_code, 200)

    def test_06_povtorno_zavesti_administratora_nelzya(self):
        """Иначе на работающем сервисе любой прохожий сделал бы себя админом."""
        self.client.cookies.clear()
        otvet = self.client.get("/nachalo", follow_redirects=False)
        self.assertEqual(otvet.headers["location"], "/vhod")

        otvet = self.client.post(
            "/nachalo",
            data={"login": "chuzhoy", "imya": "Чужой", "parol": "parol12345", "parol2": "parol12345"},
            follow_redirects=False,
        )
        self.assertEqual(otvet.headers["location"], "/vhod")

        conn = db.connect()
        est = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        conn.close()
        self.assertEqual(est, 1, "завёлся лишний пользователь")


class ZapuskatorTest(unittest.TestCase):
    """Окно программы поднимает и останавливает сервис в своём потоке."""

    def setUp(self) -> None:
        # Windows не отдаёт файл базы сразу после остановки сервиса — удаление
        # временной папки не должно из-за этого ронять проверку
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        koren = Path(self._tmp.name)
        db.DATA_DIR = koren
        db.DB_PATH = koren / "okno.db"
        from servis.app import veb

        veb.SEKRET_FAYL = koren / "sekret.key"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_zanyaty_port_ne_ronyaet_zapusk(self):
        import socket

        from servis import zapuskator

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as zanyat:
            zanyat.bind(("127.0.0.1", 0))
            zanyat.listen(1)
            port = zanyat.getsockname()[1]
            self.assertNotEqual(
                zapuskator.svobodny_port(port), port, "занятый порт предложен как свободный"
            )

    def test_servis_podnimaetsya_i_ostanavlivaetsya(self):
        from servis import zapuskator

        servis = zapuskator.Servis()
        adres = servis.zapustit()
        try:
            self.assertTrue(servis.zhdat_gotovnosti(30), "сервис не поднялся")
            with urllib.request.urlopen(f"{adres}/vhod", timeout=10) as otvet:
                self.assertEqual(otvet.status, 200)
                self.assertIn("Первый запуск", otvet.read().decode("utf-8"))
        finally:
            servis.ostanovit()
        self.assertFalse(servis.rabotaet, "сервис не остановился")


if __name__ == "__main__":
    unittest.main()
