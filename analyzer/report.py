"""Генерация HTML-отчёта."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .advice import Recommendation, summarize
from .analytics import MarketReport
from .config import Config

log = logging.getLogger(__name__)


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 100:
        return f"{value:,.0f} ₽".replace(",", " ")
    if value >= 1:
        return f"{value:.2f} ₽"
    return f"{value:.4f} ₽"


def _fmt_unit(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1:
        return f"{value:.2f}"
    if value >= 0.01:
        return f"{value:.4f}"
    return f"{value:.5f}"


def _fmt_int(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", " ")


def build_context(
    report: MarketReport,
    recommendations: list[Recommendation],
    config: Config,
    discovery_summary: dict | None = None,
    price_actions: list | None = None,
) -> dict:
    counts = summarize(recommendations)
    # Группируем строго по id категории: имена совпадают у разных игр
    # («Подписка» есть у ChatGPT, Claude, Gemini и других), и по имени
    # рекомендации приписывались бы не тем категориям.
    by_category: dict[str, list[Recommendation]] = {}
    for rec in recommendations:
        by_category.setdefault(rec.category_id, []).append(rec)

    categories = []
    for analysis in report.categories:
        categories.append(
            {
                "id": analysis.category_id,
                "name": analysis.name,
                "game": analysis.game,
                "offers": len(analysis.offers),
                "sellers": analysis.seller_count,
                "offers_with_volume": analysis.offers_with_volume,
                "auto_share": analysis.auto_share * 100,
                "promo_share": analysis.promo_share * 100,
                "online_share": analysis.online_share * 100,
                "price_stats": analysis.price_stats.to_dict(),
                "unit_price_stats": analysis.unit_price_stats.to_dict(),
                "unit_histogram": [
                    {
                        "label": b.label,
                        "count": b.count,
                        "height": 0,
                        "is_opportunity": b in analysis.opportunity_buckets,
                    }
                    for b in analysis.unit_price_histogram
                    if b.count > 0
                ],
                "model_counts": sorted(
                    analysis.model_counts.items(), key=lambda kv: -kv[1]
                )[:12],
                "positions": [
                    {
                        "title": (p.offer.description[:70] + "…")
                        if len(p.offer.description) > 70
                        else p.offer.description,
                        "price": _fmt_money(p.offer.price),
                        "unit_price": _fmt_unit(p.offer.price_per_million),
                        "family": p.family or "не определена",
                        "price_percentile": p.price_percentile,
                        "unit_percentile": p.unit_price_percentile,
                        "comparables": p.comparable_count,
                        "comparable_median_unit": _fmt_unit(p.comparable_median_unit_price),
                        "deviation": p.deviation_pct,
                        "volume": _fmt_volume(p.offer.tokens_millions),
                        "auto": p.offer.auto_delivery,
                        "reviews": p.offer.seller_reviews,
                    }
                    for p in analysis.positions
                ],
                "competitors": [
                    {
                        "name": c.name,
                        "seller_id": c.seller_id,
                        "score": c.score,
                        "reviews": c.max_reviews,
                        "rating": c.rating,
                        "online": c.online,
                        "auto_share": c.auto_share * 100,
                        "offers": c.offer_count,
                        "price_min": _fmt_money(c.price_min),
                        "price_max": _fmt_money(c.price_max),
                        "families": ", ".join(c.families[:4]),
                    }
                    for c in analysis.competitors
                ],
                "opportunities": [
                    {
                        "label": b.label,
                        "count": b.count,
                        "reviews_per_offer": round(b.reviews_per_offer, 1),
                    }
                    for b in analysis.opportunity_buckets
                ],
                "recommendations": [r.to_dict() for r in by_category.get(analysis.category_id, [])],
                "has_target": bool(analysis.target_offers),
            }
        )

    # Нормируем высоты столбиков гистограммы под 100%.
    for cat in categories:
        peak = max((b["count"] for b in cat["unit_histogram"]), default=0)
        for bucket in cat["unit_histogram"]:
            bucket["height"] = round(bucket["count"] / peak * 100, 1) if peak else 0

    return {
        "generated_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "brand": report.brand,
        "seller_name": report.target_seller_name,
        "seller_id": report.target_seller_id,
        "seller_url": f"{config.base_url}/users/{report.target_seller_id}/",
        "total_offers": _fmt_int(report.total_offers),
        "total_sellers": _fmt_int(report.total_sellers),
        "category_count": len(report.categories),
        "counts": counts,
        "total_recommendations": len(recommendations),
        "top_recommendations": [r.to_dict() for r in recommendations[:8]],
        "categories": sorted(categories, key=lambda c: (not c["recommendations"], c["name"])),
        "discovery": discovery_summary or {},
        "price_actions": _build_price_actions_context(price_actions or []),
        "repo_url": config.repo_url,
        "repo_slug": _repo_slug(config.repo_url),
        "refresh_url": _refresh_url(config),
    }


def _repo_slug(repo_url: str) -> str:
    """'https://github.com/user/repo' -> 'user/repo'.

    Нужен кнопке «Обновить сейчас»: она обращается к API GitHub напрямую.
    """
    if not repo_url:
        return ""
    parts = [p for p in repo_url.rstrip("/").split("/") if p]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return ""


def _build_price_actions_context(actions: list) -> dict:
    """Готовит ценовые действия к выводу: раздельно «поднять» и «опустить»."""
    up = [a for a in actions if a.direction == "up"]
    down = [a for a in actions if a.direction == "down"]

    def render(items: list) -> list[dict]:
        return [
            {
                "category": a.category_name,
                "game": a.game,
                "offer_title": a.offer_title,
                "volume": a.volume,
                "current_price": _fmt_money(a.current_price),
                "new_price": _fmt_money(a.target_price),
                "current_unit": f"{_fmt_unit(a.current_unit)} ₽/1M",
                "target_unit": f"{_fmt_unit(a.target_unit)} ₽/1M",
                "delta_pct": f"{a.delta_pct:+.0f}%",
                "impact_rub": _fmt_money(a.impact_rub),
                "comparables": a.comparables,
                "reason": a.reason,
            }
            for a in items
        ]

    return {
        "ru": render(up),
        "down": render(down),
        "up_count": len(up),
        "down_count": len(down),
        "up_total": _fmt_money(sum(a.impact_rub for a in up)),
        "down_total": _fmt_money(sum(a.impact_rub for a in down)),
        "total": len(actions),
    }


def _refresh_url(config: Config) -> str:
    """Ссылка на ручной запуск workflow в GitHub Actions.

    Кнопка на странице не может запускать процесс сама: Pages — статика.
    Поэтому она ведёт на форму запуска, где достаточно нажать Run workflow.
    Это безопаснее, чем держать токен GitHub в открытом HTML.
    """
    if not config.repo_url:
        return ""
    return f"{config.repo_url.rstrip('/')}/actions/workflows/analyze.yml"


def _fmt_volume(millions: float | None) -> str:
    if millions is None:
        return "—"
    if millions >= 1000:
        return f"{millions / 1000:g}B"
    return f"{millions:g}M"


def render_report(context: dict, config: Config) -> str:
    # Ссылку на обновление считаем в момент рендера, а не берём из data.json:
    # при пересборке отчёта (команда report) в сохранённом контексте она устарела.
    context = dict(context)
    context["repo_url"] = config.repo_url
    context["repo_slug"] = _repo_slug(config.repo_url)
    context["refresh_url"] = _refresh_url(config)

    templates_dir = config.root / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["money"] = _fmt_money
    env.filters["unit"] = _fmt_unit
    template = env.get_template("report.html.j2")
    return template.render(**context)


def write_report(html: str, config: Config, filename: str | None = None) -> Path:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    path = config.output_dir / (filename or config.report_filename)
    path.write_text(html, encoding="utf-8")
    return path


def write_json(context: dict, config: Config, name: str = "data.json") -> Path:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    path = config.output_dir / name
    path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
