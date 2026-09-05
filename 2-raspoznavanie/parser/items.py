"""Табличная часть счёта — ради одной строки «за что платим».

Полноценный разбор позиций (слой 2б) здесь не делается: платёжке не нужны
ни цены, ни количества. Нужно только краткое описание, чтобы в назначении
платежа стояло не голое «Оплата по счету № 12», а «за дюбель-гвоздь 6х40,
эмаль аэрозольную». Поэтому вытаскиваем наименования позиций и отдаём их
строкой через «; » — генератор сам уложит их в предел длины назначения.

Строки таблицы у разных программ устроены одинаково: заголовок
«№ Товары (работы, услуги) Кол-во Ед. Цена Сумма», затем позиции, потом
«Итого». Наименование при этом может переноситься на соседние строки —
и выше строки с числами (1С), и ниже (СБИС). Где начинается новая позиция,
надёжнее всего говорит её порядковый номер: строка, начинающаяся с «2»,
открывает вторую позицию, только если первая уже есть. Иначе «148 на ЖК…»
из адреса доставки считалось бы сто сорок восьмой позицией.
"""
from __future__ import annotations

import re

from .norm import clean

# Заголовок таблицы: слово про товар/услугу и слово про количество/цену/сумму
HEADER_RE = re.compile(r"наименование|товар|услуг|работ", re.IGNORECASE)
HEADER_COLUMNS_RE = re.compile(r"кол|цена|сумма|стоимость", re.IGNORECASE)

# Конец таблицы
STOP_RE = re.compile(
    r"^\s*(итого|всего|в том числе|в т\.\s*ч\.|сумма ндс|без ндс|ндс\s|к оплате|"
    r"всего наименований|на сумму)",
    re.IGNORECASE,
)

# Обрывки заголовка, разъехавшегося по строкам: «изм.», «рения», «товара чество»
HEADER_WORDS = {
    "№", "n", "наименование", "товар", "товара", "товары", "товаров", "работы", "работ",
    "услуги", "услуг", "кол-во", "кол", "коли-", "коли", "чество", "количество",
    "ед", "изм", "изме-", "рения", "единица", "измерения", "цена", "сумма", "стоимость",
    "ндс", "ставка", "артикул", "код", "руб", "%", "(работы,", "услуги)", "без",
}

# Подвал, приклеившийся к последней позиции: «Создано при помощи business.tbank.ru»
FOOTER_RE = re.compile(r"создано|www\.|\.ru\b|\.com\b|http", re.IGNORECASE)

# Сумма в конце строки: «2 800,00», «191 332,00 ₽». Разряды разделяются
# пробелом по три, поэтому «…400.00 2 800.00» — две суммы, а не «00 2 800.00».
AMOUNT_TAIL_RE = re.compile(r"(?<![\d.,])\d+(?:\s\d{3})*[.,]\d{2}\s*(?:₽|руб\.?|р\.)?\s*$")
VAT_COLUMN_RE = re.compile(r"\s*(без\s+ндс|ндс\s*\d{1,2}\s*%|\d{1,2}\s*%)\s*$", re.IGNORECASE)
UNITS = (
    "шт", "штук", "шт/уп", "час", "ч", "часов", "усл", "услуга", "услуг", "ед", "компл", "кг",
    "г", "т", "м", "м2", "м3", "мм", "см", "п.м", "пог.м", "м.п", "мес", "месяц", "смен",
    "смена", "смены", "уп", "упак", "л", "км", "пара", "пар", "рул", "кор", "лист", "листов",
    "дн", "дней", "день", "сут", "рейс", "рейсов", "изм", "работа", "тыс", "руб", "%",
)
UNIT_RE = re.compile(
    r"(?<![А-Яа-яЁёA-Za-z])(?:"
    + "|".join(re.escape(u) for u in sorted(UNITS, key=len, reverse=True))
    + r")\.?\s*$",
    re.IGNORECASE,
)
# Единица перед количеством — отдельным словом: «новая мес 1». Не «55х95см»
UNIT_WORD_RE = re.compile(
    r"(?:^|(?<=\s))(?:"
    + "|".join(re.escape(u) for u in sorted(UNITS, key=len, reverse=True))
    + r")\.?\s*$",
    re.IGNORECASE,
)
QUANTITY_RE = re.compile(r"(?<![\d.,])\d{1,3}(?:\s\d{3})*(?:[.,]\d+)?\s*$")
INDEX_RE = re.compile(r"^\s*(\d{1,3})\s*[.)]?\s*(?=[^\d\s]|$)")
LETTERS_RE = re.compile(r"[А-Яа-яЁёA-Za-z]")


