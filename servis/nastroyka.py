"""Первичная настройка из командной строки: создать первого администратора.

    python -m servis.nastroyka

Обычно она не нужна: при первом запуске сервис сам предложит завести себя
в браузере. Этот способ остаётся для сервера без графики.

Название организации не спрашивается. Организации, от чьего имени уходят
деньги, — это плательщики, их бухгалтер выбирает в момент платежа, и у одного
человека их бывает много.
"""
from __future__ import annotations

import getpass
import sys

from .app import auth, db


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    db.init()
    conn = db.connect()
    try:
        est = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if est and "--esli-nuzhno" in argv:
            return 0  # уже настроено — так вызывает zapusk.bat, это не ошибка
        if est and "--esche" not in argv:
            print(
                "В базе уже есть пользователи. Заводите сотрудников в интерфейсе,\n"
                "раздел «Сотрудники». Если нужна ещё одна организация — запустите\n"
                "с флагом --esche.",
                file=sys.stderr,
            )
            return 1

        print("Настройка сервиса «Счета в банк»\n")
        login = input("Логин администратора: ").strip()
        imya = input("Имя администратора: ").strip()
        parol = getpass.getpass("Пароль (от 8 знаков): ")
        if parol != getpass.getpass("Пароль ещё раз: "):
            print("пароли не совпали", file=sys.stderr)
            return 1

        with db.transaction(conn):
            cursor = conn.execute(
                "INSERT INTO companies (name, created_at) VALUES (?, ?)",
                (f"Рабочее место: {imya or login}", db.now()),
            )
            company_id = int(cursor.lastrowid)
            auth.create_user(conn, company_id, login, parol, imya, role="admin")
    except auth.AuthError as exc:
        print(f"не получилось: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nотменено", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(f"\nГотово, администратор «{login}».")
    print("Запуск сервиса:  python -m servis")
    print("Дальше: заведите плательщика в разделе «Плательщики».")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
