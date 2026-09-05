"""Сервис «счёт -> платёжка»: приложение и вход.

Запуск для проверки:
    python -m servis            (из корня проекта)
Разворачивание на сервере — servis/README.md
"""
from __future__ import annotations

import sqlite3

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, db, spravochniki, veb
from .pachki import router as router_pachki
from .veb import Vhod

app = FastAPI(title="Счета в банк", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(veb.BASE / "static")), name="static")
app.include_router(router_pachki)
app.include_router(spravochniki.router)


@app.on_event("startup")
def podgotovit() -> None:
    db.init()
    conn = db.connect()
    try:
        auth.drop_expired(conn)
    finally:
        conn.close()


@app.exception_handler(Vhod)
def na_vhod(request: Request, exc: Vhod) -> RedirectResponse:
    """Не вошёл — показываем вход, а не голую ошибку 401."""
    return veb.tuda("/vhod")


@app.get("/", response_class=HTMLResponse)
def korenj(polzovatel=Depends(veb.tekushchiy)) -> RedirectResponse:
    return veb.tuda("/pachki")


def _nikogo_net(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0


@app.get("/nachalo", response_class=HTMLResponse)
def nachalo_forma(request: Request, conn: sqlite3.Connection = Depends(veb.baza)):
    """Первый запуск: заводим себя прямо в браузере, без чёрного окна."""
    if not _nikogo_net(conn):
        return veb.tuda("/vhod")
    return veb.stranitsa(request, "nachalo.html", oshibka=None, znacheniya={})


@app.post("/nachalo", response_class=HTMLResponse)
def nachalo(
    request: Request,
    login: str = Form(...),
    parol: str = Form(...),
    parol2: str = Form(...),
    imya: str = Form(""),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    if not _nikogo_net(conn):
        # Второй раз этой страницей воспользоваться нельзя: иначе любой прохожий
        # завёл бы себе администратора на работающем сервисе.
        return veb.tuda("/vhod")

    znacheniya = {"login": login, "imya": imya}
    if parol != parol2:
        return veb.stranitsa(
            request, "nachalo.html", oshibka="пароли не совпали", znacheniya=znacheniya
        )
    try:
        with db.transaction(conn):
            # Рабочее место заводится само. Организации, от чьего имени платят,
            # к нему отношения не имеют: их бухгалтер выбирает в момент платежа.
            cursor = conn.execute(
                "INSERT INTO companies (name, created_at) VALUES (?, ?)",
                (f"Рабочее место: {imya or login}", db.now()),
            )
            company_id = int(cursor.lastrowid)
            auth.create_user(conn, company_id, login, parol, imya, role="admin")
    except auth.AuthError as exc:
        return veb.stranitsa(
            request, "nachalo.html", oshibka=str(exc), znacheniya=znacheniya
        )

    token = auth.login(conn, login, parol, ip=veb.adres(request))
    return veb.postavit_kuku(veb.tuda("/platelshchiki"), token, request)


@app.get("/vhod", response_class=HTMLResponse)
def vhod_forma(request: Request, conn: sqlite3.Connection = Depends(veb.baza)):
    if auth.user_by_token(conn, request.cookies.get(auth.SESSION_COOKIE)):
        return veb.tuda("/pachki")
    if _nikogo_net(conn):
        return veb.tuda("/nachalo")
    return veb.stranitsa(request, "vhod.html", oshibka=None)


@app.post("/vhod", response_class=HTMLResponse)
def vhod(
    request: Request,
    login: str = Form(...),
    parol: str = Form(...),
    conn: sqlite3.Connection = Depends(veb.baza),
):
    # Без общей транзакции намеренно: неудачную попытку надо записать, а откат
    # транзакции стёр бы саму запись, и счётчик всегда оставался бы нулевым.
    # Соединение работает в режиме автофиксации, `login` пишет что нужно сам.
    try:
        token = auth.login(conn, login, parol, ip=veb.adres(request))
    except auth.AuthError as exc:
        return veb.stranitsa(request, "vhod.html", oshibka=str(exc))

    return veb.postavit_kuku(veb.tuda("/pachki"), token, request)


@app.post("/vyhod")
def vyhod(request: Request, conn: sqlite3.Connection = Depends(veb.baza)):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        with db.transaction(conn):
            auth.logout(conn, token)
    otvet = veb.tuda("/vhod")
    otvet.delete_cookie(auth.SESSION_COOKIE)
    return otvet
