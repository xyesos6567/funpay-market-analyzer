"""Тесты настройки вывода в консоль Windows.

Проверяется, что русские сообщения в логе не превращаются в кракозябры:
Python печатает в UTF-8, а консоль Windows работает в cp866.
"""

from __future__ import annotations

import ctypes
import sys
from types import SimpleNamespace

import pytest

from analyzer import cli


class FakeStream:
    def __init__(self, tty: bool) -> None:
        self._tty = tty
        self.reconfigures: list[dict] = []

    def isatty(self) -> bool:
        return self._tty

    def reconfigure(self, **kwargs) -> None:
        self.reconfigures.append(kwargs)


@pytest.fixture
def fake_console(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(kernel32=SimpleNamespace(GetConsoleOutputCP=lambda: 866)),
        raising=False,
    )


def test_console_encoding_applied_for_terminal(monkeypatch, fake_console) -> None:
    out, err = FakeStream(True), FakeStream(True)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    cli._configure_console_encoding()

    assert out.reconfigures == [{"encoding": "cp866", "errors": "replace"}]
    assert err.reconfigures == [{"encoding": "cp866", "errors": "replace"}]


def test_redirected_output_stays_utf8(monkeypatch, fake_console) -> None:
    """При перенаправлении в файл кодировку менять нельзя — там нужен UTF-8."""
    out, err = FakeStream(False), FakeStream(False)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    cli._configure_console_encoding()

    assert out.reconfigures == []
    assert err.reconfigures == []


def test_no_op_when_codepage_unknown(monkeypatch) -> None:
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(kernel32=SimpleNamespace(GetConsoleOutputCP=lambda: 0)),
        raising=False,
    )
    out = FakeStream(True)
    monkeypatch.setattr(sys, "stdout", out)

    cli._configure_console_encoding()

    assert out.reconfigures == []


def test_reconfigure_failure_does_not_crash(monkeypatch, fake_console) -> None:
    """Поток без поддержки reconfigure не должен ломать запуск."""

    class StubbornStream:
        def isatty(self) -> bool:
            return True

        def reconfigure(self, **kwargs) -> None:
            raise ValueError("не поддерживается")

    monkeypatch.setattr(sys, "stdout", StubbornStream())
    monkeypatch.setattr(sys, "stderr", StubbornStream())

    cli._configure_console_encoding()  # не должно бросить исключение


# --- переопределения для CI -------------------------------------------------


def test_env_overrides_for_ci(monkeypatch, tmp_path) -> None:
    """В облаке параметры приходят переменными окружения."""
    from analyzer.config import apply_env_overrides, load_config

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "seller:\n  id: 1\nseed_categories:\n  - id: 100\n  - id: 200\n",
        encoding="utf-8",
    )
    config = load_config(config_file)

    monkeypatch.setenv("FPA_MAX_CATEGORIES", "3")
    monkeypatch.setenv("FPA_REPO_URL", "https://github.com/user/repo")
    monkeypatch.setenv("FPA_CACHE_TTL_MINUTES", "0")
    apply_env_overrides(config)

    assert config.max_extra_categories == 3
    assert config.repo_url == "https://github.com/user/repo"
    assert config.cache_ttl_minutes == 0


def test_seed_only_disables_discovery(monkeypatch, tmp_path) -> None:
    """Для быстрого прогона в облаке автопоиск можно выключить."""
    from analyzer.config import apply_env_overrides, load_config

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "seller:\n  id: 1\nseed_categories:\n  - id: 100\n", encoding="utf-8"
    )
    config = load_config(config_file)
    monkeypatch.setenv("FPA_SEED_ONLY", "1")
    apply_env_overrides(config)

    assert config.discovery_enabled is False
    assert config.max_extra_categories == 0


def test_bad_env_value_is_reported(monkeypatch, tmp_path) -> None:
    """Некорректное значение должно давать понятную ошибку, а не тихий сбой."""
    from analyzer.config import ConfigError, apply_env_overrides, load_config

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "seller:\n  id: 1\nseed_categories:\n  - id: 100\n", encoding="utf-8"
    )
    config = load_config(config_file)
    monkeypatch.setenv("FPA_MAX_CATEGORIES", "много")

    with pytest.raises(ConfigError):
        apply_env_overrides(config)
