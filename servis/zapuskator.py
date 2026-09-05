"""Запуск и остановка сервиса из программы, а не из командной строки.

Отделено от окна с кнопками намеренно: здесь нет ничего от графики, поэтому
эту часть можно проверить тестами, а окно остаётся тонким.
"""
from __future__ import annotations

import socket
import threading
from pathlib import Path

KOREN = Path(__file__).resolve().parent.parent
PORT_PO_UMOLCHANIYU = 8000
PORTOV_PODRYAD = 20


def svobodny_port(nachalo: int = PORT_PO_UMOLCHANIYU) -> int:
    """Первый свободный порт начиная с заданного.

    Занятый порт — обычное дело: сервис уже запущен в другом окне, или порт
    занял чужой сервер. Молча падать с «address already in use» человеку,
    который просто дважды щёлкнул по значку, нельзя.
    """
    for port in range(nachalo, nachalo + PORTOV_PODRYAD):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise OSError(f"свободный порт не нашёлся: {nachalo}—{nachalo + PORTOV_PODRYAD - 1}")


class Servis:
    """Сервис, живущий в отдельном потоке этого же процесса."""

    def __init__(self, host: str = "127.0.0.1", port: int | None = None) -> None:
        self.host = host
        self.port = port
        self._server = None
        self._potok: threading.Thread | None = None

    @property
    def rabotaet(self) -> bool:
        return self._potok is not None and self._potok.is_alive()

    @property
    def adres(self) -> str:
        return f"http://{self.host}:{self.port}"

    def zapustit(self) -> str:
        """Поднять сервис. Возвращает адрес, по которому он отвечает."""
        if self.rabotaet:
            return self.adres

        import uvicorn

        self.port = self.port or svobodny_port()
        config = uvicorn.Config(
            "servis.app.main:app",
            host=self.host,
            port=self.port,
            log_config=None,  # свой журнал не нужен: окно показывает состояние
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        # Обработчики сигналов ставятся только в главном потоке, а мы не в нём
        self._server.install_signal_handlers = lambda: None
        self._potok = threading.Thread(target=self._server.run, daemon=True)
        self._potok.start()
        return self.adres

    def zhdat_gotovnosti(self, sekund: float = 30.0) -> bool:
        """Дождаться, пока сервис начнёт отвечать, — до этого браузер открывать рано."""
        import time

        do = time.monotonic() + sekund
        while time.monotonic() < do:
            if self._server is not None and getattr(self._server, "started", False):
                return True
            if not self.rabotaet:
                return False
            time.sleep(0.05)
        return False

    def ostanovit(self, sekund: float = 10.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._potok is not None:
            self._potok.join(timeout=sekund)
        self._server = None
        self._potok = None


def est_zavisimosti() -> list[str]:
    """Чего не хватает для запуска. Пусто — значит всё на месте."""
    nuzhno = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "jinja2": "jinja2",
        "multipart": "python-multipart",
        "pdfplumber": "pdfplumber",
    }
    net = []
    for modul, paket in nuzhno.items():
        try:
            __import__(modul)
        except ImportError:
            net.append(paket)
    return net
