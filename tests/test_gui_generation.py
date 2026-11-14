"""Тесты поколения reconnect: устаревшие снимки не перезаписывают свежие."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication

from mchose_hub.gui.app import ManagerSnapshot

if TYPE_CHECKING:
    from pytest import MonkeyPatch


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_stale_reconnect_snapshot_ignored(
    qapp: QApplication, monkeypatch: MonkeyPatch
) -> None:
    """Колбэк с номером поколения не текущего — снимок отбрасывается."""
    from mchose_hub.gui.main_window import _DONGLE_ROW, _NOT_FOUND, MainWindow

    window = MainWindow(autostart=False)
    monkeypatch.setattr(window._dock_tab, "refresh", lambda: None)
    monkeypatch.setattr(window, "poll_now", lambda: None)

    fresh = ManagerSnapshot(dongle_path="/dev/hidraw9")
    window._reconnect_generation = 2

    window._apply_snapshot_if_current(1, fresh)  # устаревшее поколение
    assert window._device_labels[_DONGLE_ROW].text() == _NOT_FOUND

    window._apply_snapshot_if_current(2, fresh)  # актуальное поколение
    assert window._device_labels[_DONGLE_ROW].text() == "/dev/hidraw9 — подключён"
