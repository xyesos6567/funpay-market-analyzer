"""Нормализация описаний лотов: объём токенов в миллионах и распознавание моделей.

Главная идея модуля: в этой нише нельзя сравнивать цены «в лоб», потому что
лоты продаются паками разного объёма. Реальная метрика — рубли за 1M токенов.
"""

from __future__ import annotations

import logging
import re

from .parse import Offer

log = logging.getLogger(__name__)

# Слова, указывающие, что речь именно о токенах, а не о запросах/днях/кредитах.
_TOKEN_WORD = re.compile(r"токен|ткн|token", re.IGNORECASE)

# Минимальный правдоподобный объём пака. Меньше 100k токенов в этой нише
# не продают — такие значения означают ошибку разбора, а не реальный лот.
MIN_TOKENS_MILLIONS = 0.1

# Число с буквенным суффиксом: 25B, 500M, 100кк, 3 млрд, 500 тыс.
_UNIT_MULTIPLIER_MILLIONS = {
    "b": 1000.0,
    "billion": 1000.0,
    "млрд": 1000.0,
    "m": 1.0,
    "million": 1.0,
    "млн": 1.0,
    "кк": 1.0,
    "k": 0.001,
    "thousand": 0.001,
    "тыс": 0.001,
    "к": 0.001,
}

_NUM_UNIT = re.compile(
    r"(?P<num>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>млрд|млн|тыс|кк|billion|million|thousand|[bmkк])(?![а-яa-z])",
    re.IGNORECASE,
)

# Число, записанное разрядами с разделителями: 5 000 000, 20 000 000.
_GROUPED_NUM = re.compile(r"(?P<num>\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d{4,})")

# Известные модели, которые упоминают в лотах этой ниши.
# Порядок важен: более конкретные шаблоны идут раньше общих.
_MODEL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("DeepSeek V4.1", re.compile(r"v4[.,]1", re.I)),
    # Варианты пишут и как «V4 Pro», и как «V4.1 Flash», поэтому между «V4»
    # и названием варианта допускаем суффикс версии.
    ("DeepSeek V4 Pro", re.compile(r"v4(?:[.,]\d)?\s*pro", re.I)),
    ("DeepSeek V4 Flash", re.compile(r"v4(?:[.,]\d)?\s*flash", re.I)),
    ("DeepSeek V4", re.compile(r"\bv4\b", re.I)),
    ("DeepSeek R1", re.compile(r"\br1\b", re.I)),
    ("DeepSeek", re.compile(r"deep[\s-]?seek", re.I)),
    ("GPT-6", re.compile(r"gpt[\s-]?6", re.I)),
    ("GPT-5", re.compile(r"gpt[\s-]?5", re.I)),
    ("GPT-4", re.compile(r"gpt[\s-]?4", re.I)),
    ("ChatGPT", re.compile(r"chat[\s-]?gpt", re.I)),
    ("Claude Opus", re.compile(r"opus", re.I)),
    ("Claude Sonnet", re.compile(r"sonnet", re.I)),
    ("Claude", re.compile(r"claude", re.I)),
    ("Gemini", re.compile(r"gemini", re.I)),
    ("Grok", re.compile(r"grok", re.I)),
    ("Kimi", re.compile(r"kimi", re.I)),
    ("GLM", re.compile(r"\bglm\b", re.I)),
    ("MiniMax", re.compile(r"minimax", re.I)),
    ("Qwen", re.compile(r"qwen", re.I)),
    ("Midjourney", re.compile(r"midjourney", re.I)),
    ("Perplexity", re.compile(r"perplexity", re.I)),
    ("Sora", re.compile(r"\bsora\b", re.I)),
    ("Cursor", re.compile(r"cursor", re.I)),
    ("Cline", re.compile(r"cline", re.I)),
    ("Codex", re.compile(r"codex", re.I)),
    ("Copilot", re.compile(r"copilot", re.I)),
]


