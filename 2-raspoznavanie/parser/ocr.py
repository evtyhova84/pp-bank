"""Распознавание счетов-картинок: первая ступень.

Движков два, оба локальные — документ не уходит наружу:

  tesseract — основной. Ставится отдельно, зато работает и на Windows, и на Linux,
              лицензия Apache 2.0, языковые данные для русского идут в комплекте.
  windows   — встроенный `Windows.Media.Ocr`: ничего ставить не надо, но есть
              только на Windows, и на Windows Server языковой пакет бывает не
              установлен. Годится как запасной.

Оба отдают строки с координатами, то есть ровно тот же вид, что и слой PDF, —
поэтому вся дальнейшая разборка полей не меняется.

Если контрольные суммы на распознанном не сходятся, счёт помечается к эскалации:
его смотрит человек или, по настройке, внешняя vision-модель (см. vision.py).
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import subprocess
from pathlib import Path

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
SCRIPT = Path(__file__).resolve().parent / "win_ocr.ps1"
TIMEOUT_SECONDS = 120

# auto — взять тот движок, который есть в системе; можно принудить через окружение
DVIZHOK = os.environ.get("PP_OCR_ENGINE", "auto").lower()

# Куда обычно ставится Tesseract на Windows, если его нет в PATH
TESSERACT_PUTI = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)

# Ниже этой уверенности слово почти всегда мусор от шума на фото.
# Порог низкий намеренно: лучше пропустить кривую цифру в проверку контрольных
# сумм, чем выбросить её и получить «поле не найдено» без объяснения.
MIN_CONF = 30.0


class OcrUnavailable(RuntimeError):
    pass


class OcrFailed(RuntimeError):
    pass


def is_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_SUFFIXES


# --- поиск движков ---


def _powershell() -> str:
    for name in ("powershell.exe", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    raise OcrUnavailable("не найден powershell — встроенный OCR Windows недоступен")


def tesseract_exe() -> str | None:
    found = shutil.which("tesseract")
    if found:
        return found
    for put in TESSERACT_PUTI:
        if Path(put).exists():
            return put
    return None


def tessdata() -> str | None:
    """Где лежат языковые данные, если не там, куда их кладёт установщик.

    Русского языка в установщике Tesseract по умолчанию нет, а дописать файл
    в Program Files можно только администратором. Поэтому язык кладётся рядом
    с проектом, и путь передаётся движку явно: сервис на сервере может работать
    под ограниченной учётной записью, и прав на Program Files у него не будет.
    """
    zadano = os.environ.get("PP_TESSDATA")
    if zadano and Path(zadano).is_dir():
        return zadano
    ryadom = Path(__file__).resolve().parent.parent / "tessdata"
    return str(ryadom) if ryadom.is_dir() else None


def dostupnye_dvizhki() -> list[str]:
    """Движки в порядке предпочтения.

    Встроенный в Windows идёт первым там, где он есть: на реальных сканах счетов
    он дал 33 ключевых поля из 40 против 24 у Tesseract — он аккуратнее с
    рамками таблиц, а починка цифр в ocr_repair настроена на его огрехи.
    Tesseract остаётся основным движком на сервере без Windows OCR и второй
    попыткой, когда первая не сошлась по контрольным суммам.
    """
    est = []
    if SCRIPT.exists() and (shutil.which("powershell.exe") or shutil.which("pwsh")):
        est.append("windows")
    if tesseract_exe():
        est.append("tesseract")
    return est


def vybrat_dvizhok() -> str:
    est = dostupnye_dvizhki()
    if DVIZHOK != "auto":
        if DVIZHOK not in est:
            raise OcrUnavailable(
                f"движок распознавания «{DVIZHOK}» недоступен; найдены: {', '.join(est) or 'ни одного'}"
            )
        return DVIZHOK
    if not est:
        raise OcrUnavailable(
            "не найден движок распознавания картинок. Установите Tesseract "
            "(https://github.com/UB-Mannheim/tesseract/wiki) с русским языком "
            "или включите языковой пакет OCR в Windows."
        )
    return est[0]


# --- подготовка картинки ---


# До какой стороны увеличивать фото. Телефон отдаёт счёт как 960×1280, и в шапке
# с реквизитами кегль выходит в 8–10 пикселей — оба движка на таком читают
# «ьик» вместо «БИК» и теряют целые клетки. На трёхкратном увеличении того же
# снимка встроенный движок прочитал шапку целиком: проверено на фото, где
# без увеличения не нашлись ни счёт, ни БИК, ни корр. счёт.
TSELEVAYA_STORONA = 2500
MAX_MNOZHITEL = 4


def mnozhitel_dlya(shirina: int, vysota: int) -> int:
    """Во сколько раз увеличить: до целевой стороны, но не больше чем вчетверо."""
    korotkaya = min(shirina, vysota)
    if korotkaya <= 0 or korotkaya >= TSELEVAYA_STORONA:
        return 1
    return min(MAX_MNOZHITEL, -(-TSELEVAYA_STORONA // korotkaya))


def _podgotovit(path: Path, tmp_dir: Path) -> Path:
    """Мелкое фото распознаётся заметно хуже — увеличиваем и обесцвечиваем.

    Без Pillow шаг просто пропускается: он улучшает результат, но не обязателен.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return path

    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert("L")
            shirina, vysota = img.size
            mnozhitel = mnozhitel_dlya(shirina, vysota)
            if mnozhitel > 1:
                img = img.resize((shirina * mnozhitel, vysota * mnozhitel), Image.LANCZOS)
            target = tmp_dir / "podgotovleno.png"
            img.save(target)
            return target
    except Exception:
        return path  # подготовка — не обязательный шаг, оригинал тоже сгодится


