"""Тесты юнит-экономики: объём токенов и рубли за 1M токенов.

Описания и цены взяты из реальных лотов продавца 4233111,
эталонные значения посчитаны вручную во время разведки.
"""

from __future__ import annotations

import pytest

from analyzer.normalize import detect_models, enrich_offer, parse_token_volume_millions
from analyzer.parse import Offer


@pytest.mark.parametrize(
    ("description", "expected_millions"),
    [
        ("⚡ VibeTokens • DeepSeek V4 Flash 25B токенов • макс. выгода • авто", 25000.0),
        ("⚡ VibeTokens • DeepSeek V4 Flash 10B токенов • макс. выгода", 10000.0),
        ("⚡ VibeTokens • DeepSeek V4 Flash 3B токенов • опт • 3 модели", 3000.0),
        ("⚡ VibeTokens • DeepSeek V4 Flash 1B токенов • Cursor/Cline/Codex", 1000.0),
        ("⚡ VibeTokens • DeepSeek V4 Flash 500M токенов • для агентов", 500.0),
        ("⚡ VibeTokens • DeepSeek V4 Flash 300M токенов • хит • без VPN", 300.0),
        ("⚡ VibeTokens • DeepSeek V4 Flash 100M токенов • без VPN • Cursor", 100.0),
        ("⚡ VibeTokens • DeepSeek V4 Flash 10M токенов • пробник", 10.0),
        ("⚡ VibeTokens • GLM 5.3 API 100M токенов • Cursor/Cline • без VPN", 100.0),
        ("🎁 ПРОБНИК 5 000 000 ТОКЕНОВ • DEEPSEEK API-КЛЮЧ", 5.0),
        ("100кк токенов в подарок", 100.0),
        ("2000 млн токенов", 2000.0),
        # Не токены — объём не должен определиться.
        ("⚡ VibeTokens • Безлимит 30 дней • DeepSeek/GLM/MiniMax • 600 зап/день", None),
        ("🛠 Настрою DeepSeek API под Cursor/Cline/Codex + 100M токенов", 100.0),
        # Регрессии: числа без слова «токен» не должны становиться объёмом.
        # «x5 5x х5 100» раньше давало 5100 токенов.
        ("☮️ Оплата подписки: ChatGPT PRO x5 5x х5 100 (за 100$) на Ваш аккаунт", None),
        # «25 000 кредитов» — это кредиты Gemini, а не токены.
        ("Google AI Ultra Премиум 1 мес | 25 000 кредитов на ваш аккаунт", None),
        # «3M» здесь — уровень подписки X Premium, а не токены.
        ("【Grok + Cursor + X Ultra • 3M 50x】Grok 4.7, Composer 2.5 • Личный", None),
        ("GEMINI PRO АНТИГРАВИТИ — 12 МЕСЯЦЕВ, 12 000 кредитов", None),
        # Слишком маленький объём для реального лота — считаем ошибкой разбора.
        ("10 токенов в подарок", None),
    ],
)
def test_parse_token_volume(description: str, expected_millions: float | None) -> None:
    assert parse_token_volume_millions(description) == (
        pytest.approx(expected_millions) if expected_millions is not None else None
    )


@pytest.mark.parametrize(
    ("description", "price", "expected_per_million"),
    [
        ("VibeTokens • DeepSeek V4 Flash 25B токенов", 1500.0, 0.060),
        ("VibeTokens • DeepSeek V4 Flash 10B токенов", 800.0, 0.080),
        ("VibeTokens • DeepSeek V4 Flash 3B токенов", 350.0, 0.1167),
        ("VibeTokens • DeepSeek V4 Flash 1B токенов", 150.0, 0.150),
        ("VibeTokens • DeepSeek V4 Flash 100M токенов", 15.0, 0.150),
        ("VibeTokens • DeepSeek V4 Flash 10M токенов", 5.09, 0.509),
    ],
)
def test_price_per_million(description: str, price: float, expected_per_million: float) -> None:
    offer = enrich_offer(Offer(offer_id="1", url="", description=description, price=price))
    assert offer.price_per_million == pytest.approx(expected_per_million, abs=0.0005)


def test_volume_discount_is_monotonic() -> None:
    """Проверяем, что лестница скидок друга действительно монотонна."""
    packs = [
        ("10M токенов", 5.09),
        ("100M токенов", 15.0),
        ("500M токенов", 75.0),
        ("1B токенов", 150.0),
        ("10B токенов", 800.0),
        ("25B токенов", 1500.0),
    ]
    units = [
        enrich_offer(Offer(offer_id=str(i), url="", description=d, price=p)).price_per_million
        for i, (d, p) in enumerate(packs)
    ]
    assert all(u is not None for u in units)
    # Чем больше пак, тем дешевле токен.
    assert all(units[i] >= units[i + 1] for i in range(len(units) - 1)), units


def test_unlimited_lot_has_no_unit_price() -> None:
    """Лот без объёма токенов не должен получать фиктивную юнит-цену."""
    offer = enrich_offer(
        Offer(
            offer_id="1",
            url="",
            description="⚡ VibeTokens • Безлимит 30 дней • DeepSeek/GLM/MiniMax • 600 зап/день",
            price=2999.0,
        )
    )
    assert offer.tokens_millions is None
    assert offer.price_per_million is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Порядок соответствует реестру моделей: семейства идут вместе,
        # поэтому результат не зависит от порядка слов в описании.
        ("DeepSeek V4 Flash 25B токенов", ["DeepSeek V4 Flash"]),
        ("V4 PRO · V4.1 FLASH", ["DeepSeek V4.1", "DeepSeek V4 Pro", "DeepSeek V4 Flash"]),
        ("GLM 5.3 API", ["GLM"]),
        ("DEEPSEEK | GPT 6 | CLAUDE | KIMI | GROK | GEMINI", ["DeepSeek", "GPT-6", "Claude", "Gemini", "Grok", "Kimi"]),
        ("Cursor/Cline/Codex", ["Cursor", "Cline", "Codex"]),
        ("Безлимит DeepSeek/GLM/MiniMax", ["DeepSeek", "GLM", "MiniMax"]),
        ("Claude Sonnet 4", ["Claude Sonnet"]),
        ("обычный текст без моделей", []),
    ],
)
def test_detect_models(text: str, expected: list[str]) -> None:
    assert detect_models(text) == expected
