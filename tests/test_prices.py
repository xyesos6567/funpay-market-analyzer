"""Тесты ценовых действий: что поднять, что опустить.

Это главный практический вывод отчёта, поэтому проверяем и направление,
и арифметику целевой цены, и то, что несопоставимые лоты не попадают в список.
"""

from __future__ import annotations

import pytest

from analyzer.advice import build_price_actions
from analyzer.analytics import analyze_market
from analyzer.normalize import enrich_offers
from analyzer.parse import CategoryPage, Offer

SELLER = "4233111"


def offer(
    offer_id: str,
    seller_id: str,
    price: float,
    volume_m: float | None,
    description: str,
    auto: bool = True,
) -> Offer:
    o = Offer(
        offer_id=offer_id,
        url="",
        description=description,
        price=price,
        seller_id=seller_id,
        seller_name=f"s{seller_id}",
        seller_reviews=100,
        seller_rating=5.0,
        auto_delivery=auto,
        category_id="1",
    )
    if volume_m is not None:
        o.tokens_millions = volume_m
        o.price_per_million = price / volume_m
    return enrich_offers([o])[0]


def make_market(offers: list[Offer], name: str = "Токены", cat_id: str = "1"):
    page = CategoryPage(category_id=cat_id, category_name=name, game_name="Test", offers=offers)
    return analyze_market([page], target_seller_id=SELLER)


def test_cheap_lot_gets_raise_action() -> None:
    """Если продавец дешевле аналогов, действие — поднять цену."""
    ours = offer("t", SELLER, 10.0, 100.0, "DeepSeek 100M токенов")
    rivals = [
        offer(str(i), f"9{i}", 40.0, 100.0, "DeepSeek 100M токенов") for i in range(5)
    ]
    actions = build_price_actions(make_market([ours, *rivals]), threshold_pct=15)

    assert len(actions) == 1
    action = actions[0]
    assert action.direction == "up"
    assert action.current_price == 10.0
    # Медиана аналогов 0.4 ₽/1M, объём 100M -> целевая цена 40 ₽.
    assert action.target_price == pytest.approx(40.0)
    assert action.target_unit == pytest.approx(0.4)
    assert action.impact_rub == pytest.approx(30.0)
    assert action.comparables == 5


def test_expensive_lot_gets_lower_action() -> None:
    ours = offer("t", SELLER, 100.0, 100.0, "DeepSeek 100M токенов")
    rivals = [
        offer(str(i), f"9{i}", 40.0, 100.0, "DeepSeek 100M токенов") for i in range(5)
    ]
    actions = build_price_actions(make_market([ours, *rivals]), threshold_pct=15)

    assert len(actions) == 1
    assert actions[0].direction == "down"
    assert actions[0].target_price == pytest.approx(40.0)
    assert actions[0].delta_pct > 0  # положительное отклонение = дороже рынка


def test_within_threshold_produces_no_action() -> None:
    """Отклонение в пределах порога не требует вмешательства."""
    ours = offer("t", SELLER, 41.0, 100.0, "DeepSeek 100M токенов")
    rivals = [
        offer(str(i), f"9{i}", 40.0, 100.0, "DeepSeek 100M токенов") for i in range(5)
    ]
    assert build_price_actions(make_market([ours, *rivals]), threshold_pct=15) == []


def test_actions_sorted_by_money_at_stake() -> None:
    """Сверху должно быть действие с наибольшей суммой на кону."""
    small = offer("s", SELLER, 10.0, 100.0, "DeepSeek 100M токенов")
    big = offer("b", SELLER, 100.0, 10000.0, "DeepSeek 10B токенов")
    rivals = [
        offer(str(i), f"9{i}", 40.0, 100.0, "DeepSeek 100M токенов") for i in range(5)
    ] + [
        offer(f"r{i}", f"8{i}", 400.0, 10000.0, "DeepSeek 10B токенов") for i in range(5)
    ]
    actions = build_price_actions(make_market([small, big, *rivals]), threshold_pct=15)

    assert len(actions) == 2
    assert actions[0].impact_rub >= actions[1].impact_rub
    assert actions[0].volume == "10B"


def test_incomparable_volume_is_excluded() -> None:
    """Если аналогов нужного объёма нет, ценовое действие не выдаётся."""
    ours = offer("t", SELLER, 1500.0, 25000.0, "DeepSeek 25B токенов")
    rivals = [
        offer(str(i), f"9{i}", 5.0, 10.0, "DeepSeek 10M токенов") for i in range(5)
    ]
    assert build_price_actions(make_market([ours, *rivals]), threshold_pct=15) == []


def test_lot_without_volume_is_ignored() -> None:
    ours = offer("t", SELLER, 2999.0, None, "Безлимит 30 дней DeepSeek")
    rivals = [offer(str(i), f"9{i}", 40.0, 100.0, "DeepSeek 100M токенов") for i in range(5)]
    assert build_price_actions(make_market([ours, *rivals]), threshold_pct=15) == []


def test_categories_with_same_name_are_distinguished() -> None:
    """Две «Подписки» разных игр не должны склеиваться в один список."""
    a = offer("a", SELLER, 10.0, 100.0, "DeepSeek 100M токенов")
    b = offer("b", SELLER, 10.0, 100.0, "DeepSeek 100M токенов")
    page_a = CategoryPage(
        category_id="100",
        category_name="Токены",
        game_name="ChatGPT",
        offers=[a, *[offer(f"x{i}", f"7{i}", 40.0, 100.0, "DeepSeek 100M токенов") for i in range(5)]],
    )
    page_b = CategoryPage(
        category_id="200",
        category_name="Токены",
        game_name="Claude",
        offers=[b, *[offer(f"y{i}", f"6{i}", 40.0, 100.0, "DeepSeek 100M токенов") for i in range(5)]],
    )
    market = analyze_market([page_a, page_b], target_seller_id=SELLER)
    actions = build_price_actions(market, threshold_pct=15)

    assert {a.category_id for a in actions} == {"100", "200"}
    assert {a.game for a in actions} == {"ChatGPT", "Claude"}