def _near_token_word(text: str, start: int, end: int, window: int = 35) -> bool:
    """Есть ли упоминание токенов рядом с найденным числом."""
    left = text[max(0, start - window):start]
    right = text[end:end + window]
    return bool(_TOKEN_WORD.search(left) or _TOKEN_WORD.search(right))


def parse_token_volume_millions(description: str) -> float | None:
    """Возвращает объём токенов в миллионах или None, если объём не распознан.

    Требует явного упоминания токенов рядом с числом. Без этого правила в объём
    попадает мусор: «ChatGPT PRO x5 5x х5 100» превращалось в 5100 токенов,
    «25 000 кредитов» — в 25M токенов, а «X Ultra • 3M» — в 3M токенов,
    хотя 3M там означает уровень подписки. Лучше пропустить лот, чем выдумать
    объём: на этом числе строятся все выводы о цене.

    Примеры:
        'V4 Flash 25B токенов'   -> 25000.0
        'GLM 5.3 API 100M токенов' -> 100.0
        '100кк токенов'          -> 100.0
        '5 000 000 ТОКЕНОВ'      -> 5.0
        'Безлимит 30 дней'       -> None
        'ChatGPT PRO x5 100'     -> None
        '25 000 кредитов'        -> None
    """
    if not description:
        return None

    candidates: list[float] = []

    for m in _NUM_UNIT.finditer(description):
        unit = m.group("unit").lower()
        multiplier = _UNIT_MULTIPLIER_MILLIONS.get(unit)
        if multiplier is None:
            continue
        try:
            value = float(m.group("num").replace(",", "."))
        except ValueError:
            continue
        if _near_token_word(description, m.start(), m.end()):
            candidates.append(value * multiplier)

    for m in _GROUPED_NUM.finditer(description):
        raw = re.sub(r"[ \u00a0\u202f]", "", m.group("num"))
        try:
            value = float(raw)
        except ValueError:
            continue
        if _near_token_word(description, m.start(), m.end()):
            candidates.append(value / 1_000_000.0)

    if not candidates:
        return None

    # Максимальный объём: если в описании несколько паков, продаётся обычно старший.
    volume = max(candidates)
    if volume < MIN_TOKENS_MILLIONS:
        return None
    return volume


# Общие названия, которые нужно убрать, если найдено более конкретное того же семейства.
# Иначе в отчёте рядом окажутся «DeepSeek» и «DeepSeek V4 Flash» как разные модели.
_GENERIC_DROP: dict[str, tuple[str, ...]] = {
    "DeepSeek V4": ("DeepSeek V4.1", "DeepSeek V4 Pro", "DeepSeek V4 Flash"),
    "DeepSeek": (
        "DeepSeek V4",
        "DeepSeek V4.1",
        "DeepSeek V4 Pro",
        "DeepSeek V4 Flash",
        "DeepSeek R1",
    ),
    "Claude": ("Claude Opus", "Claude Sonnet"),
}


def detect_models(text: str) -> list[str]:
    """Находит упомянутые в тексте модели ИИ.

    Порядок результата соответствует порядку в _MODEL_PATTERNS: семейства идут
    вместе, поэтому результат стабилен и не зависит от порядка слов в описании.
    """
    if not text:
        return []
    found: list[str] = []
    for name, pattern in _MODEL_PATTERNS:
        if pattern.search(text) and name not in found:
            found.append(name)

    original = set(found)
    return [
        name
        for name in found
        if not any(specific in original for specific in _GENERIC_DROP.get(name, ()))
    ]


def enrich_offer(offer: Offer) -> Offer:
    """Дополняет лот юнит-экономикой и списком моделей."""
    volume = parse_token_volume_millions(offer.description)
    offer.tokens_millions = volume
    if volume and offer.price is not None:
        # Округляем, иначе 45.01/300 даёт 0.15003333333333332 и мусор в отчёте.
        offer.price_per_million = round(offer.price / volume, 6)
    else:
        offer.price_per_million = None
    offer.detected_models = detect_models(f"{offer.description} {offer.model}")
    return offer


def enrich_offers(offers: list[Offer]) -> list[Offer]:
    for offer in offers:
        enrich_offer(offer)
    return offers
