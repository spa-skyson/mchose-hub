"""Транспорт hidraw и discovery устройств (только stdlib, Linux).

Открытие /dev/hidrawN через os.open, запись/чтение отчётов через os.write/
os.read с select-таймаутом, feature-запросы через ioctl HIDIOCSFEATURE/
HIDIOCGFEATURE. Никаких hidapi/ctypes.
"""

from __future__ import annotations

import errno
import fcntl
import os
import queue
import select
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Protocol

from .protocol import (
    FEATURE_AA_REPORT_ID,
    PID_DOCK,
    PID_HEADSET_DONGLE,
    PID_HEADSET_WIRED,
    VID_MCHOSE,
    build_channel_close,
    build_channel_open,
)

_HIDRAW_SYSFS = "/sys/class/hidraw"
_REPORT_MAX_SIZE = 64
# Бюджет ожидания записи: hidraw-запись непрерываема из пользовательского
# пространства (poll/сигналы не работают), поэтому ограничивается ожидание
# вызывающего кода, а ядерная запись (до 5 с USB-таймаута) завершается в фоне.
WRITE_TIMEOUT_S = 2.0
# Дополнительное ожидание исхода уже поданной записи (2 + 4 = до 6 c суммарно,
# с запасом перекрывает 5-секундный ядерный таймаут): к моменту выхода из
# write_report исход записи всегда разрешён — LOCK сессии уходит после него.
WRITE_GRACE_S = 4.0
# Бюджет ожидания застрявшей записи при close(): сверх него транспорт
# отравляется, fd сознательно не закрывается (утечка одного fd вместо
# неограниченной блокировки выхода). Писатель — daemon-поток: не ждётся
# и умирает вместе с процессом.
CLOSE_GRACE_S = 5.0

UDEV_HINT = (
    "установите правила из packaging/99-mchose-hub.rules в /etc/udev/rules.d/ "
    "и выполните: sudo udevadm control --reload && sudo udevadm trigger"
)
# Донгл сторждит interrupt OUT, когда наушники вне 2.4G-линка или спят
# (ETIMEDOUT на os.write / SET_FEATURE) — PROTOCOL.md §7.
ETIMEDOUT_HEADSET_HINT = (
    "наушники вне 2.4G или спят; если не помогает — передёрните донгл "
    "(выньте и вставьте обратно)"
)


def hid_ioctl(*, get: bool, length: int) -> int:
    """Директива hidraw HIDIOCSFEATURE/HIDIOCGFEATURE (PROTOCOL.md §7).

    HIDIOCSFEATURE(len) = 0xC0000000 | (len << 16) | 0x4806
    HIDIOCGFEATURE(len) = 0xC0000000 | (len << 16) | 0x4807
    где length включает reportId-байт.
    """
    request = 0x4807 if get else 0x4806
    return 0xC0000000 | (length << 16) | request


def _error_hint(exc: OSError) -> str:
    return ETIMEDOUT_HEADSET_HINT if exc.errno == errno.ETIMEDOUT else ""


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

    path: str
    """Путь к hidraw-узлу устройства (/dev/hidrawN)."""

    def write_report(self, report_id: int, payload: bytes) -> int:
        """Записать отчёт: на провод уходит [reportId] + payload."""
        ...

    def read_report(self, timeout_ms: int) -> bytes | None:
        """Прочитать отчёт (первый байт = reportId); None по таймауту."""
        ...

    def send_feature(self, report_id: int, payload: bytes) -> int:
        """SET_FEATURE: буфер [reportId] + payload, ioctl по фактической длине."""
        ...

    def get_feature(self, report_id: int, size: int) -> bytes:
        """GET_FEATURE: буфер size байтов, buffer[0] = reportId."""
        ...

    def unlock_command_channel(self) -> None:
        """UNLOCK командного канала донгла (SET_FEATURE 0xAA, §7)."""
        ...

    def lock_command_channel(self) -> None:
        """LOCK командного канала донгла (SET_FEATURE 0xAA, §7)."""
        ...

    def close(self) -> None:
        """Закрыть устройство."""
        ...


@dataclass
class _WriteTask:
    """Задача очереди писателя: wire-кадр и future для результата."""

    fd: int
    wire: bytes
    result: Future[int]


