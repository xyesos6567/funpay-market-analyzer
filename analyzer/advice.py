"""Движок рекомендаций: превращает аналитику в конкретный список изменений.

Каждая рекомендация содержит приоритет, действие и числа, из которых сделан вывод.
Правила работают только на данных — без внешних API и без домыслов.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .analytics import CategoryAnalysis, MarketReport, PositionInfo
from .parse import Offer

log = logging.getLogger(__name__)

P0, P1, P2 = "P0", "P1", "P2"

# Словарь смыслов, которые продавцы используют как аргументы продажи.
# Проверяем их наличие у анализируемого продавца против конкурентов сверху.
SALES_ARGUMENTS: tuple[str, ...] = (
    "авто",
    "гарант",
    "замен",
    "поддержк",
    "24/7",
    "без vpn",
    "возврат",
    "пробник",
    "сбп",
    "карт",
    "бесплатн",
    "тест",
    "выгодн",
    "лимит",
    "мгновенн",
    "инструкц",
    "ресурс",
    "пополнен",
)


@dataclass
class PriceAction:
    """Конкретное действие с ценой: поднять или опустить.

    Отдельная структура нужна, чтобы в отчёте был явный список изменений,
    а не текст, из которого надо вычитывать числа.
    """

    direction: str  # 'up' | 'down'
    category_id: str
    category_name: str
    game: str
    offer_title: str
    volume: str
    current_price: float | None
    current_unit: float | None
    target_unit: float | None
    target_price: float | None
    delta_pct: float
    impact_rub: float
    comparables: int
    reason: str

    @property
    def new_price(self) -> float | None:
        return self.target_price

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "category_id": self.category_id,
            "category_name": self.category_name,
            "game": self.game,
            "offer_title": self.offer_title,
            "volume": self.volume,
            "current_price": self.current_price,
            "current_unit": self.current_unit,
            "target_unit": self.target_unit,
            "target_price": self.target_price,
            "delta_pct": self.delta_pct,
            "impact_rub": self.impact_rub,
            "comparables": self.comparables,
            "reason": self.reason,
        }


def _volume_label(millions: float | None) -> str:
    if not millions:
        return "—"
    if millions >= 1000:
        return f"{millions / 1000:g}B"
    return f"{millions:g}M"


def build_price_actions(report: MarketReport, threshold_pct: float) -> list[PriceAction]:
    """Собирает список ценовых действий по всем категориям.

    Учитываются только лоты с прямыми аналогами по объёму: там сравнение
    корректно. Сортировка — по сумме денег на кону, чтобы сверху было то,
    что даёт наибольший эффект.
    """
    actions: list[PriceAction] = []
    for analysis in report.categories:
        for pos in analysis.positions:
            offer = pos.offer
            if pos.scope != "volume" or pos.deviation_pct is None:
                continue
            if abs(pos.deviation_pct) < threshold_pct:
                continue
            target_unit = pos.comparable_median_unit_price
            target_price = _target_price(offer, target_unit)
            if target_unit is None or target_price is None or offer.price is None:
                continue

            delta = target_price - offer.price
            direction = "up" if delta > 0 else "down"
            if direction == "up":
                reason = (
                    f"вы дешевле аналогов на {abs(pos.deviation_pct):.0f}% — "
                    f"есть запас поднять цену без потери позиции"
                )
            else:
                reason = (
                    f"вы дороже аналогов на {pos.deviation_pct:.0f}% — "
                    f"цена выше медианы сопоставимых лотов"
                )

            actions.append(
                PriceAction(
                    direction=direction,
                    category_id=analysis.category_id,
                    category_name=analysis.name,
                    game=analysis.game,
                    offer_title=_short(offer),
                    volume=_volume_label(offer.tokens_millions),
                    current_price=offer.price,
                    current_unit=offer.price_per_million,
                    target_unit=target_unit,
                    target_price=round(target_price, 2),
                    delta_pct=pos.deviation_pct,
                    impact_rub=round(abs(delta), 2),
                    comparables=pos.comparable_count,
                    reason=reason,
                )
            )

    actions.sort(key=lambda a: -a.impact_rub)
    return actions


@dataclass
class Recommendation:
    rule: str
    priority: str
    category: str
    title: str
    action: str
    evidence: str
    # Идентификатор категории обязателен: у разных игр подкатегории называются
    # одинаково (например, «Подписка» есть у ChatGPT, Claude, Gemini и других),
    # поэтому группировать рекомендации по имени нельзя.
    category_id: str = ""
    category_game: str = ""

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "priority": self.priority,
            "category": self.category,
            "category_id": self.category_id,
            "category_game": self.category_game,
            "title": self.title,
            "action": self.action,
            "evidence": self.evidence,
        }


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 100:
        return f"{value:,.0f} ₽".replace(",", " ")
    return f"{value:.2f} ₽"


def _unit_money(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1:
        return f"{value:.2f} ₽/1M"
    return f"{value:.4f} ₽/1M"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())


def _contains_any(text: str, needles: tuple[str, ...]) -> list[str]:
    low = _normalize(text)
    return [n for n in needles if n in low]


# --- правила -------------------------------------------------------------


_AUTO_MENTION = re.compile(r"\bавто", re.IGNORECASE)


def rule_auto_promise_mismatch(analysis: CategoryAnalysis) -> list[Recommendation]:
    """R11: в описании обещана автовыдача, но флаг автовыдачи не включён.

    Самый дорогой тип расхождения: продавец пишет «авто», покупатель ищет
    лоты с автовыдачей и не находит этот лот в фильтре вообще.
    """
    if not analysis.target_offers:
        return []

    mismatched = [
        o
        for o in analysis.target_offers
        if not o.auto_delivery and _AUTO_MENTION.search(o.description)
    ]
    if not mismatched:
        return []

    return [
        Recommendation(
            rule="R11",
            priority=P0,
            category=analysis.name,
            title=f"«Авто» написано в описании, но автовыдача не включена: {len(mismatched)} лотов",
            action=(
                "Включить флаг автовыдачи в настройках этих лотов. Пока он выключен, "
                "лоты не попадают в фильтр «с автовыдачей», хотя описание обещает моментальную выдачу."
            ),
            evidence=(
                "в описаниях есть слово «авто», но у лотов не выставлен признак автовыдачи: "
                + "; ".join(_short(o) for o in mismatched[:3])
            ),
        )
    ]


def rule_price_position(
    analysis: CategoryAnalysis, threshold_pct: float
) -> list[Recommendation]:
    """R1/R2: цена лота и цена за 1M токенов против сопоставимых конкурентов."""
    out: list[Recommendation] = []
    for pos in analysis.positions:
        offer = pos.offer
        if pos.comparable_count == 0:
            continue

        # Если аналогов ровно вашего объёма на рынке нет, сравнивать юнит-цены
        # напрямую нельзя: пак на 25B и пак на 25M — разные продукты.
        if pos.scope != "volume":
            out.append(
                Recommendation(
                    rule="R2",
                    priority=P2,
                    category=analysis.name,
                    title=f"Нет прямых аналогов по объёму: {_short(offer)}",
                    action=(
                        "Сравнивать цену не с чем. Это либо ваше преимущество (никто не "
                        "предлагает такой объём), либо спрос на такой объём не сформирован — "
                        "проверить по заказам."
                    ),
                    evidence=(
                        f"{pos.volume_range}; сравнение по модели {pos.family} даёт "
                        f"{pos.comparable_count} лотов и {_unit_money(pos.comparable_median_unit_price)}, "
                        f"но объёмы у них другие"
                    ),
                )
            )
            continue

        # R2 — юнит-экономика, основная метрика ниши.
        if pos.deviation_pct is not None:
            median_unit = pos.comparable_median_unit_price
            scope_note = f"аналоги по объёму: {pos.volume_range}"
            # На выборке меньше пяти аналогов вывод шаткий — понижаем приоритет.
            small_sample = pos.comparable_count < 5
            sample_note = "; выборка маленькая, стоит перепроверить" if small_sample else ""
            if pos.deviation_pct > threshold_pct:
                out.append(
                    Recommendation(
                        rule="R2",
                        priority=P1 if small_sample else P0,
                        category=analysis.name,
                        title=f"Дороже аналогов на {pos.deviation_pct:.0f}%: {_short(offer)}",
                        action=(
                            f"Снизить цену до {_unit_money(median_unit)} за 1M токенов, "
                            f"то есть примерно до {_money(_target_price(offer, median_unit))} "
                            f"за текущий объём."
                        ),
                        evidence=(
                            f"лот {_unit_money(offer.price_per_million)} против медианы "
                            f"{_unit_money(median_unit)} у {pos.comparable_count} похожих лотов; "
                            f"{scope_note}{sample_note}"
                        ),
                    )
                )
            elif pos.deviation_pct < -threshold_pct:
                out.append(
                    Recommendation(
                        rule="R2",
                        priority=P1,
                        category=analysis.name,
                        title=f"Дешевле аналогов на {abs(pos.deviation_pct):.0f}%: {_short(offer)}",
                        action=(
                            f"Есть запас поднять цену до {_unit_money(median_unit)} за 1M токенов "
                            f"(≈{_money(_target_price(offer, median_unit))} за объём) без потери позиции."
                        ),
                        evidence=(
                            f"лот {_unit_money(offer.price_per_million)} против медианы "
                            f"{_unit_money(median_unit)} у {pos.comparable_count} похожих лотов; "
                            f"{scope_note}{sample_note}"
                        ),
                    )
                )

        # R1 — позиция по абсолютной цене лота.
        if pos.price_percentile is not None and offer.price is not None:
            stats = analysis.price_stats
            if pos.price_percentile <= 5 and stats.median and offer.price < stats.median * 0.3:
                out.append(
                    Recommendation(
                        rule="R1",
                        priority=P2,
                        category=analysis.name,
                        title=f"Лот в самом низу рынка: {_short(offer)}",
                        action=(
                            "Проверить, окупается ли лот. Если это приманка — оставить, "
                            "иначе поднять цену или убрать."
                        ),
                        evidence=(
                            f"цена {_money(offer.price)} — дешевле {100 - pos.price_percentile:.0f}% "
                            f"рынка, медиана категории {_money(stats.median)}"
                        ),
                    )
                )
            elif pos.price_percentile >= 95 and stats.median and offer.price > stats.median * 3:
                out.append(
                    Recommendation(
                        rule="R1",
                        priority=P1,
                        category=analysis.name,
                        title=f"Лот в самом верху рынка: {_short(offer)}",
                        action="Убедиться, что описание объясняет, за что такая цена.",
                        evidence=(
                            f"цена {_money(offer.price)} — дороже {pos.price_percentile:.0f}% рынка, "
                            f"медиана категории {_money(stats.median)}"
                        ),
                    )
                )
    return out


def _target_price(offer: Offer, unit_price: float | None) -> float | None:
    if unit_price is None or offer.tokens_millions is None:
        return None
    return unit_price * offer.tokens_millions


def _short(offer: Offer) -> str:
    text = offer.description.strip()
    return (text[:60] + "…") if len(text) > 60 else text


def rule_volume_ladder(analysis: CategoryAnalysis) -> list[Recommendation]:
    """R3: лестница скидок за объём."""
    out: list[Recommendation] = []
    packs = [o for o in analysis.target_offers if o.tokens_millions and o.price_per_million]
    if len(packs) < 3:
        return out

    packs.sort(key=lambda o: o.tokens_millions or 0)
    breaks: list[str] = []
    for prev, cur in zip(packs, packs[1:], strict=False):
        if prev.price_per_million and cur.price_per_million:
            if cur.price_per_million > prev.price_per_million * 1.02:
                breaks.append(
                    f"{_vol(prev)} → {_vol(cur)}: {_unit_money(prev.price_per_million)} → "
                    f"{_unit_money(cur.price_per_million)}"
                )

    if breaks:
        out.append(
            Recommendation(
                rule="R3",
                priority=P1,
                category=analysis.name,
                title="Скидка за объём работает непоследовательно",
                action=(
                    "Выровнять лестницу: цена за 1M токенов должна только падать "
                    "при росте объёма пака."
                ),
                evidence="; ".join(breaks),
            )
        )

    # Разрывы: между паками объём отличается в 5+ раз — там теряется покупатель.
    gaps: list[str] = []
    for prev, cur in zip(packs, packs[1:], strict=False):
        if prev.tokens_millions and cur.tokens_millions:
            ratio = cur.tokens_millions / prev.tokens_millions
            if ratio >= 5:
                gaps.append(f"{_vol(prev)} → {_vol(cur)} (в {ratio:.0f} раз)")
    if gaps:
        out.append(
            Recommendation(
                rule="R3",
                priority=P2,
                category=analysis.name,
                title="Большие разрывы между паками",
                action=(
                    "Добавить промежуточный пак, чтобы покупатель не прыгал "
                    "сразу через порядок объёма."
                ),
                evidence="; ".join(gaps),
            )
        )
    return out


def _vol(offer: Offer) -> str:
    m = offer.tokens_millions or 0
    if m >= 1000:
        return f"{m / 1000:g}B"
    return f"{m:g}M"


def rule_sales_arguments(analysis: CategoryAnalysis, extra_phrases: list[str]) -> list[Recommendation]:
    """R4: смыслы в описаниях, которые используют конкуренты сверху."""
    out: list[Recommendation] = []
    if not analysis.target_offers or not analysis.competitors:
        return out

    target_text = " ".join(o.description for o in analysis.target_offers)
    top_offers = [o for c in analysis.competitors[:5] for o in c.offers]
    if not top_offers:
        return out
    top_text = " ".join(o.description for o in top_offers)

    vocabulary = tuple(SALES_ARGUMENTS) + tuple(p.lower() for p in extra_phrases)
    missing = [
        arg
        for arg in vocabulary
        if arg in _normalize(top_text) and arg not in _normalize(target_text)
    ]
    if not missing:
        return out

    # Отдельно отмечаем автовыдачу: это самый сильный аргумент в нише.
    priority = P0 if "авто" in missing else P1
    out.append(
        Recommendation(
            rule="R4",
            priority=priority,
            category=analysis.name,
            title="В описаниях нет аргументов, которые используют лидеры",
            action=(
                "Добавить в описания лотов: "
                + ", ".join(missing)
                + ". Формулировать как факт, например «автовыдача 24/7», «гарантия и замена ключа»."
            ),
            evidence=(
                f"эти слова встречаются у {len(top_offers)} лотов конкурентов сверху, "
                f"но отсутствуют во всех ваших описаниях в категории"
            ),
        )
    )
    return out


def rule_auto_delivery(analysis: CategoryAnalysis) -> list[Recommendation]:
    """R5: автовыдача — критичный фактор конверсии."""
    out: list[Recommendation] = []
    if not analysis.target_offers:
        return out
    target_auto = [o for o in analysis.target_offers if o.auto_delivery]
    if len(target_auto) == len(analysis.target_offers):
        return out

    top = [o for c in analysis.competitors[:5] for o in c.offers]
    if not top:
        return out
    top_auto_share = sum(1 for o in top if o.auto_delivery) / len(top)
    if top_auto_share < 0.5:
        return out

    without = [o for o in analysis.target_offers if not o.auto_delivery]
    out.append(
        Recommendation(
            rule="R5",
            priority=P0,
            category=analysis.name,
            title=f"Нет автовыдачи у {len(without)} из {len(analysis.target_offers)} лотов",
            action=(
                "Включить автовыдачу на всех лотах категории: покупатель уходит "
                "к тому, кто выдаёт сразу, без ожидания ответа продавца."
            ),
            evidence=(
                f"у конкурентов сверху автовыдача у {top_auto_share * 100:.0f}% лотов, "
                f"у вас — у {len(target_auto) / len(analysis.target_offers) * 100:.0f}%"
            ),
        )
    )
    return out


def rule_model_coverage(analysis: CategoryAnalysis) -> list[Recommendation]:
    """R7: модели, которые есть у конкурентов, но отсутствуют у вас."""
    out: list[Recommendation] = []
    if not analysis.target_offers:
        return out

    # Считаем модели только среди лотов конкурентов сверху — там реальный спрос.
    top_offers = [o for c in analysis.competitors[:5] for o in c.offers]
    top_models: dict[str, int] = {}
    for offer in top_offers:
        for model in offer.detected_models:
            top_models[model] = top_models.get(model, 0) + 1

    target_models: set[str] = set()
    for offer in analysis.target_offers:
        target_models.update(offer.detected_models)

    missing = sorted(
        ((m, n) for m, n in top_models.items() if m not in target_models),
        key=lambda kv: -kv[1],
    )[:5]
    if not missing:
        return out

    out.append(
        Recommendation(
            rule="R7",
            priority=P1,
            category=analysis.name,
            title="Не покрыты модели, которые продают лидеры",
            action=(
                "Добавить лоты по этим моделям: "
                + ", ".join(f"{m} ({n} лотов)" for m, n in missing)
            ),
            evidence=f"модели встречаются у конкурентов сверху, у вас их нет ни в одном лоте",
        )
    )
    return out


def rule_competition_zones(analysis: CategoryAnalysis) -> list[Recommendation]:
    """R8: свободные ценовые зоны с повышенным спросом."""
    out: list[Recommendation] = []
    for bucket in analysis.opportunity_buckets[:2]:
        out.append(
            Recommendation(
                rule="R8",
                priority=P2,
                category=analysis.name,
                title=f"Свободная зона {bucket.label} за 1M токенов",
                action=(
                    f"Проверить лот с ценой в диапазоне {bucket.label} за 1M токенов: "
                    f"предложения мало, а спрос выше среднего."
                ),
                evidence=(
                    f"в зоне {bucket.count} лотов при среднем "
                    f"{bucket.reviews_per_offer:.1f} отзывов на лот у их продавцов "
                    f"против {_avg_rpo(analysis):.1f} в среднем по категории"
                ),
            )
        )
    return out


def _avg_rpo(analysis: CategoryAnalysis) -> float:
    filled = [b for b in analysis.unit_price_histogram if b.count > 0]
    if not filled:
        return 0.0
    return sum(b.reviews_per_offer for b in filled) / len(filled)


def rule_promotion(analysis: CategoryAnalysis) -> list[Recommendation]:
    """R6: промо-размещение."""
    out: list[Recommendation] = []
    if analysis.promo_share <= 0 or not analysis.target_offers:
        return out
    target_promo = sum(1 for o in analysis.target_offers if o.promoted)
    if target_promo > 0:
        return out
    top_promoted = sum(1 for c in analysis.competitors[:5] for o in c.offers if o.promoted)
    if top_promoted == 0:
        return out
    out.append(
        Recommendation(
            rule="R6",
            priority=P2,
            category=analysis.name,
            title="Конкуренты сверху используют промо, вы — нет",
            action="Протестировать промо на 1–2 самых маржинальных лотах и сравнить поток заказов.",
            evidence=(
                f"промо у {analysis.promo_share * 100:.1f}% лотов категории, "
                f"в том числе {top_promoted} у конкурентов сверху"
            ),
        )
    )
    return out


def rule_missing_category(analysis: CategoryAnalysis) -> list[Recommendation]:
    """R12: продавец не представлен в категории, где рынок живой."""
    if analysis.target_offers or not analysis.offers:
        return []
    if analysis.unit_price_stats.median is None and analysis.price_stats.median is None:
        return []
    reviews_total = sum(o.seller_reviews for o in analysis.offers)
    if reviews_total < 100:
        return []
    return [
        Recommendation(
            rule="R12",
            priority=P1,
            category=analysis.name,
            title="Вас нет в категории, где идёт торговля",
            action=(
                "Проверить запуск лотов здесь: "
                f"медиана цены {_money(analysis.price_stats.median)}, "
                f"цена за 1M токенов около {_unit_money(analysis.unit_price_stats.median)}."
            ),
            evidence=(
                f"{len(analysis.offers)} лотов от {analysis.seller_count} продавцов, "
                f"суммарно {reviews_total} отзывов"
            ),
        )
    ]


def rule_anchors(analysis: CategoryAnalysis) -> list[Recommendation]:
    """R9: сверхдешёвые лоты и их влияние на восприятие бренда."""
    cheap = [o for o in analysis.target_offers if o.price is not None and o.price < 20]
    if not cheap:
        return []
    return [
        Recommendation(
            rule="R9",
            priority=P2,
            category=analysis.name,
            title=f"Сверхдешёвые лоты-якоря: {len(cheap)} шт.",
            action=(
                "Оставить один пробник для входа, а остальные дешёвые лоты "
                "объединить или поднять: обилие цен в 5–15 ₽ снижает средний чек."
            ),
            evidence="цены: " + ", ".join(_money(o.price) for o in cheap),
        )
    ]


def rule_descriptions(analysis: CategoryAnalysis) -> list[Recommendation]:
    """R10: качество и длина описаний."""
    if not analysis.target_offers or not analysis.competitors:
        return []
    top_offers = [o for c in analysis.competitors[:5] for o in c.offers]
    if not top_offers:
        return []
    top_len = sum(len(o.description) for o in top_offers) / len(top_offers)
    target_len = sum(len(o.description) for o in analysis.target_offers) / len(analysis.target_offers)
    if target_len >= top_len * 0.8:
        return []
    return [
        Recommendation(
            rule="R10",
            priority=P1,
            category=analysis.name,
            title="Описания короче, чем у конкурентов сверху",
            action=(
                "Удлинить описания: добавить объём в токенах, способ получения, "
                "срок работы ключа, что делать при проблеме."
            ),
            evidence=(
                f"у вас в среднем {target_len:.0f} символов против {top_len:.0f} "
                f"у конкурентов сверху"
            ),
        )
    ]


# --- сборка --------------------------------------------------------------


def _tag(recs: list[Recommendation], analysis: CategoryAnalysis) -> list[Recommendation]:
    """Проставляет рекомендациям id и игру категории.

    Делается централизованно, чтобы ни одно правило не забыло указать id,
    и рекомендации не «перепутались» между одноимёнными категориями.
    """
    for rec in recs:
        rec.category_id = analysis.category_id
        rec.category_game = analysis.game
        rec.category = analysis.name
    return recs


def build_recommendations(report: MarketReport, threshold_pct: float, extra_phrases: list[str]) -> list[Recommendation]:
    """Прогоняет все правила и сортирует рекомендации по приоритету."""
    out: list[Recommendation] = []
    for analysis in report.categories:
        out.extend(_tag(rule_auto_promise_mismatch(analysis), analysis))
        out.extend(_tag(rule_price_position(analysis, threshold_pct), analysis))
        out.extend(_tag(rule_volume_ladder(analysis), analysis))
        out.extend(_tag(rule_auto_delivery(analysis), analysis))
        out.extend(_tag(rule_sales_arguments(analysis, extra_phrases), analysis))
        out.extend(_tag(rule_model_coverage(analysis), analysis))
        out.extend(_tag(rule_missing_category(analysis), analysis))
        out.extend(_tag(rule_descriptions(analysis), analysis))
        out.extend(_tag(rule_promotion(analysis), analysis))
        out.extend(_tag(rule_competition_zones(analysis), analysis))
        out.extend(_tag(rule_anchors(analysis), analysis))

    order = {P0: 0, P1: 1, P2: 2}
    out.sort(key=lambda r: (order.get(r.priority, 9), r.category, r.rule))
    return out


def summarize(recommendations: list[Recommendation]) -> dict[str, int]:
    counts = {P0: 0, P1: 0, P2: 0}
    for rec in recommendations:
        counts[rec.priority] = counts.get(rec.priority, 0) + 1
    return counts
