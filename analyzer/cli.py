"""Командный интерфейс анализатора.

Примеры:
    python -m analyzer run                 # полный цикл: собрать, проанализировать, отчёт
    python -m analyzer discover            # только автопоиск категорий
    python -m analyzer report              # пересобрать отчёт из последнего data.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .advice import build_price_actions, build_recommendations, summarize
from .analytics import analyze_market
from .config import Config, apply_env_overrides, load_config
from .discover import categories_from_result, discover_categories, save_discovery
from .http import FetchError, Fetcher
from .normalize import enrich_offers
from .parse import CategoryPage, ParseError, parse_category_page, parse_seller_offers
from .report import build_context, render_report, write_json, write_report

log = logging.getLogger("analyzer")


def _configure_console_encoding() -> None:
    """Приводит вывод к кодировке консоли Windows.

    Python печатает в UTF-8, а консоль Windows работает в OEM-кодировке
    (cp866 для русского языка), поэтому русские сообщения в логе превращаются
    в кракозябры. Перенастраиваем поток только когда он действительно идёт
    в консоль: при перенаправлении в файл UTF-8 остаётся правильным вариантом.
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        if not getattr(stream, "isatty", lambda: False)():
            continue
        try:
            import ctypes

            codepage = ctypes.windll.kernel32.GetConsoleOutputCP()
        except (ImportError, AttributeError, OSError):
            return
        if not codepage:
            return
        try:
            stream.reconfigure(encoding=f"cp{codepage}", errors="replace")
        except (AttributeError, ValueError, OSError):
            # Поток может не поддерживать reconfigure — это не повод падать.
            pass


def _setup_logging(verbose: bool) -> None:
    _configure_console_encoding()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def _make_fetcher(config: Config) -> Fetcher:
    return Fetcher(
        base_url=config.base_url,
        user_agent=config.user_agent,
        delay_seconds=config.delay_seconds,
        timeout_seconds=config.timeout_seconds,
        retries=config.retries,
        cache_dir=config.cache_dir,
        cache_ttl_minutes=config.cache_ttl_minutes,
    )


@dataclass
class CollectStats:
    collected: int = 0
    failed: list[str] = field(default_factory=list)


def collect_pages(config: Config, fetcher: Fetcher, categories) -> tuple[list[CategoryPage], CollectStats]:
    """Скачивает и разбирает страницы категорий, обогащая лоты юнит-экономикой."""
    pages: list[CategoryPage] = []
    stats = CollectStats()

    for cat in categories:
        url = fetcher.category_url(cat.id)
        try:
            html = fetcher.get(url).text
            page = parse_category_page(html, cat.id, cat.name)
        except (FetchError, ParseError) as exc:
            log.error("Категория %s (%s) пропущена: %s", cat.id, cat.name, exc)
            stats.failed.append(f"{cat.id} {cat.name}: {exc}")
            continue

        enrich_offers(page.offers)
        pages.append(page)
        stats.collected += 1

        target = [o for o in page.offers if o.seller_id == config.seller_id]
        log.info(
            "  %-28s лотов %5d, продавцов %4d, ваших лотов %d",
            (page.category_name or cat.id)[:28],
            len(page.offers),
            len({o.seller_id for o in page.offers if o.seller_id}),
            len(target),
        )

    return pages, stats


def fetch_seller_overview(config: Config, fetcher: Fetcher) -> dict:
    """Сводка по профилю продавца: сколько у него всего лотов и по каким категориям."""
    try:
        html = fetcher.get(config.seller_url).text
        offers = parse_seller_offers(html, config.seller_id)
    except (FetchError, ParseError) as exc:
        log.warning("Не удалось получить профиль продавца: %s", exc)
        return {}

    enrich_offers(offers)
    by_category: dict[str, int] = {}
    for offer in offers:
        key = offer.category_name or offer.category_id
        by_category[key] = by_category.get(key, 0) + 1

    return {
        "total_offers": len(offers),
        "by_category": by_category,
        "categories": sorted({o.category_id for o in offers if o.category_id}),
    }


def cmd_discover(config: Config, fetcher: Fetcher) -> int:
    result = discover_categories(config, fetcher)
    path = config.root / "data" / "discovered_categories.json"
    save_discovery(result, path)

    log.info("Категорий в анализе: %d", len(result.categories))
    for cat in result.categories:
        origin = {"seed": "сид", "auto": "авто", "manual": "вручную"}[cat.source]
        log.info("  %-6s %-28s %s", cat.id, cat.name, origin)
    log.info("Сохранено: %s", path)
    return 0