class HidrawTransport:
    """Сырой hidraw-транспорт с блокировкой на каждую операцию.

    Записи обслуживает один daemon-поток (очередь queue.Queue): hidraw-запись
    непрерываема, поэтому застрявшая (NAK) запись не должна удерживать
    не-daemon поток — интерпретатор иначе дождался бы его при выходе.
    """

    def __init__(
        self,
        dev_path: str,
        *,
        write_timeout_s: float = WRITE_TIMEOUT_S,
        write_grace_s: float = WRITE_GRACE_S,
        close_grace_s: float = CLOSE_GRACE_S,
    ) -> None:
        self.path = dev_path
        self._lock = threading.Lock()
        self._write_timeout_s = write_timeout_s
        self._write_grace_s = write_grace_s
        self._close_grace_s = close_grace_s
        self._queue: queue.Queue[_WriteTask | None] = queue.Queue()
        self._writer: threading.Thread | None = None
        self._last_write: Future[int] | None = None
        self._poisoned = False
        self._closing = False  # close начат: новые записи отвергаются
        try:
            self._fd = os.open(dev_path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError as exc:
            raise TransportError(
                f"нет доступа на запись в {dev_path}", UDEV_HINT
            ) from exc
        except OSError as exc:
            raise TransportError(f"не удалось открыть {dev_path}: {exc}") from exc

    def _ensure_alive(self) -> None:
        """Отравленный транспорт не обслуживает операции (fd сознательно не закрыт)."""
        if self._poisoned:
            raise TransportError(
                f"транспорт {self.path} отравлен незавершённой записью"
            )

    def write_report(self, report_id: int, payload: bytes) -> int:
        """Записать отчёт; первым байтом всегда пишется reportId.

        Для дока reportId=0 — нулевой байт всё равно пишется (требование
        hidraw для нумерованных отчётов). Запись уходит в выделенный поток,
        ожидание ограничено write_timeout_s; по истечении бюджета исход
        записи дожидается ещё write_grace_s: write_report возвращает
        управление только когда судьба данных известна (успех / ошибка /
        не разрешена), поэтому LOCK в finally сессии всегда уходит после
        финала записи. Запись, не разрешённая даже за grace, отравляет
        транспорт (все дальнейшие операции дают TransportError).
        """
        wire = bytes([report_id]) + payload
        future = self._submit_write(wire)
        try:
            return future.result(timeout=self._write_timeout_s)
        except TimeoutError:
            outcome = self._await_write_outcome(future)
            if outcome is None:
                self._poisoned = True
                raise TransportError(
                    f"запись в {self.path} не разрешена за "
                    f"{self._write_timeout_s + self._write_grace_s:.1f} с",
                    ETIMEDOUT_HEADSET_HINT,
                ) from None
            if isinstance(outcome, Exception):
                hint = _error_hint(outcome) if isinstance(outcome, OSError) else ""
                raise TransportError(
                    f"ошибка записи в {self.path}: {outcome}", hint
                ) from outcome
            raise TransportError(
                f"запись в {self.path} не подтверждена: данные могли дойти",
                ETIMEDOUT_HEADSET_HINT,
            )
        except OSError as exc:
            raise TransportError(
                f"ошибка записи в {self.path}: {exc}", _error_hint(exc)
            ) from exc

    def _await_write_outcome(self, future: Future[int]) -> int | Exception | None:
        """Дождаться исхода записи в пределах grace; None — не завершилась."""
        try:
            return future.result(timeout=self._write_grace_s)
        except TimeoutError:
            return None
        except Exception as exc:  # noqa: BLE001 — исход передаётся вызывающему
            return exc

    def _submit_write(self, wire: bytes) -> Future[int]:
        """Поставить запись в очередь писателя; сериализация бесплатна."""
        with self._lock:
            self._ensure_alive()
            if self._closing:
                raise TransportError(f"транспорт {self.path} закрывается")
            fd = self._fd
            if fd < 0:
                raise TransportError(f"устройство {self.path} закрыто")
            if self._writer is None or not self._writer.is_alive():
                self._writer = threading.Thread(
                    target=self._write_loop,
                    name=f"hidraw-write-{self.path}",
                    daemon=True,  # умирает вместе с процессом, не держит выход
                )
                self._writer.start()
            future: Future[int] = Future()
            self._queue.put(_WriteTask(fd=fd, wire=wire, result=future))
            self._last_write = future
            return future

    def _write_loop(self) -> None:
        """Цикл писателя: задачи из очереди до стоп-токена (None).

        Исход каждой задачи разрешается в future: ждущий всегда узнаёт
        успех/ошибку; застрявшая задача остаётся в ядре, поток ждёт
        следующую (для отравленного транспорта — просто до конца процесса).
        """
        while True:
            task = self._queue.get()
            if task is None:
                return
            try:
                task.result.set_result(os.write(task.fd, task.wire))
            except Exception as exc:  # noqa: BLE001 — исход уходит ждущему
                task.result.set_exception(exc)

    def read_report(self, timeout_ms: int) -> bytes | None:
        """Чтение до 64 байт с таймаутом; None, если устройство молчит."""
        with self._lock:
            self._ensure_alive()
            try:
                ready, _, _ = select.select([self._fd], [], [], timeout_ms / 1000)
            except OSError as exc:
                raise TransportError(f"ошибка ожидания {self.path}: {exc}") from exc
            if not ready:
                return None
            try:
                chunk = os.read(self._fd, _REPORT_MAX_SIZE)
            except BlockingIOError:
                return None
            except OSError as exc:
                raise TransportError(f"ошибка чтения {self.path}: {exc}") from exc
            if not chunk:
                raise TransportError(f"{self.path}: устройство отключено (EOF)")
            return chunk

    def send_feature(self, report_id: int, payload: bytes) -> int:
        """Feature-запись HIDIOCSFEATURE(len), len = 1 + len(payload).

        Буфер ioctl включает reportId-байт; директива строится по
        фактической длине буфера (PROTOCOL.md §7).
        """
        buffer = bytes([report_id]) + payload
        with self._lock:
            self._ensure_alive()
            try:
                result = fcntl.ioctl(
                    self._fd,
                    hid_ioctl(get=False, length=len(buffer)),
                    bytearray(buffer),
                )
            except OSError as exc:
                raise TransportError(
                    f"ошибка SET_FEATURE(0x{report_id:02X}) в {self.path}: {exc}",
                    _error_hint(exc),
                ) from exc
        return result if isinstance(result, int) else len(buffer)

    def get_feature(self, report_id: int, size: int) -> bytes:
        """Feature-чтение HIDIOCGFEATURE(len); size включает reportId."""
        buf = bytearray(min(max(size, 1), _REPORT_MAX_SIZE))
        buf[0] = report_id
        with self._lock:
            self._ensure_alive()
            try:
                result = fcntl.ioctl(
                    self._fd, hid_ioctl(get=True, length=len(buf)), buf
                )
            except OSError as exc:
                raise TransportError(
                    f"ошибка GET_FEATURE(0x{report_id:02X}) в {self.path}: {exc}",
                    _error_hint(exc),
                ) from exc
        count = result if isinstance(result, int) else len(buf)
        return bytes(buf[: max(count, 0)])

    def unlock_command_channel(self) -> None:
        """UNLOCK командного канала: SET_FEATURE(0xAA) = [0xAA,0x01,0x01]."""
        self.send_feature(FEATURE_AA_REPORT_ID, build_channel_open())

    def lock_command_channel(self) -> None:
        """LOCK командного канала: SET_FEATURE(0xAA) = [0xAA,0x01,0x00]."""
        self.send_feature(FEATURE_AA_REPORT_ID, build_channel_close())

    def close(self) -> None:
        """Закрыть дескриптор (идемпотентно), ограничив ожидание записи.

        С момента вызова транспорт помечен закрывающимся (флаг под
        блокировкой): _submit_write отвергает новые записи — дренирование
        полно, все задачи, принятые до close, дожидаются исхода в пределах
        close_grace_s. Если запись не завершилась за grace — транспорт
        отравляется, fd сознательно НЕ закрывается (утечка одного fd лучше
        неограниченной блокировки выхода и лучше записи в чужой fd,
        переиспользовавший номер). Писатель — daemon-поток: close его НЕ
        ждёт, поток умирает вместе с процессом. Детерминированный исход:
        фоновая запись может завершиться после close в не-закрытый fd;
        при выходе процесса daemon-поток прерывается ядром — процесс
        завершается всегда.
        """
        if self._poisoned:
            return  # отравлен: ждать нечего, fd остаётся незакрытым
        with self._lock:
            self._closing = True  # отвергнуть записи до конца дренирования
            last, self._last_write = self._last_write, None
        drained = True
        if last is not None:
            try:
                last.result(timeout=self._close_grace_s)
            except TimeoutError:
                drained = False
            except Exception:  # noqa: BLE001, S110 — ошибка записи: исход известен
                pass
        if not drained:
            self._poisoned = True
            return  # daemon-писателя не ждём — умирает с процессом
        with self._lock:
            if self._writer is not None and self._writer.is_alive():
                self._queue.put(None)  # стоп-токен: писатель завершится сам
            self._writer = None
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