# --- движок tesseract ---


def _tesseract_recognize(path: Path, language: str) -> dict:
    exe = tesseract_exe()
    if not exe:
        raise OcrUnavailable("tesseract не найден")

    yazyk = {"ru": "rus", "ru-RU": "rus"}.get(language, language)
    psm = os.environ.get("PP_OCR_PSM", "3")  # 3 — автоматическая разметка страницы

    import tempfile

    komanda = [exe, "", "stdout", "-l", yazyk, "--psm", psm, "tsv"]
    dannye = tessdata()
    if dannye:
        komanda[3:3] = ["--tessdata-dir", dannye]

    with tempfile.TemporaryDirectory() as tmp:
        kartinka = _podgotovit(path, Path(tmp))
        komanda[1] = str(kartinka)
        try:
            completed = subprocess.run(komanda, capture_output=True, timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise OcrFailed(
                f"распознавание {path.name} не уложилось в {TIMEOUT_SECONDS} с"
            ) from exc

    if completed.returncode != 0:
        oshibka = completed.stderr.decode("utf-8", errors="replace").strip()
        posledn = oshibka.splitlines()[-1] if oshibka else "?"
        if "Failed loading language" in oshibka or "tessdata" in oshibka:
            raise OcrUnavailable(
                f"в tesseract не установлен язык «{yazyk}»: {posledn}. "
                "Доустановите языковой пакет russian."
            )
        raise OcrFailed(f"tesseract вернул ошибку: {posledn}")

    return _razobrat_tsv(completed.stdout.decode("utf-8", errors="replace"), path)


def _razobrat_tsv(tsv: str, path: Path) -> dict:
    """TSV tesseract -> слова с координатами.

    Слова, а не строки: собственная разбивка на строки у tesseract на счетах
    ненадёжна — соседние ячейки таблицы он охотно склеивает в одну строку.
    Полосы мы соберём сами, по перекрытию, тем же кодом, что и для Windows OCR.
    """
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)
    lines = []
    uverennost = []
    shirina = 0
    for row in reader:
        try:
            uroven = int(row.get("level") or 0)
        except ValueError:
            continue
        if uroven == 1:  # страница целиком — берём её ширину
            shirina = max(shirina, int(float(row.get("width") or 0)))
        if uroven != 5:  # 5 — отдельное слово
            continue
        # Линии таблицы движок отдаёт как символы рамки и лепит их к соседним
        # словам: «[БИК», «| 041117601», «Сч. № [30101810…». Метки и числа
        # после этого не находятся, поэтому рамку срезаем.
        text = (row.get("text") or "").strip().strip("[]|¦")
        if not text:
            continue
        try:
            conf = float(row.get("conf") or -1)
        except ValueError:
            conf = -1.0
        if conf < MIN_CONF:
            continue
        uverennost.append(conf)
        lines.append(
            {
                "x": float(row["left"]),
                "y": float(row["top"]),
                "h": float(row["height"]),
                "text": text,
            }
        )

    if not lines:
        raise OcrFailed(f"на изображении {path.name} не найдено текста")

    return {
        "engine": "tesseract",
        "lines": lines,
        "width": shirina,
        "confidence": round(sum(uverennost) / len(uverennost), 1) if uverennost else 0.0,
    }


