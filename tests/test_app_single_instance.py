"""Тесты single-instance guard (QLockFile, offscreen, без event loop)."""

from __future__ import annotations

from pathlib import Path

from mchose_hub.gui.app import try_acquire_single_instance


def _isolate_home(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "mchose_hub.gui.app.Path.home", lambda: tmp_path
    )


def test_first_instance_acquires_lock(tmp_path: Path, monkeypatch: object) -> None:
    """Первый экземпляр захватывает lock; файл блокировки создан."""
    _isolate_home(tmp_path, monkeypatch)
    lock = try_acquire_single_instance()
    assert lock is not None
    assert (tmp_path / ".cache" / "mchose-hub" / "app.lock").exists()
    lock.unlock()


def test_second_instance_refused(tmp_path: Path, monkeypatch: object) -> None:
    """Пока lock занят, второй экземпляр получает None."""
    _isolate_home(tmp_path, monkeypatch)
    first = try_acquire_single_instance()
    assert first is not None
    try:
        assert try_acquire_single_instance() is None
    finally:
        first.unlock()


def test_lock_released_allows_new_instance(tmp_path: Path, monkeypatch: object) -> None:
    """После release() (штатный выход) захват работает снова."""
    _isolate_home(tmp_path, monkeypatch)
    first = try_acquire_single_instance()
    assert first is not None
    first.unlock()
    second = try_acquire_single_instance()
    assert second is not None
    second.unlock()
