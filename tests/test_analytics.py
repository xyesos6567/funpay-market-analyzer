"""Точечные тесты аналитики и скоринга конкурентов.

Здесь проверяются свойства, от которых зависит правдивость отчёта:
сравниваются только сопоставимые лоты, а балл конкурента отражает
реальную силу продавца.
"""

from __future__ import annotations

import pytest

from analyzer.analytics import (
    VOLUME_BANDS,
    analyze_category,
    build_buckets,
    compute_stats,
    percentile,
    percentile_rank,
)
from analyzer.competitors import (
    build_seller_profiles,
    competitor_score,
    model_family,
    score_sellers,
    top_competitors,
)
from analyzer.normalize import enrich_offer
from analyzer.parse import Offer

SELLER = "111"


def make_offer(
    offer_id: str,
    *,
    seller_id: str = "999",
    description: str = "",
    price: float | None = 100.0,
    tokens_millions: float | None = None,
    reviews: int = 0,
    rating: float | None = 5.0,
    online: bool = True,
    auto: bool = True,
    promoted: bool = False,
    category: str = "1",
) -> Offer:
    offer = Offer(
        offer_id=offer_id,
        url="",
        description=description,
        price=price,
        seller_id=seller_id,
        seller_name=f"seller{seller_id}",
        seller_reviews=reviews,
        seller_rating=rating,
        seller_online=online,
        auto_delivery=auto,
        promoted=promoted,
        category_id=category,
    )
    if tokens_millions is not None:
        offer.tokens_millions = tokens_millions
        if price is not None:
            offer.price_per_million = round(price / tokens_millions, 6)
    return enrich_offer(offer)


# --- статистика ----------------------------------------------------------


def test_percentile_edges() -> None:
    values = [1.0, 2.0, 3.0, 4.0]
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 1.0) == 4.0
    assert percentile(values, 0.5) == pytest.approx(2.5)
    assert percentile([], 0.5) is None
    assert percentile([7.0], 0.5) == 7.0


def test_percentile_rank() -> None:
    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile_rank(values, 5.0) == 0.0
    assert percentile_rank(values, 50.0) == 100.0
    # Ниже 25 лежат два значения из четырёх — это 50-й процентиль.
    assert percentile_rank(values, 25.0) == pytest.approx(50.0)
    # Совпадение с элементом учитывается как половина его веса.
    assert percentile_rank(values, 30.0) == pytest.approx(62.5)
    assert percentile_rank([], 1.0) is None


def test_compute_stats() -> None:
    stats = compute_stats([1.0, 2.0, 3.0, 4.0, 5.0])
    assert stats.count == 5
    assert stats.minimum == 1.0
    assert stats.median == 3.0
    assert stats.maximum == 5.0
    assert stats.mean == 3.0


def test_compute_stats_empty() -> None:
    stats = compute_stats([])
    assert stats.count == 0
    assert stats.median is None


def test_buckets_cover_values() -> None:
    values = [0.5, 5.0, 50.0, 5000.0]
    offers = [make_offer(str(i), price=v, tokens_millions=1.0) for i, v in enumerate(values)]
    buckets = build_buckets(values, offers, (1, 10, 100, 1000))
    assert sum(b.count for b in buckets) == 4
    # Значения должны попасть в разные корзины, а не свалены в одну.
    assert sum(1 for b in buckets if b.count > 0) == 4


# --- сопоставимость лотов ------------------------------------------------


def test_volume_scope_used_for_similar_packs() -> None:
    """Паки сопоставимого объёма сравниваются напрямую."""
    target = make_offer("t", seller_id=SELLER, description="DeepSeek 100M токенов", price=10.0)
    rivals = [
        make_offer(str(i), description="DeepSeek 100M токенов", price=20.0, tokens_millions=100.0)
        for i in range(5)
    ]
    analysis = analyze_category("1", "cat", "DeepSeek", [target, *rivals], SELLER)
    pos = analysis.positions[0]
    assert pos.scope == "volume"
    assert pos.comparable_count == 5
    assert pos.comparable_median_unit_price == pytest.approx(0.2)
    assert pos.deviation_pct == pytest.approx(-50.0)


def test_scope_is_family_when_volumes_differ_wildly() -> None:
    """Крупный пак не должен сравниваться с мелкими: это другой продукт."""
    target = make_offer("t", seller_id=SELLER, description="DeepSeek 25B токенов", price=1500.0)
    # Конкуренты продают только мелкие паки — 2M, что в тысячи раз меньше.
    rivals = [
        make_offer(str(i), description="DeepSeek 2M токенов", price=10.0, tokens_millions=2.0)
        for i in range(10)
    ]
    analysis = analyze_category("1", "cat", "DeepSeek", [target, *rivals], SELLER)
    pos = analysis.positions[0]
    assert pos.scope == "family", "при огромной разнице объёмов прямое сравнение недопустимо"
    assert pos.comparable_median_unit_price is not None


def test_no_comparables_without_family() -> None:
    target = make_offer("t", seller_id=SELLER, description="Нечто без модели", price=100.0)
    rivals = [make_offer(str(i), description="Тоже без модели", price=50.0) for i in range(5)]
    analysis = analyze_category("1", "cat", "X", [target, *rivals], SELLER)
    assert analysis.positions[0].comparable_count == 0


