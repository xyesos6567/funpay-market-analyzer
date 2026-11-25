"""Автопоиск похожих категорий через каталог FunPay.

Разведка показала: главная страница отдаёт весь каталог (≈898 игр) с подкатегориями
одним запросом. Это надёжнее обхода каталога постранично, поэтому автопоиск
строится именно на ней.

Логика отбора:
  1. находим игры, чьё название соответствует ИИ-сервисам (ChatGPT, Claude, GLM и т.д.);
  2. внутри таких игр берём только «сервисные» подкатегории (токены, API, подписка,
     услуги) — аккаунты не берём, так как анализируемый продавец торгует доступами,
     а не аккаунтами.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Category, Config
from .http import Fetcher
from .parse import parse_catalog

log = logging.getLogger(__name__)

# Игры, чьё название содержит одно из этих слов, считаем ИИ-сервисами.
# Список намеренно узкий: подстрока «ai» даёт ложные срабатывания
# (например, «AI Limit», «Honkai Impact 3rd»), поэтому её здесь нет.
AI_GAME_KEYWORDS: tuple[str, ...] = (
    "deepseek",
    "chatgpt",
    "openai",
    "claude",
    "anthropic",
    "gemini",
    "grok",
    "kimi",
    "glm",
    "minimax",
    "qwen",
    "midjourney",
    "perplexity",
    "copilot",
    "cursor ai",
    "нейросет",
)

# Подкатегории, релевантные продавцу токенов и API-доступов.
SERVICE_SUBCATEGORY_HINTS: tuple[str, ...] = (
    "токен",
    "api",
    "подписка",
    "услуг",
    "пополнение",
    "промокод",
    "промо",
)

# Явные исключения: названия, где ключевое слово совпадает случайно.
EXCLUDED_GAME_NAMES: tuple[str, ...] = (
    "stella sora",
    "ai limit",
    "honkai impact",
)


# Насколько подкатегория близка к товару анализируемого продавца (токены и доступы).
# Чем меньше число, тем выше приоритет при ограничении числа категорий.
_SUBCATEGORY_PRIORITY: tuple[tuple[str, int], ...] = (
    ("токен", 0),
    ("api", 1),
    ("пополнение", 2),
    ("подписка", 3),
    ("промокод", 4),
    ("промо", 5),
    ("услуг", 6),
)


def _subcategory_priority(name: str) -> int:
    low = name.lower()
    for hint, weight in _SUBCATEGORY_PRIORITY:
        if hint in low:
            return weight
    return 99


@dataclass
class DiscoveredCategory:
    id: str
    name: str
    game: str
    source: str  # 'seed' | 'auto' | 'manual'


@dataclass
class DiscoveryResult:
    categories: list[DiscoveredCategory] = field(default_factory=list)
    ai_games: list[str] = field(default_factory=list)
    total_games_scanned: int = 0

    def to_dict(self) -> dict:
        return {
            "total_games_scanned": self.total_games_scanned,
            "ai_games": self.ai_games,
            "categories": [asdict(c) for c in self.categories],
        }


def _is_ai_game(name: str, extra_keywords: tuple[str, ...] = (), excluded: tuple[str, ...] = ()) -> bool:
    low = name.lower()
    if any(excl.lower() in low for excl in excluded):
        return False
    return any(kw in low for kw in AI_GAME_KEYWORDS) or any(
        kw.lower() in low for kw in extra_keywords
    )


def _is_service_subcategory(name: str, hints: tuple[str, ...] = ()) -> bool:
    low = name.lower()
    return any(hint in low for hint in (*SERVICE_SUBCATEGORY_HINTS, *hints))


def discover_categories(config: Config, fetcher: Fetcher) -> DiscoveryResult:
    """Собирает список категорий: сиды + автопоиск + ручные дополнения."""
    result = DiscoveryResult()

    # Сиды всегда на первом месте — их анализируем в любом случае.
    seen: dict[str, DiscoveredCategory] = {}
    for cat in config.seed_categories:
        seen[cat.id] = DiscoveredCategory(id=cat.id, name=cat.name, game="", source="seed")

    if config.discovery_enabled:
        try:
            html = fetcher.get(f"{config.base_url}/").text
            games = parse_catalog(html)
            result.total_games_scanned = len(games)

            extra_kw = tuple(config.discovery_keywords)
            excluded = tuple(config.discovery_exclude_games) or EXCLUDED_GAME_NAMES
            sub_hints = tuple(config.discovery_subcategory_hints)

            # Сначала собираем всех кандидатов, и только потом применяем лимит:
            # иначе лимит съедают первые по порядку каталога игры, а самые
            # близкие к товару категории (токены, API) до анализа не доживают.
            candidates: list[tuple[int, str, str, str]] = []
            for game in games:
                if not _is_ai_game(game.name, extra_kw, excluded):
                    continue
                result.ai_games.append(game.name)
                for cat_id, cat_name in game.subcategories:
                    if not _is_service_subcategory(cat_name, sub_hints):
                        continue
                    if cat_id in seen:
                        if not seen[cat_id].game:
                            seen[cat_id].game = game.name
                        continue
                    candidates.append(
                        (_subcategory_priority(cat_name), game.name, cat_id, cat_name)
                    )

            candidates.sort(key=lambda item: (item[0], item[1], item[3]))
            auto_added = 0
            for _priority, game_name, cat_id, cat_name in candidates:
                if auto_added >= config.max_extra_categories:
                    break
                seen[cat_id] = DiscoveredCategory(
                    id=cat_id, name=cat_name, game=game_name, source="auto"
                )
                auto_added += 1

            log.info(
                "Автопоиск: игр в каталоге %d, ИИ-сервисов %d, добавлено категорий %d",
                result.total_games_scanned,
                len(result.ai_games),
                auto_added,
            )
        except Exception as exc:  # автопоиск не должен ломать основной сценарий
            log.warning("Автопоиск категорий не удался: %s", exc)

    # Ручные дополнения из конфига.
    for cat in config.all_categories():
        if cat.id not in seen:
            seen[cat.id] = DiscoveredCategory(id=cat.id, name=cat.name, game="", source="manual")

    result.categories = list(seen.values())
    return result


def save_discovery(result: DiscoveryResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def categories_from_result(result: DiscoveryResult) -> list[Category]:
    return [Category(id=c.id, name=c.name) for c in result.categories]
