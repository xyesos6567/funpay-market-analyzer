"""Сквозной тест конвейера на сохранённых страницах, без обращения к сети.

Проверяет, что парсинг, юнит-экономика, аналитика, рекомендации и рендер
отчёта работают вместе и дают осмысленный результат.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from analyzer.advice import build_recommendations, summarize
from analyzer.analytics import analyze_market
from analyzer.config import load_config
from analyzer.discover import DiscoveryResult, DiscoveredCategory
from analyzer.normalize import enrich_offers
from analyzer.parse import CategoryPage, Offer, parse_category_page, parse_seller_offers
from analyzer.report import build_context, render_report

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
SELLER_ID = "4233111"


@pytest.fixture(scope="module")
def config():
    return load_config(PROJECT_ROOT / "config.yaml")


@pytest.fixture(scope="module")
def market(config):
    page = parse_category_page(
        (FIXTURES / "category_4506.html").read_text(encoding="utf-8"),
        "4506",
        "API-ключ DeepSeek",
    )
    enrich_offers(page.offers)

    seller_offers = parse_seller_offers(
        (FIXTURES / "seller_4233111.html").read_text(encoding="utf-8"), SELLER_ID
    )
    enrich_offers(seller_offers)

    return analyze_market(
        [page],
        target_seller_id=config.seller_id,
        target_seller_name=config.seller_name,
        brand=config.brand,
        top_n=config.top_competitors,
    )


def test_target_seller_is_found_in_category(market) -> None:
    """Лоты анализируемого продавца должны находиться на странице категории."""
    analysis = market.categories[0]
    assert analysis.target_offers, "в категории 4506 должны быть лоты продавца 4233111"
    assert all(o.seller_id == SELLER_ID for o in analysis.target_offers)


def test_unit_economics_are_computed(market) -> None:
    analysis = market.categories[0]
    assert analysis.offers_with_volume > 100, "объём токенов должен распознаваться массово"
    assert analysis.unit_price_stats.median is not None


def test_target_positions_have_comparables(market) -> None:
    """У лотов продавца должны находиться аналоги для сравнения цены."""
    analysis = market.categories[0]
    with_comparables = [p for p in analysis.positions if p.comparable_count > 0]
    assert with_comparables, "должны найтись сопоставимые лоты конкурентов"
    assert any(p.scope in {"volume", "family"} for p in with_comparables)


def test_comparison_scope_is_honest(market) -> None:
    """Масштаб сравнения должен быть выставлен для каждого лота.

    Если объём лота распознан и на рынке есть паки того же размера,
    сравнение идёт по объёму; иначе — только по модели.
    """
    analysis = market.categories[0]
    assert analysis.positions, "у продавца должны быть лоты в категории"
    for pos in analysis.positions:
        assert pos.scope in {"volume", "family", "none"}
        if pos.scope == "volume":
            assert pos.deviation_pct is not None
            assert pos.comparable_count >= 3


def test_competitors_are_ranked(market) -> None:
    analysis = market.categories[0]
    assert len(analysis.competitors) == 10
    scores = [c.score for c in analysis.competitors]
    assert scores == sorted(scores, reverse=True), "конкуренты должны идти по убыванию балла"
    assert scores[0] > 0


def test_recommendations_are_tags_with_category_id(config, market) -> None:
    """У каждой рекомендации должен быть id категории, а не только имя.

    Имена подкатегорий на FunPay повторяются («Подписка» есть у ChatGPT,
    Claude и Gemini), поэтому группировка по имени смешивала бы категории.
    """
    recs = build_recommendations(
        market, config.price_deviation_threshold_pct, config.desired_phrases
    )
    assert recs
    for rec in recs:
        assert rec.category_id, "id категории обязателен"
        assert rec.category_id == "4506"


def test_report_groups_recommendations_by_category_id() -> None:
    """Две категории с одинаковым именем не должны делить рекомендации."""
    from analyzer.advice import build_recommendations as build
    from analyzer.analytics import MarketReport
    from analyzer.parse import CategoryPage

    def page(cat_id: str, price: float, auto: bool) -> CategoryPage:
        offers = [
            Offer(
                offer_id="1",
                url="",
                description="DeepSeek 100M токенов",
                price=price,
                seller_id="4233111",
                seller_name="POLICEA",
                seller_reviews=10,
                seller_rating=5.0,
                auto_delivery=auto,
                category_id=cat_id,
            ),
            Offer(
                offer_id="2",
                url="",
                description="DeepSeek 100M токенов",
                price=10.0,
                seller_id="777",
                seller_name="rival",
                seller_reviews=900,
                seller_rating=5.0,
                auto_delivery=True,
                category_id=cat_id,
            ),
        ]
        enrich_offers(offers)
        return CategoryPage(
            category_id=cat_id, category_name="Подписка", game_name="Test", offers=offers
        )

    market_report = analyze_market(
        [page("100", 50.0, False), page("200", 10.0, False)],
        target_seller_id="4233111",
        top_n=5,
    )
    recs = build(market_report, 15, [])
    ids = {r.category_id for r in recs}
    assert ids == {"100", "200"}, "рекомендации должны различаться по id, а не по имени"


def test_recommendations_are_generated(market, config) -> None:
    recs = build_recommendations(
        market,
        threshold_pct=config.price_deviation_threshold_pct,
        extra_phrases=config.desired_phrases,
    )
    assert recs, "рекомендации должны генерироваться"
    counts = summarize(recs)
    assert sum(counts.values()) == len(recs)

    # Каждая рекомендация обязана иметь конкретное действие и ссылку на данные.
    for rec in recs:
        assert rec.action and rec.evidence
        assert rec.priority in {"P0", "P1", "P2"}
        assert rec.rule.startswith("R")


def test_recommendations_are_prioritized(market, config) -> None:
    recs = build_recommendations(
        market, config.price_deviation_threshold_pct, config.desired_phrases
    )
    order = [r.priority for r in recs]
    assert order == sorted(order), "P0 должны идти раньше P1, а P1 — раньше P2"


def test_report_renders(config, market) -> None:
    discovery = DiscoveryResult(
        categories=[DiscoveredCategory(id="4506", name="API-ключ DeepSeek", game="DeepSeek", source="seed")],
        ai_games=["DeepSeek", "ChatGPT"],
        total_games_scanned=898,
    )
    recs = build_recommendations(
        market, config.price_deviation_threshold_pct, config.desired_phrases
    )
    context = build_context(
        market,
        recs,
        config,
        {
            "total_games_scanned": discovery.total_games_scanned,
            "ai_games": discovery.ai_games,
            "categories": [c.id for c in discovery.categories],
        },
    )
    html = render_report(context, config)

    assert "<html" in html
    assert config.seller_name in html
    # Отчёт должен показывать юнит-экономику, а не только цены лотов.
    assert "₽/1M" in html or "1M токенов" in html
    # И не должен содержать неотрендеренных выражений Jinja.
    assert "{{" not in html and "{%" not in html


def test_report_contains_leading_competitor(config, market) -> None:
    """Лидер рынка по отзывам должен попасть в отчёт как сильный конкурент."""
    recs = build_recommendations(
        market, config.price_deviation_threshold_pct, config.desired_phrases
    )
    context = build_context(market, recs, config, {})
    html = render_report(context, config)

    leader = market.categories[0].competitors[0]
    assert leader.name in html
    assert leader.max_reviews > 0