def test_volume_band_boundaries_are_respected() -> None:
    """Внутри масштаба 'volume' объёмы обязаны попадать в заявленный диапазон."""
    target = make_offer("t", seller_id=SELLER, description="DeepSeek 1000M токенов", price=100.0)
    rivals = [
        make_offer(str(i), description="DeepSeek 1000M токенов", price=200.0, tokens_millions=1000.0)
        for i in range(4)
    ]
    analysis = analyze_category("1", "cat", "DeepSeek", [target, *rivals], SELLER)
    pos = analysis.positions[0]
    assert pos.scope == "volume"
    low, high = VOLUME_BANDS[0]
    for rival in rivals:
        assert low * 1000.0 <= rival.tokens_millions <= high * 1000.0


# --- рекомендации --------------------------------------------------------


def test_promise_mismatch_is_flagged() -> None:
    """«Авто» в описании без флага автовыдачи — критичная потеря трафика."""
    from analyzer.advice import rule_auto_promise_mismatch

    lying = make_offer(
        "t",
        seller_id=SELLER,
        description="DeepSeek 100M токенов • авто",
        auto=False,
    )
    honest = make_offer(
        "h",
        seller_id="999",
        description="DeepSeek 100M токенов • авто",
        auto=True,
    )
    analysis = analyze_category("1", "cat", "DeepSeek", [lying, honest], SELLER)
    recs = rule_auto_promise_mismatch(analysis)
    assert len(recs) == 1
    assert recs[0].priority == "P0"
    assert "автовыдач" in recs[0].action.lower()


def test_no_mismatch_when_flag_matches_promise() -> None:
    from analyzer.advice import rule_auto_promise_mismatch

    good = make_offer("t", seller_id=SELLER, description="DeepSeek 100M • авто", auto=True)
    analysis = analyze_category("1", "cat", "DeepSeek", [good], SELLER)
    assert rule_auto_promise_mismatch(analysis) == []


def test_small_sample_downgrades_priority() -> None:
    """Вывод по 3 аналогам не должен подаваться как критичный.

    Сравнение начинается минимум с трёх аналогов: на двух это шум.
    """
    from analyzer.advice import rule_price_position

    target = make_offer("t", seller_id=SELLER, description="DeepSeek 100M токенов", price=50.0)
    rivals = [
        make_offer(str(i), description="DeepSeek 100M токенов", price=10.0, tokens_millions=100.0)
        for i in range(3)
    ]
    analysis = analyze_category("1", "cat", "DeepSeek", [target, *rivals], SELLER)
    assert analysis.positions[0].comparable_count == 3
    recs = rule_price_position(analysis, threshold_pct=15)
    assert recs, "превышение цены должно фиксироваться"
    assert recs[0].priority == "P1", "при выборке меньше 5 аналогов приоритет понижается"
    assert "выборка маленькая" in recs[0].evidence


def test_two_comparables_are_not_enough() -> None:
    """На двух аналогах вывод о цене не делается вовсе."""
    from analyzer.advice import rule_price_position

    target = make_offer("t", seller_id=SELLER, description="DeepSeek 100M токенов", price=50.0)
    rivals = [
        make_offer(str(i), description="DeepSeek 100M токенов", price=10.0, tokens_millions=100.0)
        for i in range(2)
    ]
    analysis = analyze_category("1", "cat", "DeepSeek", [target, *rivals], SELLER)
    assert analysis.positions[0].comparable_count == 0
    assert rule_price_position(analysis, threshold_pct=15) == []





def test_model_family() -> None:
    assert model_family("DeepSeek V4 Flash") == "DeepSeek"
    assert model_family("GPT-6") == "GPT-6"
    assert model_family("") == ""


def test_seller_profiles_group_offers() -> None:
    offers = [
        make_offer("1", seller_id="7", reviews=100),
        make_offer("2", seller_id="7", reviews=100),
        make_offer("3", seller_id="8", reviews=5),
    ]
    profiles = build_seller_profiles(offers, SELLER)
    assert set(profiles) == {"7", "8"}
    assert profiles["7"].offer_count == 2
    assert profiles["7"].max_reviews == 100


def test_competitor_score_prefers_reviews_and_auto() -> None:
    strong = build_seller_profiles([make_offer("1", seller_id="1", reviews=5000)], SELLER)["1"]
    weak = build_seller_profiles(
        [make_offer("2", seller_id="2", reviews=5, rating=3.0, online=False, auto=False)], SELLER
    )["2"]
    assert competitor_score(strong, 5000) > competitor_score(weak, 5000)


def test_top_competitors_excludes_target_and_sorts() -> None:
    offers = [
        make_offer("1", seller_id="1", reviews=1000),
        make_offer("2", seller_id="2", reviews=10),
        make_offer("3", seller_id=SELLER, reviews=9999),
    ]
    profiles = build_seller_profiles(offers, SELLER)
    score_sellers(profiles)
    ranked = top_competitors(profiles, limit=10)
    assert [p.seller_id for p in ranked] == ["1", "2"], "целевой продавец не должен попадать в конкуренты"


def test_target_flag_is_set() -> None:
    profiles = build_seller_profiles([make_offer("1", seller_id=SELLER)], SELLER)
    assert profiles[SELLER].is_target
