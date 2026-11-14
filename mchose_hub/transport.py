"""Транспорт hidraw и discovery устройств (только stdlib, Linux).

Открытие /dev/hidrawN через os.open, запись/чтение отчётов через os.write/
os.read с select-таймаутом, feature-запросы через ioctl HIDIOCSFEATURE/
HIDIOCGFEATURE. Никаких hidapi/ctypes.
"""

from __future__ import annotations

import fcntl
import os
import select
import threading
from typing import Protocol

from .protocol import (
    PID_DOCK,
    PID_HEADSET_DONGLE,
    PID_HEADSET_WIRED,
    VID_MCHOSE,
)

_HIDRAW_SYSFS = "/sys/class/hidraw"
_REPORT_MAX_SIZE = 64

UDEV_HINT = (
    "установите правила из packaging/99-mchose-hub.rules в /etc/udev/rules.d/ "
    "и выполните: sudo udevadm control --reload && sudo udevadm trigger"
)

# hidraw ioctl для feature-отчётов с буфером 64 байта.
_HIDIOCSFEATURE_64 = 0x40064806
_HIDIOCGFEATURE_64 = 0xC0064807


class TransportError(Exception):
    """Ошибка транспорта с человекочитаемым сообщением и подсказкой."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return self.message


class HidTransport(Protocol):
    """Минимальный контракт транспорта для сервисного слоя."""

    def write_report(self, report_id: int, payload: bytes) -> int:
        """Записать отчёт: на провод уходит [reportId] + payload."""
        ...

    def read_report(self, timeout_ms: int) -> bytes | None:
        """Прочитать отчёт (первый байт = reportId); None по таймауту."""
        ...

    def close(self) -> None:
        """Закрыть устройство."""
        ...


class HidrawTransport:
    """Сырой hidraw-транспорт с блокировкой на каждую операцию."""

    def __init__(self, dev_path: str) -> None:
        self.path = dev_path
        self._lock = threading.Lock()
        try:
            self._fd = os.open(dev_path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError as exc:
            raise TransportError(
                f"нет доступа на запись в {dev_path}", UDEV_HINT
            ) from exc
        except OSError as exc:
            raise TransportError(f"не удалось открыть {dev_path}: {exc}") from exc

    def write_report(self, report_id: int, payload: bytes) -> int:
        """Записать отчёт; первым байтом всегда пишется reportId.

        Для дока reportId=0 — нулевой байт всё равно пишется (требование
        hidraw для нумерованных отчётов).
        """
        wire = bytes([report_id]) + payload
        with self._lock:
            try:
                return os.write(self._fd, wire)
            except OSError as exc:
                raise TransportError(f"ошибка записи в {self.path}: {exc}") from exc

    def read_report(self, timeout_ms: int) -> bytes | None:
        """Чтение до 64 байт с таймаутом; None, если устройство молчит."""
        with self._lock:
            try:
                ready, _, _ = select.select([self._fd], [], [], timeout_ms / 1000)
            except OSError as exc:
                raise TransportError(f"ошибка ожидания {self.path}: {exc}") from exc
            if not ready:
                return None
            try:
                return os.read(self._fd, _REPORT_MAX_SIZE)
            except BlockingIOError:
                return None
            except OSError as exc:
                raise TransportError(f"ошибка чтения {self.path}: {exc}") from exc

    def send_feature(self, buffer: bytes) -> int:
        """Feature-запрос HIDIOCSFEATURE(64); buffer включает reportId."""
        with self._lock:
            result = fcntl.ioctl(self._fd, _HIDIOCSFEATURE_64, bytearray(buffer))
        return result if isinstance(result, int) else len(buffer)

    def get_feature(self, size: int, report_id: int = 0) -> bytes:
        """Feature-чтение HIDIOCGFEATURE(64); size <= 64, buffer[0]=reportId."""
        buf = bytearray(min(max(size, 1), _REPORT_MAX_SIZE))
        buf[0] = report_id
        with self._lock:
            result = fcntl.ioctl(self._fd, _HIDIOCGFEATURE_64, buf)
        count = result if isinstance(result, int) else len(buf)
        return bytes(buf[: max(count, 0)])

    def close(self) -> None:
        """Закрыть дескриптор (идемпотентно)."""
        with self._lock:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1


def _parse_hid_id(value: str) -> tuple[int, int] | None:
    """HID_ID=bus:vendor:product (hex) -> (vid, pid)."""
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[1], 16), int(parts[2], 16)
    except ValueError:
        return None


def scan_hidraw() -> list[tuple[str, int, int]]:
    """Скан /sys/class/hidraw: [(путь /dev/hidrawN, vid, pid), ...]."""
    try:
        entries = os.listdir(_HIDRAW_SYSFS)
    except OSError:
        return []
    entries.sort(key=lambda name: int(name[6:]) if name[6:].isdigit() else 1 << 30)
    devices: list[tuple[str, int, int]] = []
    for name in entries:
        uevent = os.path.join(_HIDRAW_SYSFS, name, "device", "uevent")
        try:
            with open(uevent, encoding="utf-8") as handle:
                content = handle.read()
        except OSError:
            continue
        for line in content.splitlines():
            if line.startswith("HID_ID="):
                ids = _parse_hid_id(line[len("HID_ID=") :])
                if ids is not None:
                    devices.append((f"/dev/{name}", ids[0], ids[1]))
                break
    return devices


def find_device(vid: int, pids: int | tuple[int, ...]) -> HidrawTransport | None:
    """Открыть первое устройство vid + один из pids.

    Нет совпадений -> None; открыть не удалось (права/отсутствие узла) ->
    TransportError с подсказкой про udev.
    """
    wanted = (pids,) if isinstance(pids, int) else tuple(pids)
    last_error: TransportError | None = None
    for path, dev_vid, dev_pid in scan_hidraw():
        if dev_vid != vid or dev_pid not in wanted:
            continue
        try:
            return HidrawTransport(path)
        except TransportError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    return None


def find_dongle() -> HidrawTransport | None:
    """Донгл наушников: PID 600A (V9 Turbo+) или 600D (V9 Turbo)."""
    return find_device(VID_MCHOSE, (PID_HEADSET_DONGLE, PID_HEADSET_WIRED))


def find_dock() -> HidrawTransport | None:
    """RGB-док: PID 6008."""
    return find_device(VID_MCHOSE, PID_DOCK)
