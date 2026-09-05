"""Починка типичных ошибок OCR в числовых местах.

Движки распознавания путают цифры с похожими буквами: `54 ооо,оо` вместо `54 000,00`,
`S32101001` вместо `532101001`. В наименованиях такие замены делать нельзя, поэтому
чинится только там, где по смыслу обязаны стоять цифры: реквизиты и суммы.

Проверить результат есть чем: у ИНН, БИК и счетов контрольные суммы.
"""
from __future__ import annotations

import re

# буква -> цифра, на которую она похожа
LOOKALIKE = {
    "О": "0", "о": "0", "O": "0", "o": "0", "D": "0", "Q": "0",
    "З": "3", "з": "3",
    "I": "1", "l": "1", "|": "1", "İ": "1",
    "S": "5", "s": "5",
    "б": "6", "Б": "6",
    "В": "8",
    "Ч": "4",
    "Т": "7",
    "g": "9",
}
LOOKALIKE_CHARS = "".join(LOOKALIKE)
DIGITS_OR_LOOKALIKE = re.compile(rf"^[\d{re.escape(LOOKALIKE_CHARS)}]+$")

# «№» распознаётся как NQ, N2, N9, Ne, NO
NUMBER_SIGN_RE = re.compile(r"\bN[QqO029e]\b|\bNo\b")

# «ООО» перед кавычкой или названием движок читает как нули: «000 "СИГМА-ОПТ"».
# Сумму не трогаем: у «1 000 Штук» перед нулями стоит цифра.
OOO_AS_ZEROS_RE = re.compile(r"(?<!\d)(?<!\d\s)\b000\b(?=\s*[«\"“”'‘]|\s+[А-Яа-яЁё])")

# метки, после которых обязаны идти цифры
REQUISITE_LABEL_RE = re.compile(
    r"(?P<label>ИНН|КПП|БИК|Корр\.?\s*с[чx]ет|Кор\.?\s*с[чx]|с[чx]ет|с[чx]\.?)\s*(?:№|N)?\s*"
    r"(?P<value>[\dОоOoDQЗзIl|SsбБВЧТg]{4,25})",
    re.IGNORECASE,
)

AMOUNT_TOKEN_RE = re.compile(rf"^[\d{re.escape(LOOKALIKE_CHARS)}]+[.,][\d{re.escape(LOOKALIKE_CHARS)}]{{1,2}}$")


def to_digits(text: str) -> str:
    return "".join(LOOKALIKE.get(ch, ch) for ch in text)


def _count_digits(text: str) -> int:
    return sum(ch.isdigit() for ch in text)


def fix_numeric_context(text: str) -> str:
    """Чинит суммы: токен из «цифр и похожих букв» рядом с числом становится числом."""
    tokens = text.split(" ")
    fixed: list[str] = []
    for index, token in enumerate(tokens):
        core = token.strip(".,;:%")
        suffix = token[len(core) :] if core else ""
        prefix = ""
        if core and not DIGITS_OR_LOOKALIKE.match(core) and not AMOUNT_TOKEN_RE.match(core):
            fixed.append(token)
            continue
        if not core:
            fixed.append(token)
            continue

        digits_here = _count_digits(core)
        previous_numeric = bool(fixed) and _count_digits(fixed[-1]) >= 1
        # «54 ооо,оо»: сам по себе кусок цифр не содержит, но идёт за числом
        if digits_here >= 2 or (previous_numeric and (digits_here or len(core) >= 2)):
            fixed.append(prefix + to_digits(core) + suffix)
        else:
            fixed.append(token)
    return " ".join(fixed)


def _fix_requisite(match: re.Match) -> str:
    return f"{match.group('label')} {to_digits(match.group('value'))}"


# «НДС S%» — ставка одной буквой, отдельным токеном её не починить
VAT_RATE_RE = re.compile(
    rf"(?P<label>НДС)(?P<gap>[^%\d]{{0,4}}?)(?P<rate>[\d{re.escape(LOOKALIKE_CHARS)}]{{1,2}})\s*%",
    re.IGNORECASE,
)


def _fix_vat_rate(match: re.Match) -> str:
    return f"{match.group('label')}{match.group('gap')}{to_digits(match.group('rate'))}%"


def repair_row(row: str) -> str:
    row = NUMBER_SIGN_RE.sub("№", row)
    row = OOO_AS_ZEROS_RE.sub("ООО", row)
    row = REQUISITE_LABEL_RE.sub(_fix_requisite, row)
    row = VAT_RATE_RE.sub(_fix_vat_rate, row)
    return fix_numeric_context(row)


def repair_cells(
    cells: list[list[tuple[float, str]]]
) -> list[list[tuple[float, str]]]:
    return [[(x, repair_row(text)) for x, text in row] for row in cells]
