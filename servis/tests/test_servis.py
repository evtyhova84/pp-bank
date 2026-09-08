"""Сквозные проверки сервиса: от входа до файла для клиент-банка.

База и папки на время теста подменяются на временные — в рабочие данные тесты
не лезут. Само распознавание здесь подменено заглушкой: оно проверяется своими
41 тестом в части 2, а реальные счета в репозиторий не кладутся — там живые
реквизиты контрагентов. Проверяется всё остальное: доступ, права, защита форм,
проверка реквизитов, правка руками, нумерация и байты выходного файла.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

KOREN = Path(__file__).resolve().parent.parent.parent
if str(KOREN) not in sys.path:
    sys.path.insert(0, str(KOREN))

from servis.app import auth, db  # noqa: E402

# Реквизиты с настоящими контрольными суммами, но вымышленные (демо части 1)
PLATELSHCHIK = {
    "code": "romashka",
    "name": "ООО Ромашка",
    "full_name": 'Общество с ограниченной ответственностью "Ромашка"',
    "inn": "7719617469",
    "kpp": "771901001",
    "account": "40702810300180001774",
    "bank_name": "АО ОТП БАНК",
    "bank_city": "Г. МОСКВА",
    "bic": "044525311",
    "corr_account": "30101810000000000311",
    "metka": "ОТП",
    "numbering_start": "900001",
}

# Второй банк той же организации: реквизиты другие, нумерация независимая
VTOROY_SCHET = {
    "metka": "Петрокоммерц",
    "account": "40702810123111111114",
    "bank_name": "ПАО БАНК ПЕТРОКОММЕРЦ",
    "bank_city": "Г. МОСКВА",
    "bic": "044525352",
    "corr_account": "30101810700000000352",
    "numbering_start": "800001",
}

SCHET_CHISTY = {
    "invoice_number": "1274",
    "invoice_date": "2026-08-20",
    "recipient_name": "ООО Прогресс Парк",
    "recipient_full": 'Общество с ограниченной ответственностью "Прогресс Парк"',
    "recipient_inn": "7701325465",
    "recipient_kpp": "770101001",
    "recipient_account": "40702810123111111114",
    "recipient_bank_name": "ПАО БАНК ПЕТРОКОММЕРЦ",
    "recipient_bank_city": "Г. МОСКВА",
    "recipient_bic": "044525352",
    "recipient_corr_account": "30101810700000000352",
    "total_amount": "12354.00",
    "vat_rate": "20",
    "vat_amount": "2059.00",
    "vat_status": "included",
}

# Тот же счёт, но БИК распознан с ошибкой в одной цифре — контрольные суммы не сойдутся
SCHET_KRIVOY = {**SCHET_CHISTY, "invoice_number": "1275", "recipient_bic": "044525353"}

# Тот же получатель, но КПП со скана не прочитался — его должен добрать справочник
SCHET_BEZ_KPP = {**SCHET_CHISTY, "invoice_number": "1276", "recipient_kpp": ""}

# Ещё не оплаченный счёт — на нём проверяем дубли внутри одной пачки
SCHET_DRUGOY = {**SCHET_CHISTY, "invoice_number": "1299"}

# Счёт, выставленный на другую организацию: реквизиты чистые, но платить
# по нему с этого счёта нельзя. Парсер это видит и говорит — сервис обязан
# донести, а не потерять по дороге.
SCHET_CHUZHOY = {**SCHET_CHISTY, "invoice_number": "1300", "_buyer_inn": "1661034770",
                 "_buyer_name": "ООО ТЕТРАКОМ", "_buyer_kpp": "166101001"}

# Организация, на которую выставлен чужой счёт: заводится по ходу теста
# с банковскими реквизитами первого плательщика (это вымышленные реквизиты)
TETRAKOM = {
    **{k: PLATELSHCHIK[k] for k in ("account", "bank_name", "bank_city", "bic", "corr_account")},
    "code": "tetrakom",
    "name": "ООО ТЕТРАКОМ",
    "full_name": "ООО ТЕТРАКОМ",
    "inn": "1661034770",
    "kpp": "166101001",
    "metka": "ОТП",
    "numbering_start": "700001",
}

# Получатель, чьи счета про НДС молчат (так выглядят счета ИП на УСН).
# Реквизиты вымышленные, контрольные суммы сходятся.
TIHAYA_GAVAN = {
    "recipient_name": "ООО Тихая Гавань",
    "recipient_full": 'Общество с ограниченной ответственностью "Тихая Гавань"',
    "recipient_inn": "7705123452",
    "recipient_kpp": "770501001",
    "recipient_account": "40702810500000001230",
    "recipient_bank_name": "АО ОТП БАНК",
    "recipient_bank_city": "Г. МОСКВА",
    "recipient_bic": "044525311",
    "recipient_corr_account": "30101810000000000311",
    "invoice_date": "2026-08-21",
    "total_amount": "1000.00",
    "vat_rate": None,
    "vat_amount": None,
    "vat_status": "unknown",
    "payment_description": "Услуги по договору; Консультация по учёту",
}
SCHET_NDS_NEIZVESTEN_1 = {**TIHAYA_GAVAN, "invoice_number": "N-1"}
SCHET_NDS_NEIZVESTEN_2 = {**TIHAYA_GAVAN, "invoice_number": "N-2"}
SCHET_NDS_NEIZVESTEN_3 = {**TIHAYA_GAVAN, "invoice_number": "N-3"}

# Со скана реквизиты прочитались верно, а имя — мусором
SCHET_MUSOR_V_IMENI = {**SCHET_CHISTY, "invoice_number": "1301",
                       "recipient_name": "(Иттолнитель): Москва, пр-кт", "_name_unreliable": True}

FAYLY = {
    "chisty.pdf": SCHET_CHISTY,
    "krivoy.pdf": SCHET_KRIVOY,
    "bez_kpp.pdf": SCHET_BEZ_KPP,
    "drugoy.pdf": SCHET_DRUGOY,
    "chuzhoy.pdf": SCHET_CHUZHOY,
    "musor.pdf": SCHET_MUSOR_V_IMENI,
    "nds1.pdf": SCHET_NDS_NEIZVESTEN_1,
    "nds2.pdf": SCHET_NDS_NEIZVESTEN_2,
    "nds3.pdf": SCHET_NDS_NEIZVESTEN_3,
}


def _zaglushka(path, payer_inn="", vtoraya_stupen=None):
    """Вместо распознавания — заранее известные данные.

    Загруженный файл сохраняется под случайным именем, поэтому какой это счёт,
    узнаём по его содержимому: тест кладёт туда ключ.
    """
    from servis.app.razbor import Razobrano, proverit_summy

    data = dict(FAYLY[Path(path).read_bytes().decode("utf-8").strip()])
    iz_parsera = []
    # Те же замечания и в тех же словах, что у настоящего парсера
    if data.get("_buyer_inn") and payer_inn and data["_buyer_inn"] != payer_inn:
        iz_parsera.append(f"счёт выставлен не нам: покупатель ИНН {data['_buyer_inn']}")
    if data.get("_name_unreliable"):
        iz_parsera.append("наименование получателя распознано ненадёжно: мусор")
    if data.get("vat_status") == "unknown":
        iz_parsera.append("НДС не найден и «без НДС» в счёте не написано")
    return Razobrano(
        fayl=Path(path).name, data=data, problems=iz_parsera + proverit_summy(data),
        dvizhok="тест", iz_parsera=iz_parsera,
    )


class SkvoznoyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        koren = Path(cls._tmp.name)

        # Подменяем хранилище до того, как приложение его тронет
        db.DATA_DIR = koren
        db.DB_PATH = koren / "test.db"
        from servis.app import pachki, veb

        pachki.ZAGRUZKI = koren / "zagruzki"
        pachki.VYGRUZKI = koren / "vygruzki"
        pachki.razobrat = _zaglushka
        veb.SEKRET_FAYL = koren / "sekret.key"

        db.init()
        conn = db.connect()
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO companies (name, created_at) VALUES (?, ?)", ("Тест", db.now())
            )
            auth.create_user(conn, 1, "admin", "parol12345", "Админ", "admin")
            auth.create_user(conn, 1, "petrov", "parol12345", "Пётр", "operator")
        conn.close()

        from fastapi.testclient import TestClient

        from servis.app.main import app

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        cls._tmp.cleanup()

    # --- вспомогательное ---

    def voyti(self, login: str = "admin", parol: str = "parol12345"):
        self.client.cookies.clear()
        otvet = self.client.post(
            "/vhod", data={"login": login, "parol": parol}, follow_redirects=False
        )
        self.assertEqual(otvet.status_code, 303, "вход не удался")

    def token(self, put: str) -> str:
        nayden = re.search(r'name="token" value="([^"]+)"', self.client.get(put).text)
        self.assertIsNotNone(nayden, f"на странице {put} нет метки формы")
        return nayden.group(1)

    @property
    def schet_id(self) -> int:
        conn = db.connect()
        try:
            row = conn.execute("SELECT id FROM payer_accounts ORDER BY id").fetchone()
            self.assertIsNotNone(row, "не заведён ни один счёт плательщика")
            return int(row["id"])
        finally:
            conn.close()

    def zhdat(self, pachka_id: int) -> None:
        conn = db.connect()
        try:
            for _ in range(150):
                status = conn.execute(
                    "SELECT status FROM batches WHERE id = ?", (pachka_id,)
                ).fetchone()["status"]
                if status != "processing":
                    return
                time.sleep(0.05)
            self.fail("распознавание не завершилось")
        finally:
            conn.close()

    def zagruzit(self, imena: list[str], schet_id: int | None = None) -> int:
        otvet = self.client.post(
            "/pachki/nova",
            data={
                "token": self.token("/pachki/nova"),
                "schet_id": str(schet_id or self.schet_id),
                "data_platezha": "2026-08-29",
            },
            files=[
                ("fayly", (imya, imya.encode("utf-8"), "application/pdf")) for imya in imena
            ],
            follow_redirects=False,
        )
        self.assertEqual(otvet.status_code, 303, otvet.text[:400])
        pachka_id = int(otvet.headers["location"].rsplit("/", 1)[1])
        self.zhdat(pachka_id)
        return pachka_id

    # --- доступ ---

    def test_01_bez_vhoda_ne_puskaet(self):
        self.client.cookies.clear()
        otvet = self.client.get("/pachki", follow_redirects=False)
        self.assertEqual(otvet.status_code, 303)
        self.assertEqual(otvet.headers["location"], "/vhod")

    def test_02_neverny_parol(self):
        otvet = self.client.post("/vhod", data={"login": "admin", "parol": "nepravilny"})
        self.assertIn("неверный логин или пароль", otvet.text)

    def test_03_operator_ne_pravit_spravochniki(self):
        self.voyti("petrov")
        self.assertEqual(
            self.client.post("/platelshchiki", data={"token": "x"}).status_code, 403
        )
        self.assertEqual(self.client.get("/sotrudniki").status_code, 403)

    def _id_polzovatelya(self, login: str) -> int:
        conn = db.connect()
        try:
            return conn.execute("SELECT id FROM users WHERE login = ?", (login,)).fetchone()["id"]
        finally:
            conn.close()

    def test_03a_rol_menyaetsya_tuda_i_obratno(self):
        """Оператора повышают до администратора и возвращают обратно.

        Роль читается из базы на каждый запрос, поэтому она действует сразу —
        сотруднику не нужно перезаходить.
        """
        petrov = self._id_polzovatelya("petrov")
        self.voyti()
        self.client.post(f"/sotrudniki/{petrov}/rol", data={"token": self.token("/sotrudniki")})
        self.voyti("petrov")
        self.assertEqual(self.client.get("/sotrudniki").status_code, 200)

        self.voyti()
        self.client.post(f"/sotrudniki/{petrov}/rol", data={"token": self.token("/sotrudniki")})
        self.voyti("petrov")
        self.assertEqual(self.client.get("/sotrudniki").status_code, 403)

    def test_03b_sebe_rol_ne_smenit(self):
        """Себя разжаловать нельзя: вернуть роль было бы некому."""
        self.voyti()
        ya = self._id_polzovatelya("admin")
        otvet = self.client.post(
            f"/sotrudniki/{ya}/rol", data={"token": self.token("/sotrudniki")}
        )
        self.assertEqual(otvet.status_code, 400)

    def test_03v_operator_rol_ne_menyaet(self):
        """Смена роли — дело администратора, оператору маршрут закрыт."""
        self.voyti("petrov")
        self.assertEqual(
            self.client.post("/sotrudniki/1/rol", data={"token": "x"}).status_code, 403
        )

    def test_04_forma_bez_metki_otvergaetsya(self):
        self.voyti()
        otvet = self.client.post(
            "/pachki/nova",
            data={"token": "poddelka", "schet_id": "1", "data_platezha": "2026-08-29"},
            files=[("fayly", ("chisty.pdf", b"%PDF", "application/pdf"))],
        )
        self.assertEqual(otvet.status_code, 400)

    # --- справочник плательщиков ---

    def test_05_platelshchik_sohranyaetsya(self):
        self.voyti()
        otvet = self.client.post(
            "/platelshchiki",
            data={**PLATELSHCHIK, "token": self.token("/platelshchiki")},
            follow_redirects=True,
        )
        self.assertIn("ООО Ромашка", otvet.text)

    def test_06_krivye_rekvizity_ne_sohranyayutsya(self):
        """Плательщик с несходящимся ИНН до базы не доходит."""
        self.voyti()
        otvet = self.client.post(
            "/platelshchiki",
            data={
                **PLATELSHCHIK,
                "code": "krivoy",
                "inn": "7719617460",
                "token": self.token("/platelshchiki"),
            },
        )
        self.assertIn("ИНН", otvet.text)
        conn = db.connect()
        est = conn.execute("SELECT COUNT(*) AS n FROM payers WHERE code = 'krivoy'").fetchone()["n"]
        conn.close()
        self.assertEqual(est, 0, "плательщик с неверным ИНН попал в базу")

    # --- основной сценарий ---

    def test_07_polny_prohod(self):
        self.voyti()
        pachka_id = self.zagruzit(["chisty.pdf", "krivoy.pdf"])

        stranitsa = self.client.get(f"/pachki/{pachka_id}").text
        self.assertIn("нужна проверка", stranitsa, "кривой счёт не помечен")
        self.assertIn("Платёжек к выгрузке: 1", stranitsa)
        self.assertIn("Требуют проверки: 1", stranitsa)

        # выгружается только чистый счёт
        otvet = self.client.post(
            f"/pachki/{pachka_id}/vygruzka",
            data={"token": self.token(f"/pachki/{pachka_id}")},
            follow_redirects=False,
        )
        self.assertEqual(otvet.status_code, 303, otvet.text[:400])

        fayl = self.client.get(f"/pachki/{pachka_id}/fayl")
        self.assertEqual(fayl.status_code, 200)
        text = fayl.content.decode("cp1251")
        self.assertTrue(text.startswith("1CClientBankExchange"))
        self.assertEqual(text.count("СекцияДокумент=Платежное поручение"), 1)
        self.assertIn("Номер=900001", text)
        self.assertIn("\r\n", text, "банк ждёт перевод строки CRLF")
        self.assertIn("НазначениеПлатежа=Оплата по счету № 1274", text)

    def test_08_pravka_rukami_chinit_schet(self):
        self.voyti()
        pachka_id = self.zagruzit(["krivoy.pdf"])

        conn = db.connect()
        doc_id = conn.execute(
            "SELECT id FROM docs WHERE batch_id = ?", (pachka_id,)
        ).fetchone()["id"]
        conn.close()

        put = f"/pachki/{pachka_id}"
        otvet = self.client.post(
            f"{put}/dokument/{doc_id}",
            data={
                **{k: v for k, v in SCHET_CHISTY.items() if not k.startswith("_")},
                "invoice_number": "1275",
                "token": self.token(put),
                "deystvie": "sohranit",
            },
            follow_redirects=True,
        )
        self.assertIn("Платёжек к выгрузке: 1", otvet.text)
        self.assertIn("поправлено вручную", otvet.text)

    def test_09_isklyuchenny_schet_ne_vygruzhaetsya(self):
        self.voyti()
        pachka_id = self.zagruzit(["chisty.pdf"])
        conn = db.connect()
        doc_id = conn.execute(
            "SELECT id FROM docs WHERE batch_id = ?", (pachka_id,)
        ).fetchone()["id"]
        conn.close()

        put = f"/pachki/{pachka_id}"
        self.client.post(
            f"{put}/dokument/{doc_id}",
            data={"token": self.token(put), "deystvie": "isklyuchit"},
            follow_redirects=True,
        )
        otvet = self.client.post(
            f"{put}/vygruzka", data={"token": self.token(put)}, follow_redirects=False
        )
        self.assertEqual(otvet.status_code, 400, "выгрузка пустой пачки должна отклоняться")

    def test_10_nomera_ne_dublyatsya(self):
        """Тот же счёт в новой пачке получает тот же номер — двойной оплаты нет."""
        self.voyti()
        conn = db.connect()
        bylo = conn.execute("SELECT COUNT(*) AS n FROM assigned").fetchone()["n"]
        conn.close()

        pachka_id = self.zagruzit(["chisty.pdf"])
        put = f"/pachki/{pachka_id}"
        self.client.post(f"{put}/vygruzka", data={"token": self.token(put)}, follow_redirects=False)

        conn = db.connect()
        stalo = conn.execute("SELECT COUNT(*) AS n FROM assigned").fetchone()["n"]
        conn.close()
        self.assertEqual(stalo, bylo, "тот же счёт получил новый номер платёжки")

    # --- реквизиты из старой платёжки ---

    def obmen(self) -> bytes:
        """Файл клиент-банка с нашим плательщиком — то, что бухгалтер уже имеет."""
        stroki = [
            "1CClientBankExchange",
            "ВерсияФормата=1.02",
            "Кодировка=Windows",
            f"РасчСчет={PLATELSHCHIK['account']}",
            "СекцияДокумент=Платежное поручение",
            "Номер=41",
            "Дата=12.08.2026",
            "Сумма=1000.00",
            f"ПлательщикСчет={PLATELSHCHIK['account']}",
            f"ПлательщикИНН={PLATELSHCHIK['inn']}",
            f"Плательщик1={PLATELSHCHIK['full_name']}",
            f"ПлательщикКПП={PLATELSHCHIK['kpp']}",
            f"ПлательщикБанк1={PLATELSHCHIK['bank_name']}",
            f"ПлательщикБанк2={PLATELSHCHIK['bank_city']}",
            f"ПлательщикБИК={PLATELSHCHIK['bic']}",
            f"ПлательщикКорсчет={PLATELSHCHIK['corr_account']}",
            "ПолучательСчет=40702810123111111114",
            "ПолучательИНН=7701325465",
            "КонецДокумента",
            "КонецФайла",
        ]
        return "\r\n".join(stroki).encode("cp1251")

    def test_11c_rekvizity_chitayutsya_iz_platezhki(self):
        self.voyti()
        otvet = self.client.post(
            "/platelshchiki/iz-platezhki",
            data={"token": self.token("/platelshchiki")},
            files=[("fayl", ("1c_to_kl.txt", self.obmen(), "text/plain"))],
        )
        self.assertEqual(otvet.status_code, 200)
        for pole in ("account", "inn", "kpp", "bic", "corr_account"):
            self.assertIn(
                f'value="{PLATELSHCHIK[pole]}"', otvet.text, f"поле {pole} не подставилось"
            )
        self.assertIn("Реквизиты прочитаны", otvet.text)
        self.assertNotIn("40702810123111111114", otvet.text, "подставились реквизиты получателя")

    def test_11f_vtoraya_storona_platezhki_perekluchaetsya_knopkoy(self):
        """Платёжка, которой заплатили нам: сервис подставляет получателя и
        объясняет почему, а кнопка «это не мы» переставляет стороны."""
        from servis.tests.test_iz_platezhki import PLATEZHKA_VHODYASHCHAYA
        from parser import rows

        self.voyti()
        bylo = rows.read_rows
        rows.read_rows = lambda put: PLATEZHKA_VHODYASHCHAYA
        try:
            otvet = self.client.post(
                "/platelshchiki/iz-platezhki",
                data={"token": self.token("/platelshchiki")},
                files=[("fayl", ("platezhka.pdf", b"%PDF-1.4", "application/pdf"))],
            )
        finally:
            rows.read_rows = bylo
        self.assertEqual(otvet.status_code, 200)
        self.assertIn('name="inn" value="7701325465"', otvet.text, "получатель не подставлен")
        self.assertIn("выдан банком получателя", otvet.text)
        self.assertIn("взять плательщика", otvet.text)

        skrytye = dict(re.findall(r'name="((?:vybor|drugaya)_\w+)" value="([^"]*)"', otvet.text))
        self.assertEqual(skrytye["vybor_inn"], "7719617469")
        otvet = self.client.post(
            "/platelshchiki/storona",
            data={**skrytye, "token": self.token("/platelshchiki")},
        )
        self.assertEqual(otvet.status_code, 200)
        self.assertIn('name="inn" value="7719617469"', otvet.text, "стороны не поменялись")
        self.assertIn('name="account" value="40702810300180001774"', otvet.text)
        self.assertIn("Подставлен плательщик", otvet.text)
        self.assertIn("взять получателя", otvet.text, "обратное переключение пропало")

    def test_11d_ne_platezhka_govorit_pochemu(self):
        self.voyti()
        otvet = self.client.post(
            "/platelshchiki/iz-platezhki",
            data={"token": self.token("/platelshchiki")},
            files=[("fayl", ("spisok.txt", "просто текст".encode("utf-8"), "text/plain"))],
        )
        self.assertEqual(otvet.status_code, 200)
        self.assertIn("не похож на выгрузку клиент-банка", otvet.text)

    def test_11e_operator_ne_chitaet_platezhki(self):
        self.voyti("petrov")
        otvet = self.client.post(
            "/platelshchiki/iz-platezhki",
            data={"token": "x"},
            files=[("fayl", ("1c_to_kl.txt", self.obmen(), "text/plain"))],
        )
        self.assertEqual(otvet.status_code, 403)

    def test_11a_podbor_parolya_zakryvaet_vhod(self):
        """После нескольких неудач вход закрыт даже с верным паролем."""
        conn = db.connect()
        with db.transaction(conn):
            auth.create_user(conn, 1, "zamok", "parol12345", "Замок", "operator")
        conn.close()

        for _ in range(auth.POPYTOK_NA_LOGIN):
            otvet = self.client.post("/vhod", data={"login": "zamok", "parol": "nepravilny"})
            self.assertIn("неверный логин или пароль", otvet.text)

        otvet = self.client.post(
            "/vhod", data={"login": "zamok", "parol": "parol12345"}, follow_redirects=False
        )
        self.assertEqual(otvet.status_code, 200, "вход должен быть закрыт, а не выполнен")
        self.assertIn("слишком много неудачных попыток", otvet.text)
        self.assertIn("мин.", otvet.text, "не сказано, сколько ждать")

    def test_11b_udachny_vhod_sbrasyvaet_schetchik(self):
        """Опечатки не копятся: вспомнил пароль — счётчик обнулился."""
        conn = db.connect()
        with db.transaction(conn):
            auth.create_user(conn, 1, "sidorov", "parol12345", "Сидоров", "operator")
        conn.close()

        for _ in range(auth.POPYTOK_NA_LOGIN - 1):
            self.client.post("/vhod", data={"login": "sidorov", "parol": "nepravilny"})

        otvet = self.client.post(
            "/vhod", data={"login": "sidorov", "parol": "parol12345"}, follow_redirects=False
        )
        self.assertEqual(otvet.status_code, 303, "верный пароль должен пустить")

        # счётчик обнулён — снова доступны все попытки
        conn = db.connect()
        ostalos = conn.execute(
            "SELECT COUNT(*) AS n FROM popytki WHERE login = 'sidorov' AND udachno = 0"
        ).fetchone()["n"]
        conn.close()
        self.assertEqual(ostalos, 0, "неудачные попытки не обнулились после входа")

    def test_11_chuzhuyu_pachku_ne_vidno(self):
        """Пачка другой организации недоступна даже по прямой ссылке."""
        conn = db.connect()
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO companies (name, created_at) VALUES (?, ?)", ("Соседи", db.now())
            )
            chuzhaya = conn.execute("SELECT id FROM companies WHERE name = 'Соседи'").fetchone()["id"]
            conn.execute(
                "INSERT INTO payers (company_id, code, name, inn, created_at) VALUES (?,?,?,?,?)",
                (chuzhaya, "sosed", "ООО Соседи", PLATELSHCHIK["inn"], db.now()),
            )
            payer_id = conn.execute("SELECT id FROM payers WHERE code = 'sosed'").fetchone()["id"]
            conn.execute(
                "INSERT INTO payer_accounts (payer_id, account, bank_name, bic, corr_account,"
                " created_at) VALUES (?,?,?,?,?,?)",
                (payer_id, PLATELSHCHIK["account"], "БАНК", PLATELSHCHIK["bic"],
                 PLATELSHCHIK["corr_account"], db.now()),
            )
            auth.create_user(conn, chuzhaya, "sosed", "parol12345", "Сосед", "admin")
            conn.execute(
                "INSERT INTO batches (company_id, user_id, payer_id, payment_date, created_at)"
                " VALUES (?, (SELECT id FROM users WHERE login='sosed'), ?, ?, ?)",
                (chuzhaya, payer_id, "2026-08-29", db.now()),
            )
            chuzhaya_pachka = conn.execute(
                "SELECT id FROM batches WHERE company_id = ?", (chuzhaya,)
            ).fetchone()["id"]
        conn.close()

        self.voyti()
        self.assertEqual(self.client.get(f"/pachki/{chuzhaya_pachka}").status_code, 404)


    # --- несколько расчётных счетов у одного плательщика ---

    def test_12_vtoroy_schet_i_svoya_numeraciya(self):
        """У второго банка своя нумерация: счётчик ведётся по (плательщик, счёт, год)."""
        self.voyti()
        conn = db.connect()
        payer_id = conn.execute("SELECT id FROM payers WHERE code = 'romashka'").fetchone()["id"]
        conn.close()

        otvet = self.client.post(
            f"/platelshchiki/{payer_id}/schet",
            data={**VTOROY_SCHET, "token": self.token("/platelshchiki")},
            follow_redirects=True,
        )
        self.assertIn(VTOROY_SCHET["account"], otvet.text, "второй счёт не появился")

        conn = db.connect()
        vtoroy_id = conn.execute(
            "SELECT id FROM payer_accounts WHERE account = ?", (VTOROY_SCHET["account"],)
        ).fetchone()["id"]
        conn.close()

        otvet = self.client.post(
            "/pachki/nova",
            data={
                "token": self.token("/pachki/nova"),
                "schet_id": str(vtoroy_id),
                "data_platezha": "2026-08-29",
            },
            files=[("fayly", ("chisty.pdf", b"chisty.pdf", "application/pdf"))],
            follow_redirects=False,
        )
        pachka_id = int(otvet.headers["location"].rsplit("/", 1)[1])
        self.zhdat(pachka_id)

        put = f"/pachki/{pachka_id}"
        self.assertIn(VTOROY_SCHET["account"], self.client.get(put).text)
        otvet = self.client.post(
            f"{put}/vygruzka", data={"token": self.token(put)}, follow_redirects=False
        )
        self.assertEqual(otvet.status_code, 303, otvet.text[:400])

        text = self.client.get(f"{put}/fayl").content.decode("cp1251")
        self.assertIn("Номер=800001", text, "номер взят не из диапазона второго счёта")
        self.assertIn(f"ПлательщикРасчСчет={VTOROY_SCHET['account']}", text)

    # --- справочник получателей ---

    def test_13_spravochnik_poluchateley_nabiraetsya_sam(self):
        self.voyti()
        otvet = self.client.get("/poluchateli")
        self.assertEqual(otvet.status_code, 200)
        self.assertIn("ООО Прогресс Парк", otvet.text, "получатель не попал в справочник")
        self.assertIn(SCHET_CHISTY["recipient_account"], otvet.text)

        conn = db.connect()
        row = conn.execute(
            "SELECT * FROM poluchateli WHERE inn = ?", (SCHET_CHISTY["recipient_inn"],)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertGreater(row["schetov"], 1, "счётчик разобранных счетов не растёт")
        self.assertGreater(row["platezhey"], 0, "оплаты не отмечаются")

    def test_14_nedochitannoe_beryotsya_iz_spravochnika(self):
        """КПП не прочитался со скана, но получатель знакомый — поле подставится."""
        self.voyti()
        pachka_id = self.zagruzit(["bez_kpp.pdf"])

        conn = db.connect()
        doc = conn.execute(
            "SELECT data_json, problems_json FROM docs WHERE batch_id = ?", (pachka_id,)
        ).fetchone()
        conn.close()

        import json as _json

        data = _json.loads(doc["data_json"])
        self.assertEqual(data["recipient_kpp"], SCHET_CHISTY["recipient_kpp"])
        self.assertIn("из справочника получателей", doc["problems_json"])

    def test_15_rekvizity_deneg_iz_spravochnika_ne_beryotsya(self):
        """Расчётный счёт и ИНН из истории не подставляются никогда."""
        from servis.app import poluchateli

        self.assertNotIn("account", poluchateli.DOPOLNYAEM)
        self.assertNotIn("inn", poluchateli.DOPOLNYAEM)
        self.assertNotIn("bic", poluchateli.DOPOLNYAEM)


    # --- замечания парсера не теряются ---

    def test_18_chuzhoy_schet_ne_uhodit_v_fayl(self):
        """«Счёт выставлен не нам» — замечание парсера. Однажды оно терялось
        при пересчёте после подстановки из справочника, и 49 чужих счетов ушли
        в файл как чистые."""
        self.voyti()
        pachka_id = self.zagruzit(["chuzhoy.pdf"])
        conn = db.connect()
        doc = conn.execute(
            "SELECT status, problems_json FROM docs WHERE batch_id = ?", (pachka_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(doc["status"], "problem", "чужой счёт прошёл как чистый")
        self.assertIn("выставлен не нам", doc["problems_json"])

    def test_19_musor_v_imeni_ne_uhodit_v_fayl_a_spravochnik_chinit(self):
        """Ненадёжное имя — ошибка, а не заметка: оно ушло бы в поле «Получатель».
        Но если этому получателю уже платили, справочник знает его имя и заменяет."""
        self.voyti()
        pachka_id = self.zagruzit(["musor.pdf"])
        conn = db.connect()
        doc = conn.execute(
            "SELECT status, data_json, problems_json FROM docs WHERE batch_id = ?", (pachka_id,)
        ).fetchone()
        conn.close()
        import json as _json

        data = _json.loads(doc["data_json"])
        # Этому получателю (ИНН + счёт из SCHET_CHISTY) уже платили — имя известно
        self.assertEqual(doc["status"], "ok", doc["problems_json"])
        self.assertEqual(data["recipient_name"], "ООО Прогресс Парк")
        self.assertIn("из справочника получателей подставлено", doc["problems_json"])

    # --- дубли ---

    def test_16_dubl_v_pachke_ne_oplachivaetsya_dvazhdy(self):
        """Один счёт, попавший в пачку дважды, даёт одну платёжку, а не две."""
        self.voyti()
        pachka_id = self.zagruzit(["drugoy.pdf", "drugoy.pdf"])

        conn = db.connect()
        docs = conn.execute(
            "SELECT included, problems_json FROM docs WHERE batch_id = ? ORDER BY id",
            (pachka_id,),
        ).fetchall()
        conn.close()

        self.assertEqual([d["included"] for d in docs], [1, 0], "дубль не исключён")
        self.assertIn("дубль", docs[1]["problems_json"])

        stranitsa = self.client.get(f"/pachki/{pachka_id}").text
        self.assertIn("Платёжек к выгрузке: 1", stranitsa)

        put = f"/pachki/{pachka_id}"
        self.client.post(f"{put}/vygruzka", data={"token": self.token(put)}, follow_redirects=False)
        text = self.client.get(f"{put}/fayl").content.decode("cp1251")
        self.assertEqual(
            text.count("СекцияДокумент=Платежное поручение"), 1, "счёт оплачен дважды"
        )

    def test_17_ranee_oplachenny_schet_isklyuchaetsya(self):
        """Счёт из прошлой пачки не уйдёт в банк второй раз."""
        self.voyti()
        pachka_id = self.zagruzit(["drugoy.pdf"])

        conn = db.connect()
        doc = conn.execute(
            "SELECT included, problems_json FROM docs WHERE batch_id = ?", (pachka_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(doc["included"], 0, "ранее оплаченный счёт не исключён")
        self.assertIn("уже выгружался", doc["problems_json"])

    def dokument(self, pachka_id: int):
        conn = db.connect()
        try:
            doc = conn.execute(
                "SELECT * FROM docs WHERE batch_id = ? ORDER BY id", (pachka_id,)
            ).fetchone()
            data = json.loads(doc["data_json"])
            return doc, data
        finally:
            conn.close()

    # --- НДС: выбор бухгалтера и память справочника ---

    def test_20_nds_ne_nayden_vybiraet_buhgalter(self):
        """Счёт про НДС молчит — идёт человеку; человек выбирает «Без НДС» —
        счёт чистый, а в назначении платежа стоит описание из счёта."""
        self.voyti()
        pachka_id = self.zagruzit(["nds1.pdf"])
        doc, _ = self.dokument(pachka_id)
        self.assertEqual(doc["status"], "problem")
        self.assertIn("НДС не найден", doc["problems_json"])

        put = f"/pachki/{pachka_id}"
        stranitsa = self.client.get(put).text
        self.assertIn('<option value="none"', stranitsa, "нет выбора «Без НДС»")
        self.assertIn('<option value="22"', stranitsa, "нет ставки 22 % в списке")

        otvet = self.client.post(
            f"{put}/dokument/{doc['id']}",
            data={
                **{k: v for k, v in SCHET_NDS_NEIZVESTEN_1.items()
                   if not k.startswith("_") and v is not None},
                "vat_rate": "none",
                "vat_amount": "",
                "token": self.token(put),
                "deystvie": "sohranit",
            },
            follow_redirects=True,
        )
        self.assertIn("Платёжек к выгрузке: 1", otvet.text)
        _, data = self.dokument(pachka_id)
        self.assertEqual(data["vat_status"], "none")
        self.assertIsNone(data["vat_rate"])
        # предпросмотр назначения — ровно то, что уйдёт в файл
        self.assertIn("за услуги по договору, Консультация по учёту. Без НДС.", otvet.text)

        self.client.post(f"{put}/vygruzka", data={"token": self.token(put)}, follow_redirects=False)
        text = self.client.get(f"{put}/fayl").content.decode("cp1251")
        self.assertIn(
            "НазначениеПлатежа=Оплата по счету № N-1 от 21.08.2026 за услуги по договору, "
            "Консультация по учёту. Без НДС.",
            text,
        )

    def test_21_nds_beryotsya_iz_istorii_poluchatelya(self):
        """Тому же получателю уже платили без НДС — следующий молчащий счёт
        проходит чистым, с заметкой, а не с вопросом."""
        self.voyti()
        pachka_id = self.zagruzit(["nds2.pdf"])
        doc, data = self.dokument(pachka_id)
        self.assertEqual(doc["status"], "ok", doc["problems_json"])
        self.assertEqual(data["vat_status"], "none")
        self.assertIn("по прошлым счетам этот получатель работает без НДС", doc["problems_json"])
        self.assertNotIn("НДС не найден и", doc["problems_json"])

    def test_22_vybor_stavki_schitaet_summu_nds(self):
        """Бухгалтер выбрал ставку, сумму не вписал — она считается от итога,
        и справочник запоминает новую ставку за получателем."""
        self.voyti()
        pachka_id = self.zagruzit(["nds3.pdf"])
        doc, _ = self.dokument(pachka_id)
        put = f"/pachki/{pachka_id}"
        otvet = self.client.post(
            f"{put}/dokument/{doc['id']}",
            data={
                **{k: v for k, v in SCHET_NDS_NEIZVESTEN_3.items()
                   if not k.startswith("_") and v is not None},
                "vat_rate": "20",
                "vat_amount": "",
                "token": self.token(put),
                "deystvie": "sohranit",
            },
            follow_redirects=True,
        )
        _, data = self.dokument(pachka_id)
        self.assertEqual(data["vat_status"], "included")
        self.assertEqual(data["vat_rate"], "20")
        self.assertEqual(data["vat_amount"], "166.67")
        self.assertIn("В т.ч. НДС 20% - 166.67 руб.", otvet.text)

        conn = db.connect()
        row = conn.execute(
            "SELECT vat_status, vat_rate FROM poluchateli WHERE inn = ?",
            (TIHAYA_GAVAN["recipient_inn"],),
        ).fetchone()
        conn.close()
        self.assertEqual((row["vat_status"], row["vat_rate"]), ("included", "20"))

    # --- плательщик по счетам ---

    def test_23_pachka_na_chuzhogo_pokupatelya_perenaznachaetsya(self):
        """Счета выставлены на организацию, которой нет в справочнике: сервис
        предлагает завести её с заполненными ИНН/КПП/названием, а потом —
        переназначить пачку одной кнопкой."""
        self.voyti()
        pachka_id = self.zagruzit(["chuzhoy.pdf"])
        put = f"/pachki/{pachka_id}"
        stranitsa = self.client.get(put).text
        self.assertIn("Счета выставлены не на этого плательщика", stranitsa)
        self.assertIn("ИНН 1661034770", stranitsa)
        self.assertIn("КПП 166101001", stranitsa)
        self.assertIn(f"/platelshchiki?inn=1661034770&kpp=166101001", stranitsa)
        self.assertNotIn("Переназначить пачку", stranitsa, "плательщика ещё нет — переназначать не на кого")

        # Форма плательщика предзаполнена из счетов
        forma = self.client.get(
            f"/platelshchiki?inn=1661034770&kpp=166101001&name=ООО ТЕТРАКОМ&pachka={pachka_id}"
        ).text
        self.assertIn('value="1661034770"', forma)
        self.assertIn('value="166101001"', forma)
        self.assertIn('value="tetrakom"', forma, "код не предложен по наименованию")
        self.assertIn(f'name="pachka" value="{pachka_id}"', forma)
        self.assertIn(f"из счетов пачки № {pachka_id}", forma)

        # Сохранили — вернулись к пачке
        otvet = self.client.post(
            "/platelshchiki",
            data={**TETRAKOM, "pachka": str(pachka_id), "token": self.token("/platelshchiki")},
            follow_redirects=False,
        )
        self.assertEqual(otvet.status_code, 303, otvet.text[:400])
        self.assertEqual(otvet.headers["location"], put)

        stranitsa = self.client.get(put).text
        self.assertIn("Переназначить пачку на ООО ТЕТРАКОМ", stranitsa)
        nayden = re.search(r'name="schet_id" value="(\d+)"', stranitsa)
        self.assertIsNotNone(nayden, "у нового плательщика один счёт — он должен быть подставлен скрыто")

        otvet = self.client.post(
            f"{put}/perenaznachit",
            data={"token": self.token(put), "schet_id": nayden.group(1)},
            follow_redirects=False,
        )
        self.assertEqual(otvet.status_code, 303, otvet.text[:400])
        self.zhdat(pachka_id)

        doc, _ = self.dokument(pachka_id)
        self.assertEqual(doc["status"], "ok", doc["problems_json"])
        self.assertNotIn("выставлен не нам", doc["problems_json"])
        conn = db.connect()
        pachka = conn.execute(
            "SELECT b.payer_id, p.code FROM batches b JOIN payers p ON p.id = b.payer_id"
            " WHERE b.id = ?", (pachka_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(pachka["code"], "tetrakom")
        stranitsa = self.client.get(put).text
        self.assertNotIn("Счета выставлены не на этого плательщика", stranitsa)
        self.assertIn("Платёжек к выгрузке: 1", stranitsa)

    def test_24_svoi_scheta_ne_predlagayut_perenaznachit(self):
        """Один чужой счёт среди своих — это отдельный счёт, а не ошибка пачки."""
        self.voyti()
        chisty_so_svoim = {**SCHET_CHISTY, "invoice_number": "1400", "_buyer_inn": PLATELSHCHIK["inn"]}
        FAYLY["svoy.pdf"] = chisty_so_svoim
        FAYLY["svoy2.pdf"] = {**chisty_so_svoim, "invoice_number": "1401"}
        try:
            pachka_id = self.zagruzit(["svoy.pdf", "svoy2.pdf", "chuzhoy.pdf"])
        finally:
            FAYLY.pop("svoy.pdf")
            FAYLY.pop("svoy2.pdf")
        stranitsa = self.client.get(f"/pachki/{pachka_id}").text
        self.assertNotIn("Счета выставлены не на этого плательщика", stranitsa)
        self.assertIn("Требуют проверки: 1", stranitsa)

    def test_25_ne_prinyaty_fayl_vidno_v_pachke(self):
        """Слишком тяжёлый файл раньше пропадал молча — теперь он строка в пачке.

        Человек грузил полсотни счетов, получал сорок восемь и не мог понять,
        каких двух не хватает.
        """
        from servis.app import pachki

        self.voyti()
        byl = pachki.PREDEL_FAYLA
        pachki.PREDEL_FAYLA = 64  # чтобы не гонять по тесту настоящие 25 МБ
        # свой номер счёта: иначе он совпадёт с уже выгруженным в прошлом тесте
        FAYLY["godny.pdf"] = {**SCHET_CHISTY, "invoice_number": "2501"}
        try:
            otvet = self.client.post(
                "/pachki/nova",
                data={
                    "token": self.token("/pachki/nova"),
                    "schet_id": str(self.schet_id),
                    "data_platezha": "2026-08-29",
                },
                files=[
                    ("fayly", ("godny.pdf", b"godny.pdf", "application/pdf")),
                    ("fayly", ("ogromny.pdf", b"x" * 200, "application/pdf")),
                ],
                follow_redirects=False,
            )
            self.assertEqual(otvet.status_code, 303, otvet.text[:400])
            pachka_id = int(otvet.headers["location"].rsplit("/", 1)[1])
            self.zhdat(pachka_id)
        finally:
            pachki.PREDEL_FAYLA = byl
            FAYLY.pop("godny.pdf")

        stranitsa = self.client.get(f"/pachki/{pachka_id}").text
        self.assertIn("Не принято файлов: 1", stranitsa)
        self.assertIn("ogromny.pdf", stranitsa)
        self.assertIn("не принят", stranitsa)
        # Годный счёт из той же пачки при этом дошёл до выгрузки
        self.assertIn("Платёжек к выгрузке: 1", stranitsa)

        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT status, stored_name, problems_json FROM docs"
                " WHERE batch_id = ? AND filename = ?",
                (pachka_id, "ogromny.pdf"),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["stored_name"], "", "непринятый файл на диск не сохраняем")
        self.assertIn("МБ", json.loads(row["problems_json"])[0])

    def test_26_ishodnik_ne_prinyatogo_ne_otdaetsya(self):
        """У непринятого файла нет исходника: путь не должен указать на папку пачки."""
        from servis.app import pachki

        self.voyti()
        byl = pachki.PREDEL_FAYLA
        pachki.PREDEL_FAYLA = 64
        try:
            otvet = self.client.post(
                "/pachki/nova",
                data={
                    "token": self.token("/pachki/nova"),
                    "schet_id": str(self.schet_id),
                    "data_platezha": "2026-08-29",
                },
                files=[("fayly", ("tyazhely.pdf", b"x" * 200, "application/pdf"))],
                follow_redirects=False,
            )
            pachka_id = int(otvet.headers["location"].rsplit("/", 1)[1])
            self.zhdat(pachka_id)
        finally:
            pachki.PREDEL_FAYLA = byl

        conn = db.connect()
        try:
            doc_id = conn.execute(
                "SELECT id FROM docs WHERE batch_id = ?", (pachka_id,)
            ).fetchone()["id"]
        finally:
            conn.close()
        otvet = self.client.get(f"/pachki/{pachka_id}/dokument/{doc_id}/ishodnik")
        self.assertEqual(otvet.status_code, 404)


if __name__ == "__main__":
    unittest.main()