def _is_header_fragment(row: str) -> bool:
    words = row.lower().replace(",", " ").split()
    return bool(words) and all(w.strip(".") in HEADER_WORDS or w in HEADER_WORDS for w in words)


def _strip_tail(row: str) -> tuple[str, bool]:
    """Снять с конца строки суммы, ставку НДС, единицу и количество.

    Возвращает (наименование, это строка позиции). Строкой позиции считаем ту,
    где стояли суммы и единица измерения либо хотя бы две суммы: одна сумма
    без единицы бывает и в тексте наименования («аванс 5 000,00 по договору»).
    """
    amounts = 0
    while True:
        cut = AMOUNT_TAIL_RE.sub("", row)
        if cut != row:
            row, amounts = cut, amounts + 1
            continue
        cut = VAT_COLUMN_RE.sub("", row)
        if cut != row:
            row = cut
            continue
        break
    had_unit = False
    for _ in range(2):  # единиц бывает две подряд: «усл. ед», «Ед. изм.»
        cut = UNIT_RE.sub("", row)
        if cut == row:
            break
        row, had_unit = cut, True
    is_item = amounts >= 2 or (amounts >= 1 and had_unit)
    if is_item:
        row = QUANTITY_RE.sub("", row)
        # единица бывает и перед количеством: «… новая мес 1 15000,00»
        row = UNIT_WORD_RE.sub("", row)
    return row.strip(" ,;"), is_item


def _index_of(row: str) -> int | None:
    match = INDEX_RE.match(row)
    return int(match.group(1)) if match else None


def _strip_index(row: str) -> str:
    return INDEX_RE.sub("", row, count=1)


def _looks_like_continuation(row: str) -> bool:
    """Продолжение предыдущей позиции, а не начало новой."""
    text = row.strip()
    if not text:
        return True
    if text[0].islower() or text[0] in ")]»\"":
        return True
    return text.count(")") > text.count("(")


def find_items(rows: list[str]) -> list[str]:
    """Наименования позиций счёта, в порядке таблицы."""
    start = next(
        (
            i
            for i, row in enumerate(rows)
            if HEADER_RE.search(row) and HEADER_COLUMNS_RE.search(row)
        ),
        None,
    )
    if start is None:
        return []

    items: list[str] = []
    buffer: list[str] = []  # обрывки наименования до строки с числами
    last_had_own_name = False

    def next_index() -> int:
        return len(items) + (2 if buffer else 1)

    for raw in rows[start + 1 :]:
        row = clean(raw)
        if not row:
            continue
        if STOP_RE.match(row):
            break
        if not items and not buffer and _is_header_fragment(row):
            continue
        if FOOTER_RE.search(row):
            continue
        if len(LETTERS_RE.findall(row)) < 3 and not AMOUNT_TAIL_RE.search(row):
            continue  # «3 600,оо!» — мусор

        name, is_item = _strip_tail(row)
        index = _index_of(name if is_item else row)
        starts_new = index is not None and index == next_index()

        if is_item:
            # строка с числами закрывает позицию
            own = _strip_index(name) if (starts_new or (buffer and index == len(items) + 1)) else name
            full = " ".join(buffer + ([own] if own else []))
            buffer = []
            if full:
                items.append(full)
            last_had_own_name = bool(own)
            continue

        # строка без чисел — кусок наименования
        if starts_new:
            if buffer:
                items.append(" ".join(buffer))
            buffer = [_strip_index(row)]
        elif buffer:
            buffer.append(row)
        elif items and (last_had_own_name or _looks_like_continuation(row)):
            items[-1] = f"{items[-1]} {row}"
        else:
            buffer.append(row)

    if buffer:  # строка с числами не нашлась, но наименование есть
        items.append(" ".join(buffer))
    return [clean(i) for i in items if clean(i)]


def items_summary(rows: list[str]) -> str:
    """Позиции одной строкой через «; » — для поля payment_description."""
    return "; ".join(find_items(rows))
