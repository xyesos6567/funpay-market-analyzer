"""Аналитика рынка: распределения цен, позиционирование, свободные ниши."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from .competitors import (
    SellerProfile,
    build_seller_profiles,
    model_family,
    score_sellers,
    top_competitors,
)
from .parse import Offer

log = logging.getLogger(__name__)

# Границы корзин для цен лота (₽). Логарифмическая лестница: цены в нише
# отличаются на порядки, равномерные корзины сделали бы график бесполезным.
PRICE_LADDER: tuple[float, ...] = (
    1, 3, 10, 30, 100, 300, 1000, 3000, 10000, 30000, 100000, 300000, 1_000_000,
)

# Границы корзин для цены за 1M токенов (₽).
UNIT_PRICE_LADDER: tuple[float, ...] = (
    0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1, 3, 10, 30, 100, 300, 1000,
)


def percentile(sorted_values: list[float], q: float) -> float | None:
    """Квантиль по уже отсортированному списку (q в долях 0..1)."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return sorted_values[int(pos)]
    frac = pos - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


def percentile_rank(values: list[float], value: float) -> float | None:
    """Позиция значения в выборке: 0 — дешевле всех, 100 — дороже всех."""
    if not values:
        return None
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return round((below + 0.5 * equal) / len(values) * 100, 1)


@dataclass
class Stats:
    count: int = 0
    minimum: float | None = None
    p10: float | None = None
    p25: float | None = None
    median: float | None = None
    p75: float | None = None
    p90: float | None = None
    maximum: float | None = None
    mean: float | None = None

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "min": self.minimum,
            "p10": self.p10,
            "p25": self.p25,
            "median": self.median,
            "p75": self.p75,
            "p90": self.p90,
            "max": self.maximum,
            "mean": self.mean,
        }


def compute_stats(values: list[float]) -> Stats:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return Stats()
    return Stats(
        count=len(clean),
        minimum=round(clean[0], 4),
        p10=round(percentile(clean, 0.10) or 0, 4),
        p25=round(percentile(clean, 0.25) or 0, 4),
        median=round(percentile(clean, 0.50) or 0, 4),
        p75=round(percentile(clean, 0.75) or 0, 4),
        p90=round(percentile(clean, 0.90) or 0, 4),
        maximum=round(clean[-1], 4),
        mean=round(sum(clean) / len(clean), 4),
    )


@dataclass
class Bucket:
    low: float
    high: float
    label: str
    count: int = 0
    reviews: int = 0

    @property
    def reviews_per_offer(self) -> float:
        return self.reviews / self.count if self.count else 0.0


def _fmt(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f}".replace(",", " ")
    if value >= 1:
        return f"{value:g}"
    return f"{value:.3g}"


def build_buckets(values: list[float], offers: list[Offer], ladder: tuple[float, ...]) -> list[Bucket]:
    """Строит гистограмму по логарифмической лестнице границ."""
    buckets: list[Bucket] = []
    for i in range(len(ladder) - 1):
        low, high = ladder[i], ladder[i + 1]
        buckets.append(Bucket(low=low, high=high, label=f"{_fmt(low)}–{_fmt(high)}"))
    # Хвост за пределами лестницы.
    buckets.append(
        Bucket(low=ladder[-1], high=float("inf"), label=f">{_fmt(ladder[-1])}")
    )
    buckets.insert(0, Bucket(low=0.0, high=ladder[0], label=f"<{_fmt(ladder[0])}"))

    for value, offer in zip(values, offers, strict=False):
        if value != value:  # NaN
            continue
        for bucket in buckets:
            if bucket.low <= value < bucket.high:
                bucket.count += 1
                bucket.reviews += offer.seller_reviews
                break
    return buckets


@dataclass
class PositionInfo:
    """Положение лота продавца относительно рынка."""

    offer: Offer
    family: str = ""
    price_percentile: float | None = None
    unit_price_percentile: float | None = None
    comparable_count: int = 0
    comparable_median_unit_price: float | None = None
    comparable_median_price: float | None = None
    deviation_pct: float | None = None
    # 'volume' — аналоги сопоставимы и по объёму, 'family' — только по модели,
    # 'none' — прямых аналогов нет. Влияет на то, насколько можно доверять выводу.
    scope: str = "none"
    volume_range: str = ""

    @property
    def is_overpriced(self) -> bool:
        return self.deviation_pct is not None and self.deviation_pct > 0

    @property
    def is_underpriced(self) -> bool:
        return self.deviation_pct is not None and self.deviation_pct < 0


