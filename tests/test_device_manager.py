"""Тесты DeviceManager: атомарные сессии команд, отложенное закрытие, single-flight."""

from __future__ import annotations

import errno
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


# --- close() выведенных транспортов вне блокировки менеджера (находка ревью) ---


def test_discover_closes_stale_transport_outside_lock(
    monkeypatch: MonkeyPatch,
) -> None:
    """Блокирующий close() старого транспорта не держит блокировку менеджера."""
    from mchose_hub.gui import app as app_module

    started = threading.Event()
    release = threading.Event()

    class _WedgedClose:
        path = "/dev/wedge"

        def close(self) -> None:
            started.set()
            release.wait(5)  # close() висит: LOCK-ioctl залип на устройстве

    wedge = _WedgedClose()
    dongles = iter([wedge, None])  # второй скан: донгл уже выдернут
    monkeypatch.setattr(app_module, "find_dongle", lambda: next(dongles, None))
    monkeypatch.setattr(app_module, "find_dock", lambda: None)
    monkeypatch.setattr(app_module, "scan_hidraw", list)
    manager = DeviceManager()
    manager.discover()

    snapshots: list[ManagerSnapshot] = []
    worker = threading.Thread(target=lambda: snapshots.append(manager.discover()))
    worker.start()
    assert started.wait(2)  # close() wedge выполняется в чужом потоке
    # Пока он висит, блокировка менеджера свободна: command_session берётся.
    with manager._lock:
        acquired = True
    assert acquired
    release.set()
    worker.join(5)
    assert snapshots == [ManagerSnapshot()]  # донгл больше не найден


# --- EIO = отключение: авто-ретрай пере-сканирует на новый узел ---


def test_eio_recovery_moves_to_new_node(monkeypatch: MonkeyPatch) -> None:
    """EIO помечает транспорт отключённым: ретрай открывает донгл на новом узле."""
    from mchose_hub import transport as transport_module
    from mchose_hub.gui.helpers import with_transport_retry

    # Тот же TransportError, что строит транспорт для EIO при чтении.
    error = transport_module._data_oserror(
        "/dev/hidraw1", "чтения", OSError(errno.EIO, "Ошибка ввода/вывода")
    )
    assert "устройство отключено" not in error.message
    assert "донгл будет найден заново" in error.message

    dead = FakeTransport()

    def dead_read(timeout_ms: int) -> bytes | None:
        raise error

    dead.read_report = dead_read  # type: ignore[method-assign]
    alive = FakeTransport()
    _patch_discovery(monkeypatch, [dead, alive])
    manager = DeviceManager()
    manager.discover()
    assert not dead.closed

    def pump_tick() -> int:
        with manager.command_session("headset") as service:
            assert service is not None
            return service.pump_passive()

    assert with_transport_retry(pump_tick, manager.discover)() == 0
    assert dead.closed  # старый узел закрыт при пере-скане
    assert not alive.closed  # работа продолжилась на новом транспорте
    assert manager.snapshot.dongle_path == alive.path
