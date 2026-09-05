"""Окно запуска сервиса «Счета в банк».

Расширение .pyw, а не .py: Windows запускает такой файл без чёрного окна
консоли. Всё, что нужно человеку, — дважды щёлкнуть по значку.

Окно нарочно маленькое: одна строка состояния и три кнопки. Работа идёт
в браузере, здесь только «включить» и «выключить».
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

KOREN = Path(__file__).resolve().parent
if str(KOREN) not in sys.path:
    sys.path.insert(0, str(KOREN))

FON = "#f7f7f5"
ZELYONY = "#1f7a3d"
SERY = "#6b6b6b"
KRASNY = "#a4291f"


class Okno:
    def __init__(self) -> None:
        self.servis = None
        self.soobshcheniya: queue.Queue[tuple[str, str]] = queue.Queue()

        self.koren = tk.Tk()
        self.koren.title("Счета в банк")
        self.koren.configure(bg=FON)
        self.koren.resizable(False, False)
        self.koren.protocol("WM_DELETE_WINDOW", self.zakryt)

        ramka = tk.Frame(self.koren, bg=FON, padx=28, pady=22)
        ramka.pack()

        tk.Label(
            ramka, text="Счета → банк", bg=FON, font=("Segoe UI", 17, "bold")
        ).pack(anchor="w")
        tk.Label(
            ramka,
            text="Загрузите счета — получите файл платёжек для клиент-банка.",
            bg=FON,
            fg=SERY,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 16))

        self.sostoyanie = tk.Label(
            ramka, text="Сервис остановлен", bg=FON, fg=SERY, font=("Segoe UI", 10)
        )
        self.sostoyanie.pack(anchor="w")

        self.polosa = ttk.Progressbar(ramka, mode="indeterminate", length=330)

        knopki = tk.Frame(ramka, bg=FON)
        knopki.pack(anchor="w", pady=(16, 0))
        self.knopka_pusk = ttk.Button(knopki, text="Запустить", command=self.zapustit)
        self.knopka_pusk.pack(side="left")
        self.knopka_otkryt = ttk.Button(
            knopki, text="Открыть в браузере", command=self.otkryt, state="disabled"
        )
        self.knopka_otkryt.pack(side="left", padx=8)
        self.knopka_stop = ttk.Button(
            knopki, text="Остановить", command=self.ostanovit, state="disabled"
        )
        self.knopka_stop.pack(side="left")

        self.podskazka = tk.Label(
            ramka, text="", bg=FON, fg=SERY, font=("Segoe UI", 8), wraplength=360,
            justify="left",
        )
        self.podskazka.pack(anchor="w", pady=(14, 0))

        self.koren.after(100, self._razobrat_ochered)
        self.koren.after(300, self.zapustit)  # запускаем сами: человек пришёл работать

    # --- сообщения из рабочих потоков ---

    def _skazat(self, vid: str, text: str) -> None:
        self.soobshcheniya.put((vid, text))

    def _razobrat_ochered(self) -> None:
        try:
            while True:
                vid, text = self.soobshcheniya.get_nowait()
                if vid == "sostoyanie":
                    self.sostoyanie.config(text=text, fg=SERY)
                elif vid == "rabotaet":
                    self._pokazat_rabotu(text)
                elif vid == "oshibka":
                    self._pokazat_oshibku(text)
                elif vid == "podskazka":
                    self.podskazka.config(text=text)
        except queue.Empty:
            pass
        self.koren.after(100, self._razobrat_ochered)

    def _zhdat(self, vklyuchit: bool) -> None:
        if vklyuchit:
            self.polosa.pack(anchor="w", pady=(10, 0))
            self.polosa.start(12)
        else:
            self.polosa.stop()
            self.polosa.pack_forget()

    def _pokazat_rabotu(self, adres: str) -> None:
        self._zhdat(False)
        self.sostoyanie.config(text=f"Работает: {adres}", fg=ZELYONY)
        self.knopka_pusk.config(state="disabled")
        self.knopka_otkryt.config(state="normal")
        self.knopka_stop.config(state="normal")
        self.podskazka.config(
            text="Окно можно свернуть — сервис работает, пока оно открыто. "
            "Закрыть окно = остановить сервис."
        )

    def _pokazat_oshibku(self, text: str) -> None:
        self._zhdat(False)
        self.sostoyanie.config(text="Не запустилось", fg=KRASNY)
        self.knopka_pusk.config(state="normal")
        self.knopka_otkryt.config(state="disabled")
        self.knopka_stop.config(state="disabled")
        self.podskazka.config(text="")
        messagebox.showerror("Счета в банк", text)

    # --- действия ---

    def zapustit(self) -> None:
        self.knopka_pusk.config(state="disabled")
        self._zhdat(True)
        self._skazat("sostoyanie", "Запускаем…")
        threading.Thread(target=self._zapustit_v_potoke, daemon=True).start()

    def _zapustit_v_potoke(self) -> None:
        try:
            from servis import zapuskator

            nedostayet = zapuskator.est_zavisimosti()
            if nedostayet:
                self._skazat("sostoyanie", "Доустанавливаем недостающее…")
                self._postavit(nedostayet)

            self.servis = zapuskator.Servis()
            adres = self.servis.zapustit()
            if not self.servis.zhdat_gotovnosti():
                raise RuntimeError("сервис не ответил за отведённое время")
            self._skazat("rabotaet", adres)
            webbrowser.open(adres)
        except Exception as exc:
            self.servis = None
            self._skazat("oshibka", f"{exc}\n\nЕсли не удаётся — покажите это разработчику.")

    def _postavit(self, pakety: list[str]) -> None:
        """Библиотеки ставятся молча, окном, а не командной строкой."""
        itog = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
             "-r", str(KOREN / "servis" / "requirements.txt")],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if itog.returncode != 0:
            hvost = (itog.stderr or itog.stdout or "").strip().splitlines()[-5:]
            raise RuntimeError(
                "не удалось доустановить: " + ", ".join(pakety) + "\n\n" + "\n".join(hvost)
            )

    def otkryt(self) -> None:
        if self.servis is not None and self.servis.rabotaet:
            webbrowser.open(self.servis.adres)

    def ostanovit(self) -> None:
        self._zhdat(True)
        self._skazat("sostoyanie", "Останавливаем…")
        threading.Thread(target=self._ostanovit_v_potoke, daemon=True).start()

    def _ostanovit_v_potoke(self) -> None:
        if self.servis is not None:
            self.servis.ostanovit()
            self.servis = None
        self._skazat("sostoyanie", "Сервис остановлен")
        self.soobshcheniya.put(("podskazka", ""))
        self.koren.after(0, self._posle_ostanovki)

    def _posle_ostanovki(self) -> None:
        self._zhdat(False)
        self.knopka_pusk.config(state="normal")
        self.knopka_otkryt.config(state="disabled")
        self.knopka_stop.config(state="disabled")

    def zakryt(self) -> None:
        if self.servis is not None and self.servis.rabotaet:
            if not messagebox.askokcancel(
                "Счета в банк",
                "Закрыть окно и остановить сервис?\n\n"
                "Если кто-то сейчас работает в браузере, работа прервётся.",
            ):
                return
            self.servis.ostanovit(sekund=5)
        self.koren.destroy()

    def rabotat(self) -> None:
        self.koren.mainloop()


if __name__ == "__main__":
    Okno().rabotat()
