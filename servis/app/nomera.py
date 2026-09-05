"""Журнал нумерации платёжек поверх базы.

Замена файлового NumberRegistry из части 1. Правила нумерации не меняются
(RESHENIE-numeratsiya.md): счётчик по ключу (плательщик, расчётный счёт, год),
номер выдаётся в момент выгрузки, повторная выгрузка той же пачки даёт те же
номера, запрещённые ЦБ номера перешагиваются.

Меняется только одно — где журнал лежит. В файле он годился для одного человека
за одним компьютером. На сервере сотрудников несколько, и два одновременных
нажатия «Сформировать» обязаны получить разные номера. Поэтому счётчик живёт
в базе, а вызывающий код обязан держать открытой транзакцию `db.transaction`:
методы `assign` пишут в базу и полагаются на её блокировку.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date

from . import db
from .yadro import Invoice, Payer, check_payment_number

MAX_NOMER = 999999


class NumberingError(RuntimeError):
    pass


class SqlRegistry:
    """Тот же интерфейс, что у файлового журнала, но с хранением в базе.

    Экземпляр создаётся на одну выгрузку: он знает, чья это компания, какая
    пачка и кто её выгружает, — этого нет в интерфейсе генератора, а в журнале
    видеть нужно.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        company_id: int,
        batch_id: int | None = None,
        user_id: int | None = None,
    ):
        self.conn = conn
        self.company_id = company_id
        self.batch_id = batch_id
        self.user_id = user_id
        self.summa = "0"

    # --- ключи ---

    @staticmethod
    def counter_key(payer: Payer, year: int) -> str:
        return f"{payer.id}|{payer.account}|{year}"

    # --- выдача номеров ---

    def _next_free(self, key: str, start: int) -> int:
        row = self.conn.execute(
            "SELECT last_number FROM counters WHERE company_id = ? AND key = ?",
            (self.company_id, key),
        ).fetchone()
        candidate = start if row is None else max(int(row["last_number"]) + 1, start)
        while check_payment_number(candidate):  # запрещённые ЦБ номера перешагиваем
            candidate += 1
            if candidate > MAX_NOMER:
                raise NumberingError(
                    f"диапазон нумерации исчерпан для {key}: номер не помещается в 6 знаков. "
                    "Понизьте начальный номер у этой организации."
                )
        return candidate

    def assign(self, payer: Payer, invoice: Invoice, payment_date: date) -> int:
        identity = invoice.identity(payer)
        row = self.conn.execute(
            "SELECT number FROM assigned WHERE company_id = ? AND identity = ?",
            (self.company_id, identity),
        ).fetchone()
        if row is not None:
            return int(row["number"])  # тот же счёт — тот же номер

        key = self.counter_key(payer, payment_date.year)
        number = self._next_free(key, payer.numbering_start)
        self.conn.execute(
            "INSERT INTO counters (company_id, key, last_number) VALUES (?, ?, ?)"
            " ON CONFLICT (company_id, key) DO UPDATE SET last_number = excluded.last_number",
            (self.company_id, key, number),
        )
        self.conn.execute(
            "INSERT INTO assigned (company_id, identity, number, payload, batch_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.company_id,
                identity,
                number,
                json.dumps(
                    {
                        "date": payment_date.isoformat(),
                        "payer_id": payer.id,
                        "payer_account": payer.account,
                        "recipient_inn": invoice.recipient_inn,
                        "recipient_name": invoice.recipient_name,
                        "invoice_number": invoice.invoice_number,
                        "invoice_date": invoice.invoice_date.isoformat(),
                        "amount": f"{invoice.total_amount:.2f}",
                    },
                    ensure_ascii=False,
                ),
                self.batch_id,
                db.now(),
            ),
        )
        return number

    def already_paid(self, payer: Payer, invoice: Invoice) -> dict | None:
        """Был ли этот счёт уже выгружен — защита от двойной оплаты."""
        row = self.conn.execute(
            "SELECT number, payload FROM assigned WHERE company_id = ? AND identity = ?",
            (self.company_id, invoice.identity(payer)),
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row["payload"])
        data["number"] = row["number"]
        return data

    def record_export(self, file_name: str, numbers: list[int], payer: Payer) -> None:
        self.conn.execute(
            "INSERT INTO exports (company_id, batch_id, user_id, file_name, numbers, amount,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.company_id,
                self.batch_id,
                self.user_id,
                file_name,
                json.dumps(numbers),
                self.summa,
                db.now(),
            ),
        )

    def save(self) -> None:
        """Файловому журналу здесь нужно было записать себя на диск; базе — нет.

        Фиксация происходит при выходе из `db.transaction` вызывающего кода.
        """
