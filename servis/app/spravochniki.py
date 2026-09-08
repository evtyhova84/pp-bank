"""Справочники: плательщики со счетами, получатели, сотрудники, журнал."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse

from . import auth, db, iz_platezhki, poluchateli as spravochnik_poluchateley, veb
from .yadro import payer_iz_stroki

router = APIRouter()

# Организация как таковая
POLYA_ORGANIZATSII = (
    ("code", "Код (латиницей, для имени файла)"),
    ("name", "Краткое наименование"),
    ("full_name", "Полное наименование"),
    ("inn", "ИНН"),
    ("kpp", "КПП"),
)

# Её реквизиты в конкретном банке
POLYA_SCHETA = (
    ("metka", "Метка счёта (Точка, Сбербанк)"),
    ("account", "Расчётный счёт"),
    ("bank_name", "Банк"),
    ("bank_city", "Город банка"),
    ("bic", "БИК"),
    ("corr_account", "Корр. счёт"),
)

OBYAZATELNYE = {"code", "name", "inn", "account", "bank_name", "bic", "corr_account"}


class _Stroka(dict):
    """Словарь, который ведёт себя как строка базы: недостающее — пустая строка."""

    def __getitem__(self, key):
        return super().get(key, "")


def _proverit(dannye: dict) -> list[str]:
    """Реквизиты проверяются теми же контрольными суммами, что и в банке."""
    from generator.validate import validate_payer

    stroka = _Stroka(
        {
            **dannye,
            "numbering_start": dannye.get("numbering_start") or 900001,
            "bank_profile": dannye.get("bank_profile") or "default",
        }
    )
    try:
        payer = payer_iz_stroki(stroka, stroka)
    except (ValueError, TypeError) as exc:
        return [str(exc)]
    return [str(p) for p in validate_payer(payer) if p.level == "error"]


def _spisok_platelshchikov(conn: sqlite3.Connection, company_id: int) -> list[dict]:
    """Плательщики, у каждого — его расчётные счета."""
    spisok = []
    for payer in conn.execute(
        "SELECT * FROM payers WHERE company_id = ? ORDER BY active DESC, name",
        (company_id,),
    ).fetchall():
        scheta = conn.execute(
            "SELECT * FROM payer_accounts WHERE payer_id = ? ORDER BY active DESC, metka",
            (payer["id"],),
        ).fetchall()
        spisok.append({**dict(payer), "scheta": scheta})
    return spisok


def _stranitsa_platelshchikov(
    request, conn, polzovatel, oshibki=(), znacheniya=None, soobshchenie=None,
    prochitano=(), drugaya=None,
):
    return veb.stranitsa(
        request,
        "platelshchiki.html",
        polzovatel=polzovatel,
        spisok=_spisok_platelshchikov(conn, polzovatel["company_id"]),
        polya_organizatsii=POLYA_ORGANIZATSII,
        polya_scheta=POLYA_SCHETA,
        obyazatelnye=OBYAZATELNYE,
        oshibki=list(oshibki),
        znacheniya=znacheniya or {},
        soobshchenie=soobshchenie,
        prochitano=list(prochitano),
        # вторая сторона платёжки: если наша организация — она, человек переключит
        drugaya=drugaya,
        polya_storony=POLYA_STORONY,
    )


# Что переносится из одной стороны платёжки в форму и обратно
POLYA_STORONY = tuple(iz_platezhki.PUSTO) + ("_storona",)


def _dopolnit_iz_platezhki(znacheniya: dict) -> None:
    """Чего в документе не бывает, а в форме нужно: код, метка, начальный номер."""
    znacheniya.setdefault("numbering_start", 900001)
    znacheniya.setdefault("metka", znacheniya.get("bank_name", ""))
    if not znacheniya.get("code"):
        znacheniya["code"] = iz_platezhki.kod_iz_imeni(znacheniya.get("name", ""))


# --- плательщики ---


@router.get("/platelshchiki", response_class=HTMLResponse)
def platelshchiki(
    request: Request,
    polzovatel=Depends(veb.tekushchiy),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    """Справочник; с параметрами — форма, предзаполненная покупателем из пачки.

    Сюда ведёт кнопка «завести плательщика» с экрана пачки, счета которой
    выставлены на организацию, которой в справочнике ещё нет. Наименование,
    ИНН и КПП уже прочитаны из счетов; банковских реквизитов в счёте не бывает,
    их дописывает человек.
    """
    zapros = request.query_params
    znacheniya: dict = {}
    soobshchenie = None
    if zapros.get("inn"):
        znacheniya = {
            "inn": zapros.get("inn", "").strip()[:12],
            "kpp": zapros.get("kpp", "").strip()[:9],
            "full_name": zapros.get("name", "").strip()[:200],
            "name": iz_platezhki._kratkoe(zapros.get("name", "").strip()[:200]),
        }
        znacheniya["code"] = iz_platezhki.kod_iz_imeni(znacheniya["name"])
        pachka = zapros.get("pachka", "")
        if pachka.isdigit():
            znacheniya["pachka"] = int(pachka)
        nedostayet = ", ".join(iz_platezhki.chego_ne_hvataet(znacheniya))
        soobshchenie = (
            f"Наименование, ИНН и КПП взяты из счетов пачки № {pachka or '?'}. "
            f"Банковских реквизитов в счёте нет — допишите {nedostayet} из платёжки "
            "или выписки этой организации и сохраните. Потом на экране пачки появится "
            "кнопка «переназначить»."
        )
    return _stranitsa_platelshchikov(
        request, conn, polzovatel, znacheniya=znacheniya, soobshchenie=soobshchenie
    )


def _sobrat(forma, polya) -> dict:
    return {pole: (forma.get(pole) or "").strip() for pole, _ in polya}


@router.post("/platelshchiki")
async def dobavit_platelshchika(
    request: Request,
    token: str = Form(...),
    polzovatel=Depends(veb.tolko_admin),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    """Новая организация вместе с её первым расчётным счётом."""
    veb.proverit_formu(request, token)
    forma = await request.form()
    dannye = {**_sobrat(forma, POLYA_ORGANIZATSII), **_sobrat(forma, POLYA_SCHETA)}
    dannye["numbering_start"] = (forma.get("numbering_start") or "900001").strip()
    # Пришли с экрана пачки — после сохранения вернёмся к ней
    pachka = (forma.get("pachka") or "").strip()
    if pachka.isdigit():
        dannye["pachka"] = int(pachka)

    oshibki = []
    if not dannye["code"] or not dannye["code"].replace("-", "").replace("_", "").isalnum():
        oshibki.append("код: только латинские буквы, цифры, дефис и подчёркивание")
    try:
        dannye["numbering_start"] = int(dannye["numbering_start"])
    except ValueError:
        oshibki.append("начальный номер платёжек должен быть числом")
        dannye["numbering_start"] = 900001
    oshibki += _proverit(dannye)
    if oshibki:
        return _stranitsa_platelshchikov(request, conn, polzovatel, oshibki, dannye)

    try:
        with db.transaction(conn):
            cursor = conn.execute(
                "INSERT INTO payers (company_id, code, name, full_name, inn, kpp, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    polzovatel["company_id"],
                    dannye["code"],
                    dannye["name"],
                    dannye["full_name"],
                    dannye["inn"],
                    dannye["kpp"],
                    db.now(),
                ),
            )
            _vstavit_schet(conn, int(cursor.lastrowid), dannye)
            db.log(
                conn,
                polzovatel["company_id"],
                polzovatel["id"],
                "добавлен плательщик",
                dannye["name"],
            )
    except sqlite3.IntegrityError:
        return _stranitsa_platelshchikov(
            request,
            conn,
            polzovatel,
            [f"плательщик с кодом «{dannye['code']}» уже есть"],
            dannye,
        )
    if dannye.get("pachka"):
        return veb.tuda(f"/pachki/{dannye['pachka']}")
    return veb.tuda("/platelshchiki")


def _vstavit_schet(conn: sqlite3.Connection, payer_id: int, dannye: dict) -> None:
    conn.execute(
        "INSERT INTO payer_accounts (payer_id, metka, account, bank_name, bank_city, bic,"
        " corr_account, numbering_start, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            payer_id,
            dannye.get("metka") or dannye.get("bank_name", ""),
            dannye["account"],
            dannye["bank_name"],
            dannye.get("bank_city", ""),
            dannye["bic"],
            dannye["corr_account"],
            int(dannye.get("numbering_start") or 900001),
            db.now(),
        ),
    )


@router.post("/platelshchiki/{payer_id}/schet")
async def dobavit_schet(
    request: Request,
    payer_id: int,
    token: str = Form(...),
    polzovatel=Depends(veb.tolko_admin),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    """Ещё один расчётный счёт у существующей организации."""
    veb.proverit_formu(request, token)
    payer = conn.execute(
        "SELECT * FROM payers WHERE id = ? AND company_id = ?",
        (payer_id, polzovatel["company_id"]),
    ).fetchone()
    if payer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "плательщик не найден")

    forma = await request.form()
    dannye = _sobrat(forma, POLYA_SCHETA)
    dannye["numbering_start"] = (forma.get("numbering_start") or "900001").strip()
    try:
        dannye["numbering_start"] = int(dannye["numbering_start"])
    except ValueError:
        dannye["numbering_start"] = 900001

    # Проверяем счёт вместе с организацией: контрольные суммы смотрят и на ИНН
    oshibki = _proverit({**dict(payer), **dannye})
    if oshibki:
        return _stranitsa_platelshchikov(request, conn, polzovatel, oshibki)

    try:
        with db.transaction(conn):
            _vstavit_schet(conn, payer_id, dannye)
            db.log(
                conn,
                polzovatel["company_id"],
                polzovatel["id"],
                "добавлен счёт плательщика",
                f"{payer['name']}: {dannye['account']}",
            )
    except sqlite3.IntegrityError:
        return _stranitsa_platelshchikov(
            request, conn, polzovatel, [f"счёт {dannye['account']} у этой организации уже есть"]
        )
    return veb.tuda("/platelshchiki")


@router.post("/platelshchiki/iz-platezhki")
async def iz_platezhki_forma(
    request: Request,
    token: str = Form(...),
    fayl: UploadFile = File(...),
    polzovatel=Depends(veb.tolko_admin),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    """Прочитать реквизиты из старой платёжки и подставить их в форму.

    Ничего не сохраняем: показываем заполненную форму, человек сверяет и жмёт
    «Сохранить». Реквизиты, по которым уходят деньги, не должны попадать
    в справочник без взгляда живого человека.
    """
    veb.proverit_formu(request, token)
    soderzhimoe = await fayl.read()

    znacheniya: dict = {}
    oshibki: list[str] = []
    soobshchenie = None
    prochitano: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        put = Path(tmp) / (Path(fayl.filename or "platezhka").name or "platezhka")
        put.write_bytes(soderzhimoe)
        try:
            znacheniya = iz_platezhki.razobrat(fayl.filename or "", soderzhimoe, put)
        except iz_platezhki.NeRazobrano as exc:
            oshibki = [str(exc)]
            # Показываем, что именно прочиталось: иначе человек упирается
            # в отказ и не может ни понять причину, ни рассказать о ней.
            prochitano = exc.stroki
        except Exception as exc:  # движок чтения не осилил файл
            oshibki = [f"файл не прочитался: {exc}"]

    drugaya = None
    if znacheniya:
        drugaya = znacheniya.pop("_drugaya_storona", None)
        pochemu = znacheniya.pop("_pochemu", "")
        _dopolnit_iz_platezhki(znacheniya)
        nedostayet = iz_platezhki.chego_ne_hvataet(znacheniya)

        if znacheniya.get("_istochnik") == "schet":
            # Из счёта берётся только «кто платит». Банковских реквизитов
            # плательщика в счёте не печатают — там реквизиты того, кому платят.
            oshibki = []
            soobshchenie = (
                "Из счёта прочитаны наименование, ИНН и КПП. Банковских реквизитов "
                "в счёте нет — их не печатают: в счёте стоят реквизиты того, кому "
                "платят. Допишите " + ", ".join(nedostayet) + " руками или загрузите "
                "сюда платёжку либо выписку из клиент-банка."
            )
        else:
            # Те же контрольные суммы, что и при сохранении: пусть человек сразу
            # видит, что прочиталось криво, а не после нажатия «Сохранить».
            oshibki = _proverit(znacheniya)
            soobshchenie = (
                "Реквизиты прочитаны. Сверьте их со своим документом и нажмите «Сохранить»."
                if not oshibki
                else "Прочиталось не всё — допишите недостающее руками."
            )
            if drugaya:
                # В платёжке две стороны, и мы выбрали одну по признакам.
                # Говорим, какую и почему, — и даём переключить одной кнопкой.
                soobshchenie = (
                    f"В платёжке две стороны: {znacheniya.get('_storona', 'плательщик')} "
                    f"«{znacheniya.get('name', '')}» и {drugaya.get('_storona', '')} "
                    f"«{drugaya.get('name', '')}». В форму подставлен "
                    f"{znacheniya.get('_storona', 'плательщик')}"
                    + (f": {pochemu}" if pochemu else "")
                    + ". Если ваша организация — вторая сторона, нажмите кнопку ниже. "
                    + soobshchenie
                )

    return _stranitsa_platelshchikov(
        request, conn, polzovatel, oshibki, znacheniya, soobshchenie, prochitano, drugaya
    )


@router.post("/platelshchiki/storona")
async def drugaya_storona(
    request: Request,
    token: str = Form(...),
    polzovatel=Depends(veb.tolko_admin),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    """Человек сказал: наша организация — вторая сторона платёжки.

    Ничего не читаем заново: обе стороны пришли скрытыми полями формы,
    просто меняем их местами и снова проверяем контрольными суммами.
    """
    veb.proverit_formu(request, token)
    forma = await request.form()
    znacheniya = {pole: (forma.get(f"vybor_{pole}") or "").strip() for pole in POLYA_STORONY}
    drugaya = {pole: (forma.get(f"drugaya_{pole}") or "").strip() for pole in POLYA_STORONY}
    if not znacheniya.get("inn"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "вторая сторона платёжки пуста")
    znacheniya["code"] = ""
    _dopolnit_iz_platezhki(znacheniya)
    oshibki = _proverit(znacheniya)
    soobshchenie = (
        f"Подставлен {znacheniya.get('_storona') or 'другая сторона'} «{znacheniya['name']}». "
        + (
            "Сверьте реквизиты со своим документом и нажмите «Сохранить»."
            if not oshibki
            else "Прочиталось не всё — допишите недостающее руками."
        )
    )
    return _stranitsa_platelshchikov(
        request, conn, polzovatel, oshibki, znacheniya, soobshchenie,
        drugaya=drugaya if drugaya.get("inn") else None,
    )


@router.post("/platelshchiki/{payer_id}/perekluchit")
def perekluchit_platelshchika(
    request: Request,
    payer_id: int,
    token: str = Form(...),
    polzovatel=Depends(veb.tolko_admin),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    veb.proverit_formu(request, token)
    with db.transaction(conn):
        conn.execute(
            "UPDATE payers SET active = 1 - active WHERE id = ? AND company_id = ?",
            (payer_id, polzovatel["company_id"]),
        )
    return veb.tuda("/platelshchiki")


@router.post("/scheta/{schet_id}/perekluchit")
def perekluchit_schet(
    request: Request,
    schet_id: int,
    token: str = Form(...),
    polzovatel=Depends(veb.tolko_admin),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    veb.proverit_formu(request, token)
    with db.transaction(conn):
        conn.execute(
            "UPDATE payer_accounts SET active = 1 - active WHERE id = ? AND payer_id IN"
            " (SELECT id FROM payers WHERE company_id = ?)",
            (schet_id, polzovatel["company_id"]),
        )
    return veb.tuda("/platelshchiki")


# --- получатели ---


@router.get("/poluchateli", response_class=HTMLResponse)
def poluchateli(
    request: Request,
    polzovatel=Depends(veb.tekushchiy),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    spisok = spravochnik_poluchateley.spisok(conn, polzovatel["company_id"])
    return veb.stranitsa(
        request,
        "poluchateli.html",
        polzovatel=polzovatel,
        spisok=spisok,
        dvoyniki=spravochnik_poluchateley.dvoyniki(spisok),
    )


# --- сотрудники ---


@router.get("/sotrudniki", response_class=HTMLResponse)
def sotrudniki(
    request: Request,
    polzovatel=Depends(veb.tolko_admin),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    spisok = conn.execute(
        "SELECT * FROM users WHERE company_id = ? ORDER BY active DESC, login",
        (polzovatel["company_id"],),
    ).fetchall()
    return veb.stranitsa(
        request, "sotrudniki.html", polzovatel=polzovatel, spisok=spisok, oshibka=None
    )


@router.post("/sotrudniki")
def dobavit_sotrudnika(
    request: Request,
    token: str = Form(...),
    login: str = Form(...),
    parol: str = Form(...),
    imya: str = Form(""),
    rol: str = Form("operator"),
    polzovatel=Depends(veb.tolko_admin),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    veb.proverit_formu(request, token)
    try:
        with db.transaction(conn):
            auth.create_user(conn, polzovatel["company_id"], login, parol, imya, rol)
            db.log(conn, polzovatel["company_id"], polzovatel["id"], "добавлен сотрудник", login)
    except auth.AuthError as exc:
        spisok = conn.execute(
            "SELECT * FROM users WHERE company_id = ? ORDER BY active DESC, login",
            (polzovatel["company_id"],),
        ).fetchall()
        return veb.stranitsa(
            request, "sotrudniki.html", polzovatel=polzovatel, spisok=spisok, oshibka=str(exc)
        )
    return veb.tuda("/sotrudniki")


@router.post("/sotrudniki/{user_id}/perekluchit")
def perekluchit_sotrudnika(
    request: Request,
    user_id: int,
    token: str = Form(...),
    polzovatel=Depends(veb.tolko_admin),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    veb.proverit_formu(request, token)
    if user_id == polzovatel["id"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "нельзя отключить самого себя")
    with db.transaction(conn):
        conn.execute(
            "UPDATE users SET active = 1 - active WHERE id = ? AND company_id = ?",
            (user_id, polzovatel["company_id"]),
        )
        # Отключили — выкидываем и из открытых сессий, не дожидаясь их истечения
        conn.execute(
            "DELETE FROM sessions WHERE user_id IN"
            " (SELECT id FROM users WHERE id = ? AND active = 0)",
            (user_id,),
        )
    return veb.tuda("/sotrudniki")


@router.post("/sotrudniki/{user_id}/rol")
def smenit_rol(
    request: Request,
    user_id: int,
    token: str = Form(...),
    polzovatel=Depends(veb.tolko_admin),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    veb.proverit_formu(request, token)
    # Себя не трогаем: разжаловав самого себя, администратор потеряет доступ
    # к этой же странице, и вернуть роль будет некому.
    if user_id == polzovatel["id"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "нельзя сменить роль самому себе")
    est = conn.execute(
        "SELECT role FROM users WHERE id = ? AND company_id = ?",
        (user_id, polzovatel["company_id"]),
    ).fetchone()
    if est is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "сотрудник не найден")
    novaya = "operator" if est["role"] == "admin" else "admin"
    with db.transaction(conn):
        conn.execute(
            "UPDATE users SET role = ? WHERE id = ? AND company_id = ?",
            (novaya, user_id, polzovatel["company_id"]),
        )
        db.log(
            conn,
            polzovatel["company_id"],
            polzovatel["id"],
            "смена роли",
            f"{user_id} -> {auth.ROLES[novaya]}",
        )
    return veb.tuda("/sotrudniki")


@router.post("/sotrudniki/{user_id}/parol")
def smenit_parol(
    request: Request,
    user_id: int,
    token: str = Form(...),
    parol: str = Form(...),
    polzovatel=Depends(veb.tolko_admin),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    veb.proverit_formu(request, token)
    est = conn.execute(
        "SELECT id FROM users WHERE id = ? AND company_id = ?",
        (user_id, polzovatel["company_id"]),
    ).fetchone()
    if est is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "сотрудник не найден")
    try:
        with db.transaction(conn):
            auth.set_password(conn, user_id, parol)
            db.log(conn, polzovatel["company_id"], polzovatel["id"], "смена пароля", str(user_id))
    except auth.AuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    return veb.tuda("/sotrudniki")


# --- журнал ---


@router.get("/zhurnal", response_class=HTMLResponse)
def zhurnal(
    request: Request,
    polzovatel=Depends(veb.tekushchiy),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    vygruzki = conn.execute(
        "SELECT e.*, u.login AS avtor, p.name AS payer_name FROM exports e"
        " JOIN users u ON u.id = e.user_id"
        " JOIN batches b ON b.id = e.batch_id"
        " JOIN payers p ON p.id = b.payer_id"
        " WHERE e.company_id = ? ORDER BY e.id DESC LIMIT 200",
        (polzovatel["company_id"],),
    ).fetchall()
    stroki = []
    for row in vygruzki:
        nomera_spisok = json.loads(row["numbers"] or "[]")
        stroki.append(
            {
                **dict(row),
                "kolichestvo": len(nomera_spisok),
                "diapazon": (
                    f"{nomera_spisok[0]}—{nomera_spisok[-1]}" if nomera_spisok else "—"
                ),
            }
        )
    deystviya = conn.execute(
        "SELECT a.*, u.login FROM audit a LEFT JOIN users u ON u.id = a.user_id"
        " WHERE a.company_id = ? ORDER BY a.id DESC LIMIT 100",
        (polzovatel["company_id"],),
    ).fetchall()
    return veb.stranitsa(
        request, "zhurnal.html", polzovatel=polzovatel, vygruzki=stroki, deystviya=deystviya
    )
