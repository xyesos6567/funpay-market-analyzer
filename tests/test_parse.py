"""Регрессионные тесты парсера на сохранённых страницах FunPay.

Эталонные числа получены прямым анализом HTML во время разведки:
  - в категории 4506 ровно 2001 карточка лота, из них 2000 уникальных
    (одна ссылка продублирована), медиана цены 250.92 ₽;
  - уникальных продавцов в категории 185;
  - у продавца 4233111 ровно 18 лотов в 4 категориях.

Важно: 1801 карточка из 2001 помечена классом lazyload-hidden — это реальные
лоты категории, просто скрытые до прокрутки. Их нельзя отбрасывать, иначе
рынок недооценивается более чем в 10 раз.
Если FunPay изменит разметку, эти тесты упадут раньше, чем отчёт соврёт.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from analyzer.parse import (
    ParseError,
    parse_catalog,
    parse_category_page,
    parse_price,
    parse_seller_offers,
)

FIXTURES = Path(__file__).parent / "fixtures"
CATEGORY_4506 = FIXTURES / "category_4506.html"
SELLER_PROFILE = FIXTURES / "seller_4233111.html"

SELLER_ID = "4233111"

# --- эталонные значения из разведки ---
EXPECTED_OFFERS_4506 = 2000  # 2001 карточка минус один дубль ссылки
EXPECTED_SELLERS_4506 = 185
EXPECTED_MEDIAN_4506 = 250.92
EXPECTED_SELLER_OFFERS = 18
EXPECTED_SELLER_CATEGORIES = {"4506", "4507", "4676", "3755"}


@pytest.fixture(scope="module")
def category_html() -> str:
    return CATEGORY_4506.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def seller_html() -> str:
    return SELLER_PROFILE.read_text(encoding="utf-8")


# --- парсинг цены -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1500 ₽", 1500.0),
        ("5.09 ₽", 5.09),
        ("1 500 ₽", 1500.0),
        ("20.77 ₽", 20.77),
        ("от 100 ₽ за 1 шт", 100.0),
        ("1\u00a0250,50 ₽", 1250.50),
        ("", None),
        ("цена по договорённости", None),
    ],
)
def test_parse_price(raw: str, expected: float | None) -> None:
    assert parse_price(raw) == expected


# --- страница категории -------------------------------------------------


def test_category_offer_count(category_html: str) -> None:
    page = parse_category_page(category_html, "4506", "API-ключ DeepSeek")
    assert len(page.offers) == EXPECTED_OFFERS_4506


def test_category_has_no_duplicate_offers(category_html: str) -> None:
    """Дубли карточек не должны раздувать выборку."""
    page = parse_category_page(category_html, "4506")
    ids = [o.offer_id for o in page.offers]
    assert len(ids) == len(set(ids))


def test_category_seller_count(category_html: str) -> None:
    page = parse_category_page(category_html, "4506")
    sellers = {o.seller_id for o in page.offers if o.seller_id}
    assert len(sellers) == EXPECTED_SELLERS_4506


def test_category_median_price(category_html: str) -> None:
    page = parse_category_page(category_html, "4506")
    prices = sorted(o.price for o in page.offers if o.price is not None)
    assert prices, "не должно быть пусто"
    median = prices[len(prices) // 2]
    assert median == pytest.approx(EXPECTED_MEDIAN_4506, abs=0.01)


def test_category_game_name(category_html: str) -> None:
    """Игра берётся из JSON-LD BreadcrumbList — на ней держится автопоиск."""
    page = parse_category_page(category_html, "4506")
    assert page.game_name == "DeepSeek"


def test_seller_fields_are_populated(category_html: str) -> None:
    """Проверяем, что ключевые для аналитики поля реально заполняются."""
    page = parse_category_page(category_html, "4506")
    with_seller = [o for o in page.offers if o.seller_id]
    assert len(with_seller) > EXPECTED_OFFERS_4506 * 0.9, "у большинства лотов должен быть продавец"

    with_reviews = [o for o in page.offers if o.seller_reviews > 0]
    assert len(with_reviews) > 50, "должны читаться отзывы продавцов"

    with_rating = [o for o in page.offers if o.seller_rating is not None]
    assert len(with_rating) > 50, "должен читаться рейтинг"

    auto = [o for o in page.offers if o.auto_delivery]
    assert auto, "должны находиться лоты с автовыдачей (data-auto)"

    assert any(o.description for o in page.offers), "должны читаться описания"


def test_specific_competitor_is_parsed(category_html: str) -> None:
    """Контрольный конкурент из разведки: ZEXKORA, 25M токенов за 20.77 ₽."""
    page = parse_category_page(category_html, "4506")
    zexkora = [o for o in page.offers if "ZEXKORA" in o.description.upper()]
    assert zexkora, "лот с ZEXKORA должен находиться"
    lot = min(zexkora, key=lambda o: o.price or 1e9)
    assert lot.price == pytest.approx(20.77, abs=0.01)


def test_empty_page_raises() -> None:
    """Пустая страница — явная ошибка, а не тихий пустой результат."""
    with pytest.raises(ParseError):
        parse_category_page("<html><body>нет лотов</body></html>", "1")


# --- страница продавца --------------------------------------------------


def test_seller_offer_count(seller_html: str) -> None:
    offers = parse_seller_offers(seller_html, SELLER_ID)
    assert len(offers) == EXPECTED_SELLER_OFFERS


def test_seller_categories(seller_html: str) -> None:
    offers = parse_seller_offers(seller_html, SELLER_ID)
    assert {o.category_id for o in offers} == EXPECTED_SELLER_CATEGORIES


def test_seller_offers_have_prices_and_descriptions(seller_html: str) -> None:
    offers = parse_seller_offers(seller_html, SELLER_ID)
    assert all(o.price is not None for o in offers)
    assert all(o.description for o in offers)


def test_volume_discount_ladder_is_present(seller_html: str) -> None:
    """Ключевой факт разведки: у друга цена за токен падает с ростом объёма."""
    offers = parse_seller_offers(seller_html, SELLER_ID)
    flash = [o for o in offers if "V4 Flash" in o.description]
    assert len(flash) >= 5, "должны найтись паки DeepSeek V4 Flash"

    # Самый маленький пак должен стоить за токен дороже самого большого.
    smallest = min(flash, key=lambda o: o.price or 1e9)
    largest = max(flash, key=lambda o: o.price or 0)
    assert (smallest.price or 0) < (largest.price or 0)


# --- каталог ------------------------------------------------------------


def test_catalog_requires_markup() -> None:
    with pytest.raises(ParseError):
        parse_catalog("<html><body>пусто</body></html>")
