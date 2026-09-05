"""Общая оснастка веб-слоя: шаблоны, текущий пользователь, защита форм."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from pathlib import Path

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import auth, db

BASE = Path(__file__).resolve().parent
shablony = Jinja2Templates(directory=str(BASE / "templates"))

SEKRET_FAYL = db.DATA_DIR / "sekret.key"


def sekret() -> bytes:
    """Ключ подписи форм. Создаётся при первом запуске и больше не меняется."""
    if not SEKRET_FAYL.exists():
        SEKRET_FAYL.parent.mkdir(parents=True, exist_ok=True)
        SEKRET_FAYL.write_text(secrets.token_hex(32), encoding="utf-8")
        try:  # на Linux прикроем файл от чужих глаз; на Windows права другие
            SEKRET_FAYL.chmod(0o600)
        except OSError:
            pass
    return SEKRET_FAYL.read_text(encoding="utf-8").strip().encode("ascii")


def token_formy(sessiya: str) -> str:
    """Метка формы, привязанная к сессии, — чтобы её нельзя было отправить со стороны."""
    return hmac.new(sekret(), sessiya.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def proverit_formu(request: Request, prislano: str) -> None:
    sessiya = request.cookies.get(auth.SESSION_COOKIE, "")
    if not hmac.compare_digest(token_formy(sessiya), prislano or ""):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "форма устарела, обновите страницу")


# --- соединение с базой на запрос ---


def baza() -> sqlite3.Connection:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


class Vhod(HTTPException):
    """Не вошёл — не ошибка, а повод показать страницу входа."""

    def __init__(self) -> None:
        super().__init__(status.HTTP_401_UNAUTHORIZED, "нужно войти")


def tekushchiy(request: Request, conn: sqlite3.Connection = Depends(baza)) -> sqlite3.Row:
    polzovatel = auth.user_by_token(conn, request.cookies.get(auth.SESSION_COOKIE))
    if polzovatel is None:
        raise Vhod()
    return polzovatel


def tolko_admin(polzovatel: sqlite3.Row = Depends(tekushchiy)) -> sqlite3.Row:
    if polzovatel["role"] != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "нужны права администратора")
    return polzovatel


def stranitsa(request: Request, imya: str, polzovatel=None, **kontekst):
    kontekst.setdefault("polzovatel", polzovatel)
    if polzovatel is not None:
        sessiya = request.cookies.get(auth.SESSION_COOKIE, "")
        kontekst.setdefault("token", token_formy(sessiya))
    return shablony.TemplateResponse(request, imya, kontekst)


def postavit_kuku(otvet, token: str, request: Request):
    """Кука сессии. Одна на все места, где человек оказывается вошедшим."""
    otvet.set_cookie(
        auth.SESSION_COOKIE,
        token,
        max_age=auth.SESSION_DAYS * 24 * 3600,
        httponly=True,
        samesite="strict",
        # Ставится только при работе по HTTPS: на голом HTTP браузер такую куку
        # не пришлёт обратно, и войти будет невозможно.
        secure=request.url.scheme == "https",
    )
    return otvet


LOKALNYE = {"127.0.0.1", "::1", "localhost"}


def adres(request: Request) -> str:
    """Адрес того, кто пришёл, — для счётчика неудачных входов.

    Сервис работает за обратным прокси, поэтому настоящий адрес приходит
    заголовком. Верим заголовку только если запрос пришёл с этой же машины,
    то есть от нашего прокси: иначе адрес можно было бы подделать и обнулять
    себе счётчик попыток на каждом запросе.
    """
    pryamoy = request.client.host if request.client else ""
    if pryamoy in LOKALNYE:
        cherez = request.headers.get("x-forwarded-for", "")
        if cherez:
            return cherez.split(",")[0].strip()[:64]
    return pryamoy[:64]


def tuda(put: str) -> RedirectResponse:
    """Переход после POST: 303, иначе браузер повторит отправку формы."""
    return RedirectResponse(put, status_code=status.HTTP_303_SEE_OTHER)
