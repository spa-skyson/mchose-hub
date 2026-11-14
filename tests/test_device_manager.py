"""Тесты DeviceManager: атомарные сессии команд, отложенное закрытие, single-flight."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import FakeTransport

from mchose_hub.gui.app import DeviceManager, ManagerSnapshot

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def _patch_discovery(monkeypatch: MonkeyPatch, dongles: list[FakeTransport]) -> None:
    """find_dongle/find_dock/scan_hidraw отдают переданные fake-транспорты."""
    from mchose_hub.gui import app as app_module

    dongle_iter = iter(dongles)
    monkeypatch.setattr(app_module, "find_dongle", lambda: next(dongle_iter, None))
    monkeypatch.setattr(app_module, "find_dock", lambda: None)
    monkeypatch.setattr(app_module, "scan_hidraw", list)


def test_discover_closes_immediately_without_operations(
    monkeypatch: MonkeyPatch,
) -> None:
    """Без активных команд переподключение закрывает старый транспорт сразу."""
    first = FakeTransport()
    _patch_discovery(monkeypatch, [first])
    manager = DeviceManager()
    manager.discover()
    assert manager.headset_service() is not None
    manager.discover()
    assert first.closed


def test_discover_defers_close_under_command_session(
    monkeypatch: MonkeyPatch,
) -> None:
    """fd не закрывается между unlock и lock: закрытие после выхода из сессии."""
    first, second = FakeTransport(), FakeTransport()
    _patch_discovery(monkeypatch, [first, second])
    manager = DeviceManager()
    manager.discover()
    with manager.command_session("headset") as service:
        assert service is not None
        manager.discover()  # команда «ещё выполняется» — транспорт отставлен
        assert not first.closed
        assert manager.headset_service() is not None
    assert first.closed  # закрыт сразу после освобождения
    assert not second.closed  # актуальный транспорт жив


def test_close_defers_while_command_session_active(
    monkeypatch: MonkeyPatch,
) -> None:
    """close() при активной команде откладывает закрытие до её завершения."""
    first = FakeTransport()
    _patch_discovery(monkeypatch, [first])
    manager = DeviceManager()
    manager.discover()
    with manager.command_session("headset") as service:
        assert service is not None
        manager.close()
        assert manager.headset_service() is None
        assert not first.closed
    assert first.closed


def test_command_session_atomic_against_discover_in_thread(
    monkeypatch: MonkeyPatch,
) -> None:
    """Сервис и учёт операции атомарны: discover из другого потока не закрывает fd.

    Последовательная имитация гонки: сессия открыта, discover() из чужого
    потока полностью завершён — старый транспорт ещё жив; закрытие строго
    после выхода из сессии.
    """
    first, second = FakeTransport(), FakeTransport()
    _patch_discovery(monkeypatch, [first, second])
    manager = DeviceManager()
    manager.discover()
    service = manager.headset_service()
    assert service is not None
    snapshots: list[ManagerSnapshot] = []
    scanned = threading.Event()

    def rescan() -> None:
        snapshots.append(manager.discover())
        scanned.set()

    with manager.command_session("headset") as current:
        assert current is service  # сервис получен под блокировкой менеджера
        worker = threading.Thread(target=rescan)
        worker.start()
        assert scanned.wait(2.0)
        worker.join()
        assert snapshots[0].dongle_path == second.path
        assert not first.closed  # fd жив под активной командой
        assert manager.headset_service() is not service
    assert first.closed  # закрыт строго после выхода из сессии


def test_discover_single_flight(monkeypatch: MonkeyPatch) -> None:
    """Параллельный discover не запускает второй скан, а присоединяется к нему."""
    from mchose_hub.gui import app as app_module

    scans: list[int] = []
    started = threading.Event()
    release = threading.Event()

    def slow_dongle() -> None:
        scans.append(1)
        started.set()
        release.wait(5.0)

    monkeypatch.setattr(app_module, "find_dongle", slow_dongle)
    monkeypatch.setattr(app_module, "find_dock", lambda: None)
    monkeypatch.setattr(app_module, "scan_hidraw", list)
    manager = DeviceManager()
    results: list[ManagerSnapshot] = []

    def background() -> None:
        results.append(manager.discover())

    worker = threading.Thread(target=background)
    worker.start()
    assert started.wait(2.0)
    results.append(manager.discover())  # присоединяется к текущему скану
    release.set()
    worker.join(5.0)
    assert len(scans) == 1  # второй скан не запускался
    assert len(results) == 2  # оба вызова получили снимок


def test_command_session_unknown_device() -> None:
    manager = DeviceManager()
    try:
        with manager.command_session("printer"):
            pass  # pragma: no cover — не доходит
    except ValueError:
        pass
    else:  # pragma: no cover — контракт нарушен
        raise AssertionError("ожидался ValueError для неизвестного устройства")