@dataclass
class CategoryAnalysis:
    category_id: str
    name: str
    game: str = ""
    offers: list[Offer] = field(default_factory=list)
    sellers: dict[str, SellerProfile] = field(default_factory=dict)
    price_stats: Stats = field(default_factory=Stats)
    unit_price_stats: Stats = field(default_factory=Stats)
    price_histogram: list[Bucket] = field(default_factory=list)
    unit_price_histogram: list[Bucket] = field(default_factory=list)
    model_counts: dict[str, int] = field(default_factory=dict)
    auto_share: float = 0.0
    promo_share: float = 0.0
    online_share: float = 0.0
    offers_with_volume: int = 0
    target_offers: list[Offer] = field(default_factory=list)
    positions: list[PositionInfo] = field(default_factory=list)
    competitors: list[SellerProfile] = field(default_factory=list)
    opportunity_buckets: list[Bucket] = field(default_factory=list)

    @property
    def seller_count(self) -> int:
        return len(self.sellers)

    @property
    def is_representative(self) -> bool:
        return len(self.offers) > 0


# Диапазоны объёма для поиска сопоставимых лотов, от узкого к широкому.
# Сравнивать цену пака на 25B с паком на 25M нельзя: это разные продукты
# с разной экономикой, и вывод «вы в 30 раз дешевле» был бы вредным советом.
VOLUME_BANDS: tuple[tuple[float, float], ...] = ((0.6, 1.7), (0.4, 2.5), (0.2, 5.0))


def _family_of(offer: Offer) -> str:
    if offer.detected_models:
        return model_family(offer.detected_models[0])
    return ""


def _find_comparables(offer: Offer, offers: list[Offer]) -> tuple[list[Offer], str, str]:
    """Ищет лоты конкурентов, сопоставимые с данным.

    Возвращает (лоты, масштаб сравнения, описание диапазона объёма).
    Сначала пытаемся найти аналоги того же объёма, затем постепенно расширяем
    диапазон. Если объём у лота неизвестен, сравниваем только по модели.
    """
    family = _family_of(offer)
    if not family:
        return [], "none", ""

    pool = [
        other
        for other in offers
        if other.offer_id != offer.offer_id
        and other.price_per_million is not None
        and _family_of(other) == family
    ]
    if not pool:
        return [], "none", ""

    if not offer.tokens_millions:
        return pool, "family", "объём не определён"

    for low, high in VOLUME_BANDS:
        band = [
            o
            for o in pool
            if o.tokens_millions
            and low * offer.tokens_millions <= o.tokens_millions <= high * offer.tokens_millions
        ]
        if len(band) >= 3:
            return band, "volume", f"{low:g}×–{high:g}× от вашего объёма"

    # Близких по объёму предложений на рынке нет — это само по себе результат.
    widest = [
        o
        for o in pool
        if o.tokens_millions
    ]
    if len(widest) >= 3:
        return widest, "family", "аналогов вашего объёма на рынке нет"
    return [], "none", ""


