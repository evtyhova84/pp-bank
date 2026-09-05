"""Вторая ступень распознавания: счёт, который не осилил локальный OCR.

Схема гибрида не меняется:

    скан -> локальный OCR -> контрольные суммы -> сошлось: в пачку
                                               -> не сошлось: сюда
                                               -> и здесь не сошлось: человеку

Отличие от первой ступени: модель возвращает не строки с координатами, а сразу
поля счёта. Разбирать координаты ей незачем — она читает документ целиком.
Зато проверки остаются те же самые: контрольные суммы ИНН, БИК, расчётного и
корр. счёта, сходимость НДС. Модель ошибается иначе, чем OCR, но так же ловится.

Это единственное место во всём проекте, где документ уходит за пределы сервера.
Ступень включается настройкой и работает только по тем счетам, которые локальный
движок не разобрал.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

MODEL = os.environ.get("PP_VISION_MODEL", "claude-opus-5")
MAX_TOKENS = 8000

# Читаемые моделью типы файлов
MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/png",
    ".tif": "image/png",
    ".tiff": "image/png",
}

# 5 МБ на изображение — предел запроса; крупное фото ужимаем перед отправкой
MAX_BYTES = 5 * 1024 * 1024


class VisionUnavailable(RuntimeError):
    pass


class VisionFailed(RuntimeError):
    pass


ZADANIE = """Перед тобой российский счёт на оплату. Извлеки из него реквизиты
получателя платежа и суммы — ровно то, что напечатано в документе.

Правила:
- Получатель платежа — тот, КОМУ платят: поставщик, исполнитель. Не покупатель.
  В шапке счёта слева обычно банк и реквизиты получателя, справа — плательщик.
- Переписывай цифры буквально. Не исправляй и не достраивай их по смыслу:
  расчётный счёт — 20 цифр, корреспондентский — 20 цифр и начинается на 301,
  БИК — 9 цифр, ИНН — 10 цифр у организации и 12 у ИП.
- Если поле не читается — оставь пустую строку. Пустое поле лучше выдуманного:
  по этим реквизитам уходят деньги.
- Суммы — числом с точкой, без пробелов: 12580.00
- Дата счёта — в виде ГГГГ-ММ-ДД.
- vat_status: "included" — НДС выделен в счёте; "none" — прямо написано
  «без НДС» или «НДС не облагается»; "unknown" — про НДС ничего не сказано.
- buyer_inn, buyer_kpp и buyer_name — реквизиты покупателя, кому счёт выставлен.
- payment_description — наименования позиций таблицы через «; », без цен,
  количеств и единиц измерения: это пойдёт в назначение платежа."""

SHEMA = {
    "type": "object",
    "properties": {
        "invoice_number": {"type": "string", "description": "номер счёта, как напечатан"},
        "invoice_date": {"type": "string", "description": "дата счёта, ГГГГ-ММ-ДД"},
        "recipient_name": {"type": "string", "description": "краткое наименование получателя"},
        "recipient_full": {"type": "string", "description": "полное наименование получателя"},
        "recipient_inn": {"type": "string"},
        "recipient_kpp": {"type": "string"},
        "recipient_account": {"type": "string", "description": "расчётный счёт, 20 цифр"},
        "recipient_bank_name": {"type": "string"},
        "recipient_bank_city": {"type": "string"},
        "recipient_bic": {"type": "string", "description": "БИК, 9 цифр"},
        "recipient_corr_account": {"type": "string", "description": "корр. счёт, 20 цифр"},
        "total_amount": {"type": "string", "description": "всего к оплате, например 12580.00"},
        "vat_rate": {"type": "string", "description": "ставка НДС в процентах или пустая строка"},
        "vat_amount": {"type": "string", "description": "сумма НДС или пустая строка"},
        "vat_status": {"type": "string", "enum": ["included", "none", "unknown"]},
        "buyer_inn": {"type": "string"},
        "buyer_name": {"type": "string"},
        "buyer_kpp": {"type": "string", "description": "КПП покупателя или пустая строка"},
        "payment_description": {
            "type": "string",
            "description": "наименования позиций счёта через «; », без цен и количеств",
        },
    },
    "required": [
        "invoice_number",
        "invoice_date",
        "recipient_name",
        "recipient_full",
        "recipient_inn",
        "recipient_kpp",
        "recipient_account",
        "recipient_bank_name",
        "recipient_bank_city",
        "recipient_bic",
        "recipient_corr_account",
        "total_amount",
        "vat_rate",
        "vat_amount",
        "vat_status",
        "buyer_inn",
        "buyer_name",
        "buyer_kpp",
        "payment_description",
    ],
    "additionalProperties": False,
}


def dostupno() -> bool:
    """Настроена ли ступень: есть библиотека и ключ."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def _szhat(path: Path, data: bytes) -> tuple[bytes, str]:
    """Ужать снимок до предела запроса. Без Pillow — отдаём как есть."""
    if len(data) <= MAX_BYTES:
        return data, MEDIA_TYPES.get(path.suffix.lower(), "image/jpeg")
    try:
        import io

        from PIL import Image, ImageOps
    except ImportError:
        raise VisionFailed(
            f"{path.name}: {len(data) // 1024 // 1024} МБ — больше предела в 5 МБ, "
            "и нет Pillow, чтобы ужать"
        ) from None

    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        for kachestvo in (85, 70, 55, 40):
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=kachestvo)
            if buf.tell() <= MAX_BYTES:
                return buf.getvalue(), "image/jpeg"
        # всё ещё велико — уменьшаем сам снимок
        img.thumbnail((2400, 2400))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return buf.getvalue(), "image/jpeg"


