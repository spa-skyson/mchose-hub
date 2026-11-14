"""Тесты воркеров: pending-слоты, замена отложенных запросов, shutdown."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _spin_until(
    qapp: QApplication, condition: Callable[[], bool], timeout_s: float = 3.0
) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline and not condition():
        qapp.processEvents()
        time.sleep(0.02)


def test_pending_job_does_not_start_immediately(qapp: QApplication) -> None:
    """Pending-job потока не получает и не выполняется до завершения running."""
    from mchose_hub.gui.workers import WorkerHost

    host = WorkerHost()
    gate = threading.Event()
    executed: list[str] = []
    host.run(
        lambda: "first" if gate.wait(5.0) else "first-timeout",
        on_success=executed.append,
        device="headset",
    )
    outcome = host.run(
        lambda: "second",
        on_success=executed.append,
        device="headset",
    )
    assert outcome is None  # pending: поток под него не создавался
    assert executed == []  # pending не стартовал
    gate.set()
    _spin_until(qapp, lambda: executed == ["first", "second"])
    host.shutdown()


def test_repeated_run_replaces_pending(qapp: QApplication) -> None:
    """Повторный запрос заменяет pending: предыдущий отложенный не выполняется."""
    from mchose_hub.gui.workers import WorkerHost

    host = WorkerHost()
    gate = threading.Event()
    executed: list[str] = []
    host.run(
        lambda: "first" if gate.wait(5.0) else "first-timeout",
        on_success=executed.append,
        device="dock",
    )
    host.run(lambda: "second", on_success=executed.append, device="dock")
    host.run(lambda: "third", on_success=executed.append, device="dock")
    gate.set()
    _spin_until(qapp, lambda: executed == ["first", "third"])
    assert "second" not in executed
    host.shutdown()


def test_shutdown_retry_phases(qapp: QApplication) -> None:
    """Три фазы выхода: таймаут → False, повтор до завершения → False, успех → True.

    Незавершённый воркер остаётся под владением хоста: повторный shutdown
    ждёт его заново. True только когда живых воркеров нет.
    """
    from mchose_hub.gui.workers import WorkerHost

    host = WorkerHost()
    gate = threading.Event()
    host.run(lambda: gate.wait(30.0), device="headset")
    # Фаза 1: воркер жив — False, воркер остаётся под владением.
    assert host.shutdown(timeout_ms=200) is False
    assert len(host._workers) == 1
    # Фаза 2: повторный выход до завершения воркера — снова False.
    assert host.shutdown(timeout_ms=200) is False
    assert len(host._workers) == 1
    # Фаза 3: воркер завершился — True, список очищается finished-cleanup'ом.
    gate.set()
    assert host.shutdown(timeout_ms=3000) is True
    _spin_until(qapp, lambda: not host._workers)
    assert host._workers == []
    # Хост продолжает принимать job'ы после успешного выхода.
    host.run(lambda: None, device="headset")
    assert host.shutdown(timeout_ms=3000) is True


def test_shutdown_drops_pending(qapp: QApplication) -> None:
    """При выходе pending-запросы отбрасываются: выполняется только активный."""
    from mchose_hub.gui.workers import WorkerHost

    host = WorkerHost()
    gate = threading.Event()
    executed: list[str] = []
    host.run(
        lambda: "active" if gate.wait(5.0) else "active-timeout",
        on_success=executed.append,
        device="headset",
    )
    host.run(lambda: "pending", on_success=executed.append, device="headset")
    gate.set()  # активный job завершается, pending должен быть отброшен
    assert host.shutdown(timeout_ms=3000) is True
    _spin_until(qapp, lambda: executed == ["active"])
    assert executed == ["active"]  # pending на выходе не выполнялся