def analyze_category(
    category_id: str,
    name: str,
    game: str,
    offers: list[Offer],
    target_seller_id: str,
    top_n: int = 10,
) -> CategoryAnalysis:
    analysis = CategoryAnalysis(category_id=category_id, name=name, game=game, offers=offers)

    prices = [o.price for o in offers if o.price is not None]
    analysis.price_stats = compute_stats(prices)

    unit_offers = [o for o in offers if o.price_per_million is not None]
    analysis.offers_with_volume = len(unit_offers)
    analysis.unit_price_stats = compute_stats([o.price_per_million for o in unit_offers])

    analysis.price_histogram = build_buckets(
        [o.price for o in offers if o.price is not None],
        [o for o in offers if o.price is not None],
        PRICE_LADDER,
    )
    analysis.unit_price_histogram = build_buckets(
        [o.price_per_million for o in unit_offers],
        unit_offers,
        UNIT_PRICE_LADDER,
    )

    for offer in offers:
        for model in offer.detected_models:
            analysis.model_counts[model] = analysis.model_counts.get(model, 0) + 1

    if offers:
        analysis.auto_share = sum(1 for o in offers if o.auto_delivery) / len(offers)
        analysis.promo_share = sum(1 for o in offers if o.promoted) / len(offers)
        analysis.online_share = sum(1 for o in offers if o.seller_online) / len(offers)

    analysis.sellers = build_seller_profiles(offers, target_seller_id)
    score_sellers(analysis.sellers)
    analysis.competitors = top_competitors(analysis.sellers, top_n)

    analysis.target_offers = [o for o in offers if o.seller_id == target_seller_id]
    analysis.positions = [
        _build_position(offer, offers) for offer in analysis.target_offers
    ]

    analysis.opportunity_buckets = _find_opportunities(analysis.unit_price_histogram)
    return analysis


def _build_position(offer: Offer, offers: list[Offer]) -> PositionInfo:
    info = PositionInfo(offer=offer, family=_family_of(offer))

    all_prices = [o.price for o in offers if o.price is not None]
    info.price_percentile = percentile_rank(all_prices, offer.price) if offer.price is not None else None

    if offer.price_per_million is not None:
        all_unit = [o.price_per_million for o in offers if o.price_per_million is not None]
        info.unit_price_percentile = percentile_rank(all_unit, offer.price_per_million)

    comparables, scope, volume_range = _find_comparables(offer, offers)
    info.comparable_count = len(comparables)
    info.scope = scope
    info.volume_range = volume_range
    if comparables:
        comp_unit = sorted(o.price_per_million for o in comparables)  # type: ignore[misc]
        comp_price = sorted(o.price for o in comparables if o.price is not None)
        info.comparable_median_unit_price = percentile(comp_unit, 0.5)
        info.comparable_median_price = percentile(comp_price, 0.5) if comp_price else None

        if offer.price_per_million and info.comparable_median_unit_price:
            median_unit = info.comparable_median_unit_price
            info.deviation_pct = round(
                (offer.price_per_million - median_unit) / median_unit * 100, 1
            )
    return info


def _find_opportunities(buckets: list[Bucket]) -> list[Bucket]:
    """Корзины, где спрос выше среднего, а предложение ниже среднего.

    Спрос оценивается числом отзывов продавцов в корзине (прокси реальных продаж).
    """
    filled = [b for b in buckets if b.count > 0]
    if len(filled) < 3:
        return []
    avg_count = sum(b.count for b in filled) / len(filled)
    avg_reviews_per_offer = sum(b.reviews_per_offer for b in filled) / len(filled)
    return [
        b
        for b in filled
        if b.count < avg_count and b.reviews_per_offer > avg_reviews_per_offer
    ]


@dataclass
class MarketReport:
    categories: list[CategoryAnalysis] = field(default_factory=list)
    target_seller_id: str = ""
    target_seller_name: str = ""
    brand: str = ""
    total_offers: int = 0
    total_sellers: int = 0

    @property
    def categories_with_target(self) -> list[CategoryAnalysis]:
        return [c for c in self.categories if c.target_offers]


def analyze_market(
    pages: list,  # list[CategoryPage]
    target_seller_id: str,
    target_seller_name: str = "",
    brand: str = "",
    top_n: int = 10,
) -> MarketReport:
    report = MarketReport(
        target_seller_id=target_seller_id,
        target_seller_name=target_seller_name,
        brand=brand,
    )
    all_sellers: set[str] = set()
    for page in pages:
        analysis = analyze_category(
            page.category_id,
            page.category_name or page.title,
            getattr(page, "game_name", ""),
            page.offers,
            target_seller_id,
            top_n=top_n,
        )
        report.categories.append(analysis)
        report.total_offers += len(page.offers)
        all_sellers.update(o.seller_id for o in page.offers if o.seller_id)
    report.total_sellers = len(all_sellers)
    return report
