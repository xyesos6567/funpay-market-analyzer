"""Агрегация лотов по продавцам и скоринг конкурентов.

FunPay не показывает число продаж, поэтому спрос оценивается косвенно —
через число отзывов продавца. Это прокси: он не идеален, но именно он
определяет, кого покупатель видит первым.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from .parse import Offer

log = logging.getLogger(__name__)


def model_family(model_name: str) -> str:
    """Семейство модели: 'DeepSeek V4 Flash' -> 'DeepSeek'.

    Нужно, чтобы сопоставлять лоты конкурентов между собой, не смешивая
    разные сервисы (нельзя сравнивать цену GLM с ценой Claude).
    """
    if not model_name:
        return ""
    return model_name.split()[0].strip()


@dataclass
class SellerProfile:
    seller_id: str
    name: str = ""
    category_id: str = ""
    offers: list[Offer] = field(default_factory=list)

    max_reviews: int = 0
    rating: float | None = None
    online: bool = False
    auto_share: float = 0.0
    promo_count: int = 0
    seller_info: str = ""

    score: float = 0.0
    is_target: bool = False

    @property
    def offer_count(self) -> int:
        return len(self.offers)

    @property
    def price_min(self) -> float | None:
        prices = [o.price for o in self.offers if o.price is not None]
        return min(prices) if prices else None

    @property
    def price_max(self) -> float | None:
        prices = [o.price for o in self.offers if o.price is not None]
        return max(prices) if prices else None

    @property
    def families(self) -> list[str]:
        found: list[str] = []
        for offer in self.offers:
            for model in offer.detected_models:
                fam = model_family(model)
                if fam and fam not in found:
                    found.append(fam)
        return found


def build_seller_profiles(offers: list[Offer], target_seller_id: str) -> dict[str, SellerProfile]:
    """Группирует лоты по продавцам внутри одной категории."""
    profiles: dict[str, SellerProfile] = {}
    for offer in offers:
        if not offer.seller_id:
            continue
        profile = profiles.get(offer.seller_id)
        if profile is None:
            profile = SellerProfile(
                seller_id=offer.seller_id,
                name=offer.seller_name,
                category_id=offer.category_id,
                is_target=offer.seller_id == target_seller_id,
            )
            profiles[offer.seller_id] = profile
        profile.offers.append(offer)
        profile.max_reviews = max(profile.max_reviews, offer.seller_reviews)
        if offer.seller_rating is not None:
            profile.rating = max(profile.rating or 0.0, offer.seller_rating)
        profile.online = profile.online or offer.seller_online
        profile.promo_count += 1 if offer.promoted else 0
        if offer.seller_info and not profile.seller_info:
            profile.seller_info = offer.seller_info

    for profile in profiles.values():
        if profile.offers:
            auto = sum(1 for o in profile.offers if o.auto_delivery)
            profile.auto_share = auto / len(profile.offers)
    return profiles


def competitor_score(profile: SellerProfile, max_reviews: int) -> float:
    """Композитный балл силы конкурента, 0..100.

    Веса: отзывы 40 (главный сигнал спроса), рейтинг 20, онлайн 15,
    автовыдача 15, промо 10.
    """
    # Отзывы: логарифмическая шкала, чтобы 3000 отзывов не убивали всех остальных.
    if max_reviews > 0 and profile.max_reviews > 0:
        reviews_part = math.log10(profile.max_reviews + 1) / math.log10(max_reviews + 1)
    else:
        reviews_part = 0.0

    rating_part = (profile.rating or 0.0) / 5.0
    online_part = 1.0 if profile.online else 0.0
    auto_part = profile.auto_share
    promo_part = min(1.0, profile.promo_count / max(1, profile.offer_count))

    return round(
        100.0
        * (
            0.40 * reviews_part
            + 0.20 * rating_part
            + 0.15 * online_part
            + 0.15 * auto_part
            + 0.10 * promo_part
        ),
        1,
    )


def score_sellers(profiles: dict[str, SellerProfile]) -> None:
    """Проставляет балл всем продавцам категории (на месте)."""
    max_reviews = max((p.max_reviews for p in profiles.values()), default=0)
    for profile in profiles.values():
        profile.score = competitor_score(profile, max_reviews)


def top_competitors(
    profiles: dict[str, SellerProfile], limit: int, exclude_target: bool = True
) -> list[SellerProfile]:
    ranked = [p for p in profiles.values() if not (exclude_target and p.is_target)]
    ranked.sort(key=lambda p: (-p.score, -p.max_reviews, -p.offer_count))
    return ranked[:limit]