def _blok_dokumenta(path: Path) -> dict:
    """Картинку шлём как изображение, PDF без текстового слоя — как документ."""
    data = path.read_bytes()
    if path.suffix.lower() == ".pdf":
        if len(data) > 32 * 1024 * 1024:
            raise VisionFailed(f"{path.name}: PDF больше 32 МБ")
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(data).decode("ascii"),
            },
        }
    data, media_type = _szhat(path, data)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


def raspoznat(path: str | Path) -> dict:
    """Поля счёта по картинке или PDF-скану. Проверять их — забота вызывающего."""
    path = Path(path)
    if not path.exists():
        raise VisionFailed(f"файл не найден: {path}")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise VisionUnavailable(
            "не задан ANTHROPIC_API_KEY — вторая ступень распознавания выключена"
        )
    try:
        import anthropic
    except ImportError as exc:
        raise VisionUnavailable("не установлена библиотека anthropic") from exc

    client = anthropic.Anthropic()
    soobshchenie = [
        {
            "role": "user",
            "content": [_blok_dokumenta(path), {"type": "text", "text": ZADANIE}],
        }
    ]
    zapros = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": soobshchenie,
        "output_config": {"format": {"type": "json_schema", "schema": SHEMA}},
    }

    try:
        # Отказ модели по правилам безопасности на счетах невероятен, но сервис
        # работает без человека рядом: пусть запрос доигрывается на запасной
        # модели, а не падает молча.
        otvet = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **zapros
        )
    except (anthropic.BadRequestError, TypeError):
        otvet = client.messages.create(**zapros)
    except anthropic.APIStatusError as exc:
        raise VisionFailed(f"модель не ответила: {exc}") from exc
    except anthropic.APIConnectionError as exc:
        raise VisionFailed(f"нет связи с моделью: {exc}") from exc

    if otvet.stop_reason == "refusal":
        raise VisionFailed("модель отказалась разбирать документ")

    text = next((b.text for b in otvet.content if b.type == "text"), "")
    if not text:
        raise VisionFailed("модель вернула пустой ответ")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VisionFailed(f"ответ модели не разобран как JSON: {text[:200]}") from exc

    return _v_kontrakt(data)


def _v_kontrakt(data: dict) -> dict:
    """Ответ модели -> тот же контракт, что отдаёт разбор PDF."""
    def s(key: str) -> str:
        return str(data.get(key) or "").strip()

    stavka = s("vat_rate")
    summa_nds = s("vat_amount")
    return {
        "invoice_number": s("invoice_number"),
        "invoice_date": s("invoice_date"),
        "recipient_name": s("recipient_name"),
        "recipient_full": s("recipient_full") or s("recipient_name"),
        "recipient_inn": s("recipient_inn"),
        "recipient_kpp": s("recipient_kpp"),
        "recipient_account": s("recipient_account"),
        "recipient_bank_name": s("recipient_bank_name"),
        "recipient_bank_city": s("recipient_bank_city"),
        "recipient_bic": s("recipient_bic"),
        "recipient_corr_account": s("recipient_corr_account"),
        "total_amount": s("total_amount"),
        "vat_rate": stavka or None,
        "vat_amount": summa_nds or None,
        "vat_status": s("vat_status") or "unknown",
        "payment_description": s("payment_description"),
        "_buyer_inn": s("buyer_inn"),
        "_buyer_name": s("buyer_name"),
        "_buyer_kpp": s("buyer_kpp"),
        "_engine": "claude-vision",
    }
