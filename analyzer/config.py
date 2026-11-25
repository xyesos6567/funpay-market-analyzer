"""Загрузка и валидация конфигурации."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Конфигурация некорректна."""


@dataclass
class Category:
    id: str
    name: str = ""

    @property
    def url(self) -> str:
        return f"https://funpay.com/lots/{self.id}/"


@dataclass
class Config:
    seller_id: str
    seller_name: str
    brand: str
    seed_categories: list[Category]
    discovery_enabled: bool
    discovery_keywords: list[str]
    discovery_exclude_games: list[str]
    discovery_subcategory_hints: list[str]
    max_extra_categories: int
    manual_extra: list[str]
    base_url: str
    user_agent: str
    delay_seconds: float
    timeout_seconds: float
    retries: int
    cache_ttl_minutes: int
    cache_dir: Path
    top_competitors: int
    min_offers_for_stats: int
    price_deviation_threshold_pct: float
    desired_phrases: list[str]
    output_dir: Path
    report_filename: str
    repo_url: str = ""
    root: Path = field(default_factory=Path)

    @property
    def seller_url(self) -> str:
        return f"{self.base_url}/users/{self.seller_id}/"

    def all_categories(self) -> list[Category]:
        """Сиды + ручные дополнения, без дублей, с сохранением порядка."""
        seen: set[str] = set()
        result: list[Category] = []
        for cat in self.seed_categories:
            if cat.id not in seen:
                seen.add(cat.id)
                result.append(cat)
        for cid in self.manual_extra:
            cid = str(cid)
            if cid not in seen:
                seen.add(cid)
                result.append(Category(id=cid))
        return result


def _req(data: dict[str, Any], key: str, where: str) -> Any:
    if key not in data:
        raise ConfigError(f"Отсутствует обязательный ключ '{where}.{key}'")
    return data[key]


def apply_env_overrides(config: Config) -> Config:
    """Переопределения из переменных окружения для запуска в CI.

    В GitHub Actions удобнее задавать параметры переменными, чем править
    config.yaml в каждом workflow.
    """
    import os

    def env_int(name: str, current: int) -> int:
        raw = os.environ.get(name)
        if not raw:
            return current
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f"{name} должен быть числом, получено: {raw!r}")

    max_extra = env_int("FPA_MAX_CATEGORIES", config.max_extra_categories)
    if os.environ.get("FPA_SEED_ONLY", "").lower() in {"1", "true", "yes"}:
        config.discovery_enabled = False
        max_extra = 0
    if os.environ.get("FPA_NO_DISCOVERY", "").lower() in {"1", "true", "yes"}:
        config.discovery_enabled = False

    config.max_extra_categories = max_extra
    config.repo_url = os.environ.get("FPA_REPO_URL", config.repo_url)
    config.cache_ttl_minutes = env_int("FPA_CACHE_TTL_MINUTES", config.cache_ttl_minutes)
    seller = os.environ.get("FPA_SELLER_ID")
    if seller:
        config.seller_id = seller
    return config


def load_config(path: str | Path = "config.yaml") -> Config:
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"Файл конфигурации не найден: {path}")

    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ConfigError("config.yaml должен содержать YAML-объект верхнего уровня")

    root = path.parent

    seller = _req(raw, "seller", "root")
    seller_id = str(_req(seller, "id", "seller"))

    seeds_raw = _req(raw, "seed_categories", "root")
    if not isinstance(seeds_raw, list) or not seeds_raw:
        raise ConfigError("seed_categories должен быть непустым списком")

    seeds: list[Category] = []
    for i, item in enumerate(seeds_raw):
        if not isinstance(item, dict) or "id" not in item:
            raise ConfigError(f"seed_categories[{i}] должен содержать id")
        seeds.append(Category(id=str(item["id"]), name=str(item.get("name", ""))))

    disc = raw.get("discovery", {}) or {}
    http_cfg = raw.get("http", {}) or {}
    ana = raw.get("analytics", {}) or {}
    adv = raw.get("advice", {}) or {}
    out = raw.get("output", {}) or {}

    cache_dir = Path(http_cfg.get("cache_dir", "data/cache"))
    output_dir = Path(out.get("dir", "out"))

    return Config(
        seller_id=seller_id,
        seller_name=str(seller.get("name", seller_id)),
        brand=str(seller.get("brand", "")),
        seed_categories=seeds,
        discovery_enabled=bool(disc.get("enabled", True)),
        discovery_keywords=[str(k) for k in (disc.get("keywords") or [])],
        discovery_exclude_games=[str(k) for k in (disc.get("exclude_games") or [])],
        discovery_subcategory_hints=[str(k) for k in (disc.get("service_subcategories") or [])],
        max_extra_categories=int(disc.get("max_extra_categories", 12)),
        manual_extra=[str(x) for x in (disc.get("manual_extra") or [])],
        base_url=str(http_cfg.get("base_url", "https://funpay.com")).rstrip("/"),
        user_agent=str(http_cfg.get("user_agent", "Mozilla/5.0")),
        delay_seconds=float(http_cfg.get("delay_seconds", 1.0)),
        timeout_seconds=float(http_cfg.get("timeout_seconds", 30)),
        retries=int(http_cfg.get("retries", 3)),
        cache_ttl_minutes=int(http_cfg.get("cache_ttl_minutes", 60)),
        cache_dir=cache_dir if cache_dir.is_absolute() else root / cache_dir,
        top_competitors=int(ana.get("top_competitors", 10)),
        min_offers_for_stats=int(ana.get("min_offers_for_stats", 5)),
        price_deviation_threshold_pct=float(adv.get("price_deviation_threshold_pct", 15)),
        desired_phrases=[str(p) for p in (adv.get("desired_phrases") or [])],
        output_dir=output_dir if output_dir.is_absolute() else root / output_dir,
        report_filename=str(out.get("report_filename", "report.html")),
        repo_url=str(out.get("repo_url", "") or ""),
        root=root,
    )