# --- движок Windows ---


def _windows_recognize(path: Path, language: str) -> dict:
    if not SCRIPT.exists():
        raise OcrUnavailable(f"нет скрипта распознавания: {SCRIPT}")

    import tempfile

    # Та же подготовка, что и для tesseract: раньше встроенный движок получал
    # оригинал, и на фото с телефона терял шапку с реквизитами целиком.
    with tempfile.TemporaryDirectory() as tmp:
        kartinka = _podgotovit(path, Path(tmp))
        try:
            completed = subprocess.run(
                [
                    _powershell(),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(SCRIPT),
                    "-Path",
                    str(kartinka.resolve()),
                    "-Language",
                    language,
                ],
                capture_output=True,
                timeout=TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise OcrFailed(
                f"распознавание {path.name} не уложилось в {TIMEOUT_SECONDS} с"
            ) from exc

    if completed.returncode != 0:
        error = completed.stderr.decode("cp866", errors="replace").strip()
        raise OcrFailed(f"движок OCR вернул ошибку: {error.splitlines()[-1] if error else '?'}")

    out = completed.stdout.decode("utf-8-sig", errors="replace").strip()
    if not out:
        raise OcrFailed("движок OCR ничего не вернул")
    try:
        result = json.loads(out)
    except json.JSONDecodeError as exc:
        raise OcrFailed(f"не разобран ответ движка OCR: {out[:200]}") from exc
    result.setdefault("engine", "windows")
    return result


# --- общий вход ---


def recognize(path: str | Path, language: str = "ru", engine: str | None = None) -> dict:
    """Строки с координатами: {'engine':…, 'lines':[{'x','y','h','text'}, …]}."""
    path = Path(path)
    imya = engine or vybrat_dvizhok()
    if imya == "tesseract":
        return _tesseract_recognize(path, language)
    if imya == "windows":
        return _windows_recognize(path, language)
    raise OcrUnavailable(f"неизвестный движок распознавания: {imya}")


def cells(
    path: str | Path, language: str = "ru", engine: str | None = None
) -> list[list[tuple[float, str]]]:
    """То же представление, что даёт слой PDF: строки из сегментов (x, текст)."""
    result = recognize(path, language=language, engine=engine)
    lines = result.get("lines") or []
    if not lines:
        raise OcrFailed(f"на изображении {Path(path).name} не найдено текста")

    width = result.get("width") or 0
    limit = result.get("max_dimension") or 0
    if limit and width > limit:
        raise OcrFailed(
            f"изображение шире предела движка ({width} > {limit}) — уменьшите картинку"
        )

    return group_by_overlap(lines)


OVERLAP_SHARE = 0.5


def group_by_overlap(lines: list[dict]) -> list[list[tuple[float, str]]]:
    """Строки одной высоты объединяются, только если их полосы реально перекрываются.

    Склейка по «y с допуском» здесь не годится: подписи «В том числе НДС» и
    «Всего к оплате» идут вплотную, и грубый допуск смешивает их суммы — НДС
    оказывается равен итогу. Перекрытие полос такой ошибки не даёт.
    """
    boxes = []
    for line in lines:
        text = (line.get("text") or "").strip()
        if not text:
            continue
        top = float(line["y"])
        height = float(line.get("h") or 0) or 10.0
        boxes.append((top, top + height, float(line["x"]), text))
    boxes.sort()

    rows: list[list[tuple[float, str]]] = []
    spans: list[tuple[float, float]] = []
    for top, bottom, x, text in boxes:
        height = bottom - top
        if rows:
            row_top, row_bottom = spans[-1]
            overlap = min(bottom, row_bottom) - max(top, row_top)
            if overlap >= OVERLAP_SHARE * min(height, row_bottom - row_top):
                rows[-1].append((x, text))
                spans[-1] = (min(top, row_top), max(bottom, row_bottom))
                continue
        rows.append([(x, text)])
        spans.append((top, bottom))
    return [sorted(row) for row in rows]
