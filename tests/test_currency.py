"""Тесты валюты цен.

Реальный случай: с сервера GitHub FunPay отдал цены в долларах, а программа
посчитала их рублями. Все цены оказались в 82 раза меньше, и рекомендации
стали бессмысленными. Тесты фиксируют, что такая ошибка больше не пройдёт
незамеченной.
"""

from __future__ import annotations

import pytest

from analyzer.parse import (
    ParseError,
    currency_code,
    parse_category_page,
    parse_seller_offers,
)

RUBLE_PAGE = """
<html><head><title>Токены / FunPay</title></head><body>
<div class="tc"><a href="/lots/offer?id=1" class="tc-item" data-user="111">
  <div class="tc-desc"><div class="tc-desc-text">DeepSeek 100M токенов</div></div>
  <div class="tc-price" data-s="1500.00"><div>1500 <span class="unit">₽</span></div></div>
  <div class="media-user online"><div class="media-user-name">seller</div><div class="media-user-info">онлайн</div></div>
</a></div></body></html>
"""

DOLLAR_PAGE = RUBLE_PAGE.replace("<span class=\"unit\">₽</span>", "<span class=\"unit\">$</span>")

EURO_PAGE = RUBLE_PAGE.replace("<span class=\"unit\">₽</span>", "<span class=\"unit\">€</span>")


@pytest.mark.parametrize(
    ("symbol", "code"),
    [
        ("₽", "RUB"),
        ("руб", "RUB"),
        ("$", "USD"),
        ("€", "EUR"),
        ("₴", "UAH"),
        ("usd", "USD"),
        ("", ""),
    ],
)
def test_currency_code(symbol: str, code: str) -> None:
    assert currency_code(symbol) == code


def test_ruble_price_is_parsed_with_currency() -> None:
    page = parse_category_page(RUBLE_PAGE, "1", expected_currency="RUB")
    offer = page.offers[0]
    assert offer.price == 1500.0
    assert offer.currency == "RUB"


def test_dollar_prices_are_rejected() -> None:
    """Именно этот случай дал цены в 82 раза меньше реальных."""
    with pytest.raises(ParseError) as exc:
        parse_category_page(DOLLAR_PAGE, "1", expected_currency="RUB")

    message = str(exc.value)
    assert "USD" in message
    assert "cy" in message, "ошибка должна подсказывать, где искать причину"


def test_euro_prices_are_rejected() -> None:
    with pytest.raises(ParseError):
        parse_category_page(EURO_PAGE, "1", expected_currency="RUB")


def test_currency_check_can_be_disabled() -> None:
    """Если ожидаемая валюта не задана, проверка не блокирует разбор."""
    page = parse_category_page(DOLLAR_PAGE, "1", expected_currency="")
    assert page.offers[0].currency == "USD"


def test_seller_profile_currency_is_checked() -> None:
    seller_html = DOLLAR_PAGE.replace(
        '<div class="offer">', ""
    ).replace(
        "</div></body></html>",
        "</div></body></html>",
    )
    # Минимальная структура профиля: группа лотов внутри div.offer
    profile = (
        '<html><body><div class="offer">'
        '<div class="offer-list-title"><h3><a href="https://funpay.com/lots/4506/">API-ключ</a></h3></div>'
        '<a href="/lots/offer?id=5" class="tc-item">'
        '<div class="tc-desc"><div class="tc-desc-text">DeepSeek 100M токенов</div></div>'
        '<div class="tc-price" data-s="18.19"><div>18.19 <span class="unit">$</span></div></div>'
        "</a></div></body></html>"
    )
    with pytest.raises(ParseError):
        parse_seller_offers(profile, "4233111", expected_currency="RUB")
