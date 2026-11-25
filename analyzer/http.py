"""HTTP-слой: вежливые запросы, ретраи, дисковый кэш."""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger(__name__)


class FetchError(Exception):
    """Не удалось получить страницу."""


@dataclass
class Response:
    url: str
    text: str
    from_cache: bool


class Fetcher:
    """Качает страницы FunPay с паузами, ретраями и кэшем на диске.

    Кэш нужен не для скорости как таковой, а чтобы повторные прогоны
    не создавали лишнюю нагрузку на сайт.
    """

    def __init__(
        self,
        base_url: str,
        user_agent: str,
        delay_seconds: float = 1.0,
        timeout_seconds: float = 30.0,
        retries: int = 3,
        cache_dir: Path | None = None,
        cache_ttl_minutes: int = 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.delay_seconds = max(0.0, delay_seconds)
        self.timeout_seconds = timeout_seconds
        self.retries = max(1, retries)
        self.cache_dir = cache_dir
        self.cache_ttl_seconds = cache_ttl_minutes * 60
        self._last_request_at = 0.0
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            }
        )
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # --- кэш -------------------------------------------------------------

    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{digest}.html"

    def _read_cache(self, url: str) -> str | None:
        path = self._cache_path(url)
        if not path or not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > self.cache_ttl_seconds:
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _write_cache(self, url: str, text: str) -> None:
        path = self._cache_path(url)
        if not path:
            return
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:  # кэш — не критичная часть
            log.warning("Не удалось записать кэш для %s: %s", url, exc)

    # --- запросы ---------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_at
        remaining = self.delay_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.time()

    def get(self, url: str, use_cache: bool = True) -> Response:
        if use_cache:
            cached = self._read_cache(url)
            if cached is not None:
                log.debug("Кэш: %s", url)
                return Response(url=url, text=cached, from_cache=True)

        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                resp = self._session.get(url, timeout=self.timeout_seconds)
                if resp.status_code == 200:
                    resp.encoding = resp.encoding or "utf-8"
                    text = resp.text
                    self._write_cache(url, text)
                    return Response(url=url, text=text, from_cache=False)
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_error = FetchError(f"HTTP {resp.status_code} для {url}")
                    log.warning(
                        "Попытка %d/%d: HTTP %d, повтор", attempt, self.retries, resp.status_code
                    )
                    time.sleep(min(2 ** attempt, 30))
                    continue
                raise FetchError(f"HTTP {resp.status_code} для {url}")
            except requests.RequestException as exc:
                last_error = exc
                log.warning("Попытка %d/%d: %s", attempt, self.retries, exc)
                time.sleep(min(2 ** attempt, 30))

        raise FetchError(f"Не удалось получить {url}: {last_error}")

    def category_url(self, category_id: str) -> str:
        return f"{self.base_url}/lots/{category_id}/"

    def seller_url(self, seller_id: str) -> str:
        return f"{self.base_url}/users/{seller_id}/"
