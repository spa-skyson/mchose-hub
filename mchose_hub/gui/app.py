"""Сборка приложения: DeviceManager, QApplication и точка входа main().

DeviceManager владеет транспортами и сервисами ядра. Его сервисные методы
вызываются только из фоновых воркеров (workers.py); close() при выходе
допустим из GUI-потока — это просто os.close дескрипторов.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, overload

from PyQt6.QtCore import QLockFile
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from ..service import DockService, HeadsetService
from ..transport import (
    HidTransport,
    TransportError,
    find_dock,
    find_dongle,
    scan_hidraw,
)
from .tray import paint_battery_pixmap

APP_TITLE = "MCHOSE Hub"


@dataclass(frozen=True)
class ManagerSnapshot:
    """Снимок состояния устройств для GUI-потока."""

    dongle_path: str | None = None
    dock_path: str | None = None
    dongle_error: str = ""
    dock_error: str = ""
    hint: str = ""
    devices: tuple[tuple[str, int, int], ...] = ()

    @property
    def has_dongle(self) -> bool:
        return self.dongle_path is not None

    @property
    def has_dock(self) -> bool:
        return self.dock_path is not None


class DeviceManager:
    """Транспорты и сервисы ядра; все обращения — только из воркеров."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._discover_lock = threading.Lock()
        self._dongle: HidTransport | None = None
        self._dock: HidTransport | None = None
        self._headset: HeadsetService | None = None
        self._dock_service: DockService | None = None
        self.snapshot = ManagerSnapshot()
        self._ops = 0
        self._stale: list[HidTransport] = []

    @overload
    def command_session(
        self, device: Literal["headset"]
    ) -> AbstractContextManager[HeadsetService | None]: ...

    @overload
    def command_session(
        self, device: Literal["dock"]
    ) -> AbstractContextManager[DockService | None]: ...

    def command_session(
        self, device: str
    ) -> AbstractContextManager[HeadsetService | DockService | None]:
        """Атомарно: сервис устройства + регистрация активной команды.

        Проверка наличия устройства и учёт операции происходят под одной
        блокировкой менеджера: между получением сервиса и началом команды
        discover() не может закрыть транспорт. Пока команда идёт, устаревшие
        транспорты закрываются только после выхода из контекста — fd не
        закрывается между unlock и lock (PROTOCOL.md §7). None — устройство
        отсутствует.
        """
        return self._command_session(device)

    @contextmanager
    def _command_session(
        self, device: str
    ) -> Iterator[HeadsetService | DockService | None]:
        with self._lock:
            service: HeadsetService | DockService | None
            if device == "headset":
                service = self._headset
            elif device == "dock":
                service = self._dock_service
            else:
                raise ValueError(f"неизвестное устройство: {device}")
            registered = service is not None
            if registered:
                self._ops += 1
        try:
            yield service
        finally:
            stale: list[HidTransport] = []
            with self._lock:
                if registered:
                    self._ops -= 1
                    if self._ops == 0:
                        stale, self._stale = self._stale, []
            for transport in stale:
                transport.close()

    def headset_service(self) -> HeadsetService | None:
        """Текущий сервис наушников или None, если донгла нет."""
        with self._lock:
            return self._headset

    def dock_service(self) -> DockService | None:
        """Текущий сервис дока или None, если дока нет."""
        with self._lock:
            return self._dock_service

    def discover(self) -> ManagerSnapshot:
        """Пересканировать hidraw; single-flight: параллельный вызов ждёт текущий.

        Если скан уже выполняется, новый вызов не запускает второй, а
        дожидается его и получает его же свежий снимок — два discover()
        из разных потоков не выполняются параллельно.
        """
        if not self._discover_lock.acquire(blocking=False):
            with self._discover_lock:
                return self.snapshot
        try:
            with self._lock:
                self._retire_locked()
                dongle_error = dock_error = hint = ""
                try:
                    dongle = find_dongle()
                except TransportError as exc:
                    dongle, dongle_error, hint = None, exc.message, exc.hint
                try:
                    dock = find_dock()
                except TransportError as exc:
                    dock, dock_error, hint = None, exc.message, exc.hint
                self._dongle = dongle
                self._dock = dock
                self._headset = (
                    HeadsetService(self._dongle) if self._dongle is not None else None
                )
                self._dock_service = (
                    DockService(self._dock) if self._dock is not None else None
                )
                devices = tuple(scan_hidraw())
                self.snapshot = ManagerSnapshot(
                    dongle_path=self._dongle.path if self._dongle is not None else None,
                    dock_path=self._dock.path if self._dock is not None else None,
                    dongle_error=dongle_error,
                    dock_error=dock_error,
                    hint=hint,
                    devices=devices,
                )
                return self.snapshot
        finally:
            self._discover_lock.release()

    def close(self) -> None:
        """Закрыть транспорты; при активных командах — отложить до их конца."""
        with self._lock:
            self._retire_locked()

    def _retire_locked(self) -> None:
        """Снять сервисы; транспорты закрыть сейчас или после операций."""
        transports = [t for t in (self._dongle, self._dock) if t is not None]
        self._dongle = None
        self._dock = None
        self._headset = None
        self._dock_service = None
        if self._ops > 0:
            self._stale.extend(transports)
            return
        for transport in transports:
            transport.close()


def try_acquire_single_instance() -> QLockFile | None:
    """Single-instance guard: QLockFile в ~/.cache/mchose-hub/app.lock.

    QLockFile сам снимает stale-лок от крашнувшегося процесса (в lock-файле
    записан pid владельца). None — экземпляр уже запущен. Не требует
    QApplication: вызвать до создания GUI.
    """
    lock_path = Path.home() / ".cache" / "mchose-hub" / "app.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(lock_path))
    if not lock.tryLock(100):
        return None
    return lock


def main() -> int:
    """Точка входа GUI: single-instance, QApplication, окно, цикл событий."""
    lock = try_acquire_single_instance()
    if lock is None:
        print("MCHOSE Hub уже запущен")
        return 0
    try:
        app = QApplication(sys.argv)
        app.setApplicationName(APP_TITLE)
        app.setOrganizationName("mchose-hub")
        app.setApplicationVersion("0.2.2")
        app.setWindowIcon(
            QIcon(paint_battery_pixmap(100, charging=False, asleep=False))
        )
        from .main_window import MainWindow  # локально: избегаем цикла импортов

        window = MainWindow()
        window.show()
        return app.exec()
    finally:
        lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
