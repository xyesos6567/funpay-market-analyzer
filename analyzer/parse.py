"""Разбор HTML-страниц FunPay в структурированные объекты."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


class ParseError(Exception):
    """Разметка страницы не соответствует ожидаемой.

    Отдельный тип нужен, чтобы падение парсера было явной ошибкой,
    а не тихим пустым отчётом.
    """


_LOTS_ID = re.compile(r"/lots/(\d+)/")
_OFFER_ID = re.compile(r"id=(\d+)")
_USERS_ID = re.compile(r"/users/(\d+)/")
_PRICE_NUM = re.compile(r"(\d[\d\u00a0\u202f ]*(?:[.,]\d+)?)")


def parse_price(text: str) -> float | None:
    """Достаёт число из строки цены вида '1 500 ₽', '5.09', 'от 100 ₽ за 1 шт'."""
    if not text:
        return None
    m = _PRICE_NUM.search(text)
    if not m:
        return None
    raw = m.group(1).replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    raw = raw.replace(",", ".")
    # Отбрасываем хвостовые точки вида "1500."
    raw = raw.rstrip(".")
    try:
        return float(raw)
    except ValueError:
        return None


@dataclass
class Offer:
    """Один лот (предложение) на FunPay."""

    offer_id: str
    url: str
    description: str = ""
    price: float | None = None
    currency: str = "RUB"
    category_id: str = ""
    category_name: str = ""

    seller_id: str = ""
    seller_name: str = ""
    seller_rating: float | None = None
    seller_reviews: int = 0
    seller_info: str = ""
    seller_online: bool = False
    seller_avatar: str = ""

    auto_delivery: bool = False
    promoted: bool = False
    model: str = ""

    # Заполняется позже в normalize.py
    tokens_millions: float | None = None
    price_per_million: float | None = None
    detected_models: list[str] = field(default_factory=list)

    @property
    def seller_url(self) -> str:
        return f"https://funpay.com/users/{self.seller_id}/" if self.seller_id else ""


@dataclass
class CategoryPage:
    category_id: str
    title: str = ""
    game_name: str = ""
    category_name: str = ""
    offers: list[Offer] = field(default_factory=list)


def _text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def _offer_from_tag(tag, category_id: str, category_name: str, default_seller: str = "") -> Offer | None:
    href = tag.get("href", "")
    m = _OFFER_ID.search(href)
    if not m:
        return None

    offer = Offer(
        offer_id=m.group(1),
        url=href if href.startswith("http") else f"https://funpay.com{href}",
        category_id=str(category_id),
        category_name=category_name,
    )

    desc = tag.select_one(".tc-desc-text")
    offer.description = _text(desc)

    # Цена: сначала видимый текст (он авторитетнее), потом data-s как запасной вариант.
    price_div = tag.select_one(".tc-price")
    if price_div is not None:
        inner = price_div.find("div")
        visible = _text(inner) if inner is not None else _text(price_div)
        offer.price = parse_price(visible)
        if offer.price is None and price_div.get("data-s"):
            try:
                offer.price = round(float(price_div["data-s"]), 2)
            except ValueError:
                offer.price = None

    # Продавец: data-user на карточке, иначе id из аватара.
    sid = tag.get("data-user", "")
    if not sid:
        avatar = tag.select_one(".avatar-photo")
        if avatar and avatar.get("data-href"):
            mm = _USERS_ID.search(avatar["data-href"])
            if mm:
                sid = mm.group(1)
                offer.seller_avatar = avatar["data-href"]
    offer.seller_id = str(sid or default_seller)

    offer.seller_name = _text(tag.select_one(".media-user-name"))

    stars = tag.select_one(".rating-stars")
    if stars:
        for cls in stars.get("class", []):
            mm = re.match(r"rating-(\d+)", cls)
            if mm:
                offer.seller_rating = float(mm.group(1))
                break

    cnt = tag.select_one(".rating-mini-count")
    if cnt:
        mm = re.search(r"\d+", _text(cnt))
        offer.seller_reviews = int(mm.group(0)) if mm else 0

    info = tag.select_one(".media-user-info")
    offer.seller_info = _text(info)

    user_block = tag.select_one(".media-user")
    offer.seller_online = bool(user_block and "online" in user_block.get("class", []))

    offer.auto_delivery = tag.get("data-auto") == "1"
    offer.model = tag.get("data-f-model", "")
    offer.promoted = "offer-promoted" in tag.get("class", []) or bool(
        tag.select_one(".promo-offer-icon")
    )

    return offer


def parse_category_page(html: str, category_id: str, category_name: str = "") -> CategoryPage:
    """Разбирает страницу категории: заголовок, игру и все лоты."""
    soup = BeautifulSoup(html, "lxml")
    page = CategoryPage(category_id=str(category_id))

    if soup.title:
        title = soup.title.get_text(strip=True)
        page.title = title.replace("/ FunPay", "").strip()
    if not category_name:
        category_name = page.title
    page.category_name = category_name

    # Игра и подкатегория из JSON-LD BreadcrumbList.
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except (ValueError, TypeError):
            continue
        blocks = data if isinstance(data, list) else [data]
        for block in blocks:
            if isinstance(block, dict) and block.get("@type") == "BreadcrumbList":
                parts = [el.get("name", "") for el in block.get("itemListElement", [])]
                if parts:
                    page.game_name = parts[0]
                    if len(parts) > 1 and not category_name:
                        page.category_name = parts[-1]

    items = soup.select("a.tc-item")
    if not items:
        # Явная ошибка: либо страница-заглушка, либо изменилась разметка.
        raise ParseError(
            f"На странице категории {category_id} не найдено ни одного лота (a.tc-item). "
            "Вероятно, изменилась разметка FunPay или страница не загрузилась."
        )

    for tag in items:
        offer = _offer_from_tag(tag, category_id, page.category_name)
        if offer is not None:
            page.offers.append(offer)

    # В разметке FunPay бывают дубли карточек (одна и та же ссылка на оффер).
    # Без дедупликации они искажают распределение цен и число конкурентов.
    unique: dict[str, Offer] = {}
    for offer in page.offers:
        unique.setdefault(offer.offer_id, offer)
    page.offers = list(unique.values())

    log.info("Категория %s (%s): разобрано %d лотов", category_id, page.category_name, len(page.offers))
    return page


def parse_seller_offers(html: str, seller_id: str) -> list[Offer]:
    """Разбирает лоты со страницы профиля продавца.

    Лоты сгруппированы по категориям внутри блоков div.offer,
    поэтому категорию берём из заголовка группы.
    """
    soup = BeautifulSoup(html, "lxml")
    offers: list[Offer] = []

    groups = soup.select("div.offer")
    if not groups:
        raise ParseError(
            "На странице продавца не найдено ни одной группы лотов (div.offer). "
            "Вероятно, изменилась разметка FunPay."
        )

    for group in groups:
        title_tag = group.select_one(".offer-list-title h3 a")
        cat_id = ""
        cat_name = _text(title_tag)
        if title_tag and title_tag.get("href"):
            m = _LOTS_ID.search(title_tag["href"])
            if m:
                cat_id = m.group(1)
        for tag in group.select("a.tc-item"):
            offer = _offer_from_tag(tag, cat_id, cat_name, default_seller=seller_id)
            if offer is not None:
                offers.append(offer)

    log.info("Профиль %s: разобрано %d лотов", seller_id, len(offers))
    return offers


@dataclass
class CatalogGame:
    game_id: str
    name: str
    url: str
    subcategories: list[tuple[str, str]] = field(default_factory=list)  # (id, name)


def parse_catalog(html: str) -> list[CatalogGame]:
    """Разбирает каталог с главной страницы: игры и их подкатегории."""
    soup = BeautifulSoup(html, "lxml")
    games: list[CatalogGame] = []

    for item in soup.find_all(class_="promo-game-item"):
        title = item.find(class_="game-title")
        if not title:
            continue
        link = title.find("a", href=True)
        game = CatalogGame(
            game_id=title.get("data-id", ""),
            name=title.get_text(" ", strip=True),
            url=link["href"] if link else "",
        )
        ul = item.find("ul")
        if ul:
            for a in ul.find_all("a", href=True):
                m = _LOTS_ID.search(a["href"])
                if m:
                    game.subcategories.append((m.group(1), a.get_text(" ", strip=True)))
        games.append(game)

    if not games:
        raise ParseError(
            "В каталоге на главной странице не найдено ни одной игры (promo-game-item). "
            "Вероятно, изменилась разметка FunPay."
        )
    return games