def cmd_run(config: Config, fetcher: Fetcher) -> int:
    log.info("=== Автопоиск категорий ===")
    discovery = discover_categories(config, fetcher)
    save_discovery(discovery, config.root / "data" / "discovered_categories.json")

    discovery_summary = {
        "total_games_scanned": discovery.total_games_scanned,
        "ai_games": discovery.ai_games,
        "categories": [c.id for c in discovery.categories],
    }

    categories = categories_from_result(discovery)
    log.info("=== Сбор данных: %d категорий ===", len(categories))
    pages, stats = collect_pages(config, fetcher, categories)

    if not pages:
        log.error("Не удалось собрать ни одной категории — отчёт не имеет смысла.")
        return 2

    log.info("=== Профиль продавца %s ===", config.seller_name)
    overview = fetch_seller_overview(config, fetcher)
    if overview:
        log.info("Всего лотов в профиле: %d", overview["total_offers"])

    log.info("=== Аналитика ===")
    market = analyze_market(
        pages,
        target_seller_id=config.seller_id,
        target_seller_name=config.seller_name,
        brand=config.brand,
        top_n=config.top_competitors,
    )
    log.info("Лотов обработано: %d, продавцов: %d", market.total_offers, market.total_sellers)

    recommendations = build_recommendations(
        market,
        threshold_pct=config.price_deviation_threshold_pct,
        extra_phrases=config.desired_phrases,
    )
    counts = summarize(recommendations)
    log.info(
        "Рекомендаций: %d (P0: %d, P1: %d, P2: %d)",
        len(recommendations), counts["P0"], counts["P1"], counts["P2"],
    )

    price_actions = build_price_actions(market, config.price_deviation_threshold_pct)
    if price_actions:
        up = sum(1 for a in price_actions if a.direction == "up")
        down = len(price_actions) - up
        log.info("Ценовых действий: %d (поднять: %d, опустить: %d)", len(price_actions), up, down)

    context = build_context(
        market, recommendations, config, discovery_summary, price_actions=price_actions
    )
    context["seller_overview"] = overview
    context["collection"] = {"collected": stats.collected, "failed": stats.failed}

    html = render_report(context, config)
    report_path = write_report(html, config)
    json_path = write_json(context, config)

    log.info("=== Готово ===")
    log.info("Отчёт:  %s", report_path)
    log.info("Данные: %s", json_path)
    if stats.failed:
        log.warning("Пропущено категорий: %d", len(stats.failed))
        for line in stats.failed:
            log.warning("  %s", line)
    return 0


def cmd_report(config: Config, fetcher: Fetcher) -> int:
    """Пересобирает HTML из сохранённого data.json — без обращения к сети."""
    json_path = config.output_dir / "data.json"
    if not json_path.exists():
        log.error("Не найден %s. Сначала выполните: python -m analyzer run", json_path)
        return 2
    context = json.loads(json_path.read_text(encoding="utf-8"))
    html = render_report(context, config)
    path = write_report(html, config)
    log.info("Отчёт пересобран: %s", path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="analyzer",
        description="Анализатор рынка FunPay для продавцов ИИ-доступов",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "discover", "report"],
        help="run — полный цикл, discover — только автопоиск, report — пересобрать отчёт",
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="путь к config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    parser.add_argument(
        "--max-categories",
        type=int,
        default=None,
        help="сколько категорий добавляет автопоиск (переопределяет config.yaml)",
    )
    parser.add_argument(
        "--seed-only",
        action="store_true",
        help="только категории из seed_categories, без автопоиска",
    )
    parser.add_argument(
        "--no-discovery",
        action="store_true",
        help="не скачивать каталог, использовать только seed и ручные категории",
    )
    parser.add_argument(
        "--repo-url",
        default=None,
        help="адрес репозитория GitHub для кнопки «Обновить данные»",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="куда писать отчёт (по умолчанию out)",
    )
    parser.add_argument(
        "--cache-ttl",
        type=int,
        default=None,
        help="время жизни кэша в минутах; 0 — всегда свежие данные",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    try:
        config = load_config(args.config)
    except Exception as exc:
        log.error("Ошибка конфигурации: %s", exc)
        return 2

    # Аргументы командной строки перекрывают файл, а переменные окружения — файл.
    if args.max_categories is not None:
        config.max_extra_categories = args.max_categories
    if args.seed_only:
        config.discovery_enabled = False
        config.max_extra_categories = 0
    if args.no_discovery:
        config.discovery_enabled = False
    if args.repo_url is not None:
        config.repo_url = args.repo_url
    if args.output_dir:
        config.output_dir = Path(args.output_dir)
    if args.cache_ttl is not None:
        config.cache_ttl_minutes = args.cache_ttl

    config = apply_env_overrides(config)

    fetcher = _make_fetcher(config)
    handlers = {"run": cmd_run, "discover": cmd_discover, "report": cmd_report}
    return handlers[args.command](config, fetcher)


if __name__ == "__main__":
    raise SystemExit(main())
