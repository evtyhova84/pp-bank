"""Нумерация платёжных поручений.

Решение зафиксировано в RESHENIE-numeratsiya.md:
- выделенный верхний диапазон, чтобы не пересечься с собственной нумерацией 1С;
- счётчик по ключу (плательщик, расчётный счёт, год);
- номер присваивается в момент выгрузки, а не при распознавании;
- повторная выгрузка той же пачки даёт те же номера.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .models import Invoice, Payer
from .validate import check_payment_number


class NumberingError(RuntimeError):
    pass


class NumberRegistry:
    """Журнал нумерации на диске: счётчики + закреплённые за счетами номера."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data = {"counters": {}, "assigned": {}, "log": []}
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
            self._data.setdefault("counters", {})
            self._data.setdefault("assigned", {})
            self._data.setdefault("log", [])

    # --- ключи ---

    @staticmethod
    def counter_key(payer: Payer, year: int) -> str:
        return f"{payer.id}|{payer.account}|{year}"

    # --- выдача номеров ---

    def _next_free(self, key: str, start: int) -> int:
        current = self._data["counters"].get(key)
        candidate = start if current is None else current + 1
        if candidate < start:
            candidate = start
        # перешагиваем номера, запрещённые правилами ЦБ (например, X00000)
        while check_payment_number(candidate):
            candidate += 1
            if candidate > 999999:
                raise NumberingError(
                    f"диапазон нумерации исчерпан для {key}: номер не помещается в 6 знаков. "
                    "Понизьте numbering_start для этой организации."
                )
        return candidate

    def assign(self, payer: Payer, invoice: Invoice, payment_date: date) -> int:
        """Присвоить номер счёту. Повторный вызов возвращает тот же номер."""
        identity = invoice.identity(payer)
        existing = self._data["assigned"].get(identity)
        if existing is not None:
            return int(existing["number"])

        key = self.counter_key(payer, payment_date.year)
        number = self._next_free(key, payer.numbering_start)
        self._data["counters"][key] = number
        self._data["assigned"][identity] = {
            "number": number,
            "date": payment_date.isoformat(),
            "payer_id": payer.id,
            "payer_account": payer.account,
            "recipient_inn": invoice.recipient_inn,
            "recipient_name": invoice.recipient_name,
            "invoice_number": invoice.invoice_number,
            "invoice_date": invoice.invoice_date.isoformat(),
            "amount": f"{invoice.total_amount:.2f}",
        }
        return number

    def record_export(self, file_name: str, numbers: list[int], payer: Payer) -> None:
        self._data["log"].append(
            {
                "file": file_name,
                "payer_id": payer.id,
                "payer_account": payer.account,
                "numbers": numbers,
            }
        )

    def already_paid(self, payer: Payer, invoice: Invoice) -> dict | None:
        """Был ли этот счёт уже выгружен раньше — защита от двойной оплаты."""
        return self._data["assigned"].get(invoice.identity(payer))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
