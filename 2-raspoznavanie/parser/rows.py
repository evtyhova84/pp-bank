"""Извлечение текста из PDF построчно, с восстановлением строк по координатам.

Колонки с итогами в этих счетах регулярно разъезжаются при плоском извлечении
текста, поэтому строки собираются по координате Y, а не берутся как есть.

Движки:
  pdfplumber — основной, лицензия MIT: продукт можно отдавать сторонним организациям;
  pymupdf    — если установлен; быстрее, но AGPL, поэтому не по умолчанию;
  pdftotext  — запасной, внешняя утилита poppler, режим -layout, без координат.

Здесь же единственная точка подключения распознавания картинок: read_cells
отдаёт одинаковый вид данных независимо от того, PDF это или скан, — поэтому
разборка полей ниже по течению одна на всех.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import ocr as ocr_engine
from .norm import clean

ROW_TOLERANCE = 3.0  # пункты; слова с близким Y считаем одной строкой


class ExtractionError(RuntimeError):
    pass


# Доля кегля: пробел уже этой ширины напечатан между буквами одного слова,
# а не между словами. Такие пробелы в этих счетах ставит генератор PDF.
FAKE_SPACE_RATIO = 0.2

# Доля кегля: разрыв шире этого — граница ячеек, даже если пробела в тексте нет.
# Так соседние колонки не слипаются в одно слово.
GAP_RATIO = 0.8

# Каким движком читать PDF. По умолчанию — лицензионно чистый pdfplumber.
DVIZHOK = os.environ.get("PP_PDF_ENGINE", "pdfplumber").lower()


def _span_text(span: dict) -> str:
    """Текст спана без пробелов нулевой ширины (движок pymupdf)."""
    chars = span.get("chars")
    if not chars:
        return ""
    limit = span.get("size", 10.0) * FAKE_SPACE_RATIO
    parts = []
    for index, char in enumerate(chars):
        symbol = char["c"]
        if symbol.isspace():
            x0 = char["bbox"][0]
            following = chars[index + 1]["bbox"][0] if index + 1 < len(chars) else char["bbox"][2]
            if following - x0 < limit:
                continue  # ложный пробел внутри слова
        parts.append(symbol)
    return "".join(parts)


def _cells_pymupdf(path: Path) -> list[list[tuple[float, str]]]:
    import pymupdf

    out: list[list[tuple[float, str]]] = []
    with pymupdf.open(path) as doc:
        for page in doc:
            buckets: dict[int, list[tuple[float, str]]] = {}
            for block in page.get_text("rawdict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        text = _span_text(span)
                        if not text.strip():
                            continue
                        x0, y0 = span["bbox"][0], span["bbox"][1]
                        buckets.setdefault(round(y0 / ROW_TOLERANCE), []).append((x0, clean(text)))
            for key in sorted(buckets):
                row = [(x, t) for x, t in sorted(buckets[key]) if t]
                if row:
                    out.append(row)
    return out


def _tokens_iz_simvolov(chars: list[dict]) -> list[tuple[float, str]]:
    """Символы одной строки -> ячейки (X, текст).

    Ячейка разрывается на настоящем пробеле или на широком разрыве между буквами.
    Пробелы нулевой ширины выбрасываются: ими некоторые генераторы PDF разбивают
    слова внутри, и ОБЩЕСТВО приезжает как ОБЩ ЕСТВО.
    """
    cells: list[tuple[float, str]] = []
    bukvy: list[str] = []
    nachalo: float | None = None
    predydushchiy: dict | None = None

    def sbros() -> None:
        nonlocal bukvy, nachalo, predydushchiy
        if bukvy:
            text = clean("".join(bukvy))
            if text:
                cells.append((nachalo if nachalo is not None else 0.0, text))
        bukvy = []
        nachalo = None
        predydushchiy = None

    for index, char in enumerate(chars):
        symbol = char.get("text", "")
        if not symbol:
            continue
        kegl = float(char.get("size") or 10.0)

        if symbol.isspace():
            sleduyushchiy = chars[index + 1]["x0"] if index + 1 < len(chars) else char["x1"]
            if float(sleduyushchiy) - float(char["x0"]) < kegl * FAKE_SPACE_RATIO:
                continue  # ложный пробел внутри слова
            sbros()
            continue

        if predydushchiy is not None:
            razryv = float(char["x0"]) - float(predydushchiy["x1"])
            if razryv > kegl * GAP_RATIO:
                sbros()  # разрыв шире буквы — соседняя колонка, а не то же слово

        if nachalo is None:
            nachalo = float(char["x0"])
        bukvy.append(symbol)
        predydushchiy = char

    sbros()
    return cells


def _cells_pdfplumber(path: Path) -> list[list[tuple[float, str]]]:
    import pdfplumber

    out: list[list[tuple[float, str]]] = []
    with pdfplumber.open(path) as doc:
        for page in doc.pages:
            buckets: dict[int, list[dict]] = {}
            for char in page.chars:
                buckets.setdefault(round(float(char["top"]) / ROW_TOLERANCE), []).append(char)
            for key in sorted(buckets):
                stroka = sorted(buckets[key], key=lambda c: float(c["x0"]))
                cells = _tokens_iz_simvolov(stroka)
                if cells:
                    out.append(cells)
    return out


def _rows_pdftotext(path: Path) -> list[str]:
    exe = shutil.which("pdftotext")
    if not exe:
        raise ExtractionError("не найден ни один движок чтения PDF")
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "out.txt"
        subprocess.run(
            [exe, "-enc", "UTF-8", "-layout", str(path), str(target)],
            check=True,
            capture_output=True,
        )
        text = target.read_text(encoding="utf-8", errors="replace")
    return [clean(line) for line in text.splitlines() if clean(line)]


def _dostupnye() -> list[str]:
    """Движки в порядке предпочтения — какие из них вообще установлены."""
    poryadok = [DVIZHOK] + [d for d in ("pdfplumber", "pymupdf") if d != DVIZHOK]
    est = []
    for imya in poryadok:
        try:
            __import__(imya)
        except ImportError:
            continue
        if imya not in est:
            est.append(imya)
    if shutil.which("pdftotext"):
        est.append("pdftotext")
    return est


def engine_name() -> str:
    dostupno = _dostupnye()
    return dostupno[0] if dostupno else "нет движка"


def _cells_pdf(path: Path) -> list[list[tuple[float, str]]]:
    oshibki = []
    for imya in _dostupnye():
        try:
            if imya == "pdfplumber":
                return _cells_pdfplumber(path)
            if imya == "pymupdf":
                return _cells_pymupdf(path)
            return [[(0.0, row)] for row in _rows_pdftotext(path)]
        except ImportError:
            continue
        except Exception as exc:  # движок не осилил файл — пробуем следующий
            oshibki.append(f"{imya}: {exc}")
    if oshibki:
        raise ExtractionError("; ".join(oshibki))
    raise ExtractionError("не найден ни один движок чтения PDF")


def read_cells(
    path: str | Path, ocr_dvizhok: str | None = None
) -> list[list[tuple[float, str]]]:
    """Строки документа с координатами X — нужны, чтобы разделить колонки.

    Запасной движок координат не даёт: там каждая строка — одна ячейка.
    `ocr_dvizhok` задаётся явно, а не через окружение: документы разбираются
    в несколько потоков, и общая переменная сделала бы выбор движка гонкой.
    """
    path = Path(path)
    if not path.exists():
        raise ExtractionError(f"файл не найден: {path}")

    if ocr_engine.is_image(path):
        # Картинка: текстового слоя нет, распознаём и чиним числовые места
        from .ocr_repair import repair_cells

        try:
            return repair_cells(ocr_engine.cells(path, engine=ocr_dvizhok))
        except (ocr_engine.OcrUnavailable, ocr_engine.OcrFailed) as exc:
            raise ExtractionError(str(exc)) from exc

    cells = _cells_pdf(path)
    if not cells:
        raise ExtractionError(f"в {path.name} нет текстового слоя — это скан, нужен OCR")
    return cells


def read_rows(path: str | Path, ocr_dvizhok: str | None = None) -> list[str]:
    """Строки документа сверху вниз."""
    return [clean(" ".join(t for _, t in row)) for row in read_cells(path, ocr_dvizhok)]
