"""Тесты discovery-слоя (скан hidraw в sysfs) и транспорта."""

from __future__ import annotations

import errno
import threading
import time
from concurrent.futures import Future
from pathlib import Path

import pytest

from mchose_hub import transport
from mchose_hub.protocol import PID_HEADSET_DONGLE, VID_MCHOSE


class _ResultProbe(Future[int]):
    """Future-обёртка: сигнализирует, когда close вошёл в ожидание дренирования."""

    def __init__(self, inner: Future[int]) -> None:
        super().__init__()
        self._inner = inner
        self.entered_wait = threading.Event()

    def result(self, timeout: float | None = None) -> int:
        self.entered_wait.set()  # close уже взял _lock и пометил _closing
        return self._inner.result(timeout)


def _make_uevent(root: Path, name: str, hid_id: str | None) -> None:
    device_dir = root / "hidraw" / name / "device"
    device_dir.mkdir(parents=True)
    lines = ["DRIVER=hid-generic", "HID_NAME=MCHOSE Device"]
    if hid_id is not None:
        lines.append(f"HID_ID={hid_id}")
    (device_dir / "uevent").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_scan_hidraw_parses_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transport, "_HIDRAW_SYSFS", str(tmp_path / "hidraw"))
    _make_uevent(tmp_path, "hidraw0", "0003:00003837:0000600A")
    _make_uevent(tmp_path, "hidraw1", "0003:00001552:0000A000")
    _make_uevent(tmp_path, "hidraw2", None)
    devices = transport.scan_hidraw()
    assert devices == [
        ("/dev/hidraw0", VID_MCHOSE, PID_HEADSET_DONGLE),
        ("/dev/hidraw1", 0x1552, 0xA000),
    ]


def test_scan_hidraw_missing_sysfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transport, "_HIDRAW_SYSFS", str(tmp_path / "absent"))
    assert transport.scan_hidraw() == []


def test_parse_hid_id() -> None:
    assert transport._parse_hid_id("0003:00003837:0000600A") == (0x3837, 0x600A)
    assert transport._parse_hid_id("0005:1:2") == (1, 2)
    assert transport._parse_hid_id("0003:zz:2") is None
    assert transport._parse_hid_id("0003:1") is None


def test_hid_ioctl_directives() -> None:
    """HIDIOCSFEATURE/HIDIOCGFEATURE по формуле _IOC; len включает reportId."""
    assert transport.hid_ioctl(get=False, length=64) == 0xC0404806
    assert transport.hid_ioctl(get=True, length=64) == 0xC0404807
    assert transport.hid_ioctl(get=False, length=9) == 0xC0094806
    assert transport.hid_ioctl(get=True, length=1) == 0xC0014807


def test_find_devices_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport, "_HIDRAW_SYSFS", str(tmp_path / "hidraw"))
    assert transport.find_dongle() is None
    assert transport.find_dock() is None


def test_find_dongle_unopenable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Узел обнаружен сканом, но открыть нельзя — TransportError с подсказкой.

    os.open мокается: реальный /dev/hidrawN может появиться в системе в
    любой момент, тест не должен зависеть от её состояния.
    """
    monkeypatch.setattr(transport, "_HIDRAW_SYSFS", str(tmp_path / "hidraw"))
    _make_uevent(tmp_path, "hidraw9", "0003:00003837:0000600A")

    def refuse(_path: str, _flags: int) -> int:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(transport.os, "open", refuse)
    with pytest.raises(transport.TransportError):
        transport.find_dongle()


def test_open_enodev_is_recoverable(monkeypatch: pytest.MonkeyPatch) -> None:
    """ENODEV при os.open (узел исчез между сканом и open) — восстановимый маркер.

    Тот же маркер «устройство отключено», что и на операциях живого fd:
    is_recoverable_transport отличит его и запустит авто-пере-скан.
    """

    def gone(_path: str, _flags: int) -> int:
        raise OSError(errno.ENODEV, "No such device")

    monkeypatch.setattr(transport.os, "open", gone)
    with pytest.raises(transport.TransportError) as excinfo:
        transport.HidrawTransport("/dev/hidraw0")
    assert "устройство отключено" in excinfo.value.message
    assert "переподключено" in excinfo.value.message
    assert "пере-скан" in excinfo.value.hint


def test_open_eio_keeps_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Не-ENODEV OSError при open сохраняет точный текст без маркера отключения."""

    def io_err(_path: str, _flags: int) -> int:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(transport.os, "open", io_err)
    with pytest.raises(transport.TransportError) as excinfo:
        transport.HidrawTransport("/dev/hidraw0")
    assert "не удалось открыть" in excinfo.value.message
    assert "устройство отключено" not in excinfo.value.message


def test_write_timeout_default_constant() -> None:
    """Константы бюджетов: запись 2 с, grace 4 с, close 5 с."""
    assert transport.WRITE_TIMEOUT_S == 2.0
    assert transport.WRITE_GRACE_S == 4.0
    assert transport.CLOSE_GRACE_S == 5.0


def test_write_late_success_resolves_outcome_before_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Запись дошла после бюджета: «не подтверждена», исход разрешён до возврата.

    Критерий LOCK-порядка: к моменту выхода из write_report фьючерс завершён
    (done выставлен внутри записи) — LOCK сессии уходит строго после финала
    записи.
    """
    monkeypatch.setattr(transport.os, "open", lambda *args, **kwargs: 42)
    closed: list[int] = []
    monkeypatch.setattr(transport.os, "close", lambda fd: closed.append(fd))
    finished = threading.Event()

    def slow(fd: int, data: bytes) -> int:
        time.sleep(0.2)  # дольше write_timeout_s, в пределах grace
        finished.set()
        return len(data)

    monkeypatch.setattr(transport.os, "write", slow)
    device = transport.HidrawTransport(
        "/dev/null", write_timeout_s=0.05, write_grace_s=1.0
    )
    with pytest.raises(transport.TransportError) as excinfo:
        device.write_report(0x55, b"\x01")
    writer = device._writer
    assert writer is not None and writer.daemon  # daemon: не держит выход процесса
    assert finished.is_set()  # исход записи разрешён ДО выхода из write_report
    assert "не подтверждена" in excinfo.value.message
    device.close()
    assert closed == [42]  # обычный close (всё дренировано) закрывает fd
    writer.join(2.0)
    assert not writer.is_alive()  # штатный close: писатель завершён стоп-токеном


def test_write_report_success_and_clean_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """Штатная запись и штатный close: результат на месте, писатель завершён."""
    monkeypatch.setattr(transport.os, "open", lambda *args, **kwargs: 42)
    written: list[bytes] = []
    closed: list[int] = []
    monkeypatch.setattr(transport.os, "close", lambda fd: closed.append(fd))

    def write(fd: int, data: bytes) -> int:
        written.append(data)
        return len(data)

    monkeypatch.setattr(transport.os, "write", write)
    device = transport.HidrawTransport("/dev/null")
    assert device.write_report(0x00, b"\xaa\x01") == 3  # reportId + 2 байта payload
    assert written == [bytes((0x00, 0xAA, 0x01))]  # reportId всегда первым байтом
    device.close()
    assert closed == [42]
    assert device._writer is None  # поток погашен стоп-токеном


def test_write_rejected_after_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """После close транспорт закрывающийся: новые записи отвергаются."""
    monkeypatch.setattr(transport.os, "open", lambda *args, **kwargs: 42)
    monkeypatch.setattr(transport.os, "close", lambda fd: None)

    def write(fd: int, data: bytes) -> int:
        return len(data)

    monkeypatch.setattr(transport.os, "write", write)
    device = transport.HidrawTransport("/dev/null")
    assert device.write_report(0x55, b"\x01") == 2
    device.close()  # штатный: запись дренирована, fd закрыт
    with pytest.raises(transport.TransportError) as excinfo:
        device.write_report(0x55, b"\x02")
    assert "закрывается" in excinfo.value.message


def test_close_rejects_write_while_draining(monkeypatch: pytest.MonkeyPatch) -> None:
    """Гонка close+write: при дренировании новая запись получает «закрывается».

    Close в фоне ждёт исход записи; probe-future гарантирует, что к моменту
    параллельной записи флаг _closing уже стоит — отказ детерминирован,
    окно «запись принята, но fd закрыт без дренирования» исключено.
    """
    monkeypatch.setattr(transport.os, "open", lambda *args, **kwargs: 42)
    monkeypatch.setattr(transport.os, "close", lambda fd: None)
    release = threading.Event()

    def stuck(fd: int, data: bytes) -> int:
        release.wait(5.0)
        return len(data)

    monkeypatch.setattr(transport.os, "write", stuck)
    device = transport.HidrawTransport("/dev/null", close_grace_s=0.15)
    first = device._submit_write(b"\x01")
    probe = _ResultProbe(first)
    device._last_write = probe  # close будет ждать через probe: сигнал входа
    closer = threading.Thread(target=device.close)
    closer.start()
    assert probe.entered_wait.wait(2.0)  # close в дренировании, _closing стоит
    with pytest.raises(transport.TransportError) as excinfo:
        device._submit_write(b"\x02")  # параллельная запись во время close
    assert "закрывается" in excinfo.value.message
    closer.join(5.0)
    assert device._poisoned is True  # close детерминирован: grace истёк → poison
    release.set()
    first.result(2.0)  # фоновая запись завершилась в не-закрытый fd


def test_write_unresolved_poisons_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Запись не разрешилась даже за grace: «не разрешена» + транспорт отравлен."""
    monkeypatch.setattr(transport.os, "open", lambda *args, **kwargs: 42)
    monkeypatch.setattr(transport.os, "close", lambda fd: None)
    release = threading.Event()

    def stuck(fd: int, data: bytes) -> int:
        release.wait(5.0)
        return len(data)

    monkeypatch.setattr(transport.os, "write", stuck)
    device = transport.HidrawTransport(
        "/dev/null", write_timeout_s=0.05, write_grace_s=0.1
    )
    with pytest.raises(transport.TransportError) as excinfo:
        device.write_report(0x55, b"\x01")
    assert "не разрешена" in excinfo.value.message
    with pytest.raises(transport.TransportError):
        device.read_report(5)  # отравлен: любые операции дают TransportError
    started = time.monotonic()
    device.close()  # отравленный close НЕ ждёт daemon-писателя
    elapsed = time.monotonic() - started
    assert elapsed < 1.0  # немедленно, без close-ожидания застрявшей записи
    release.set()  # daemon-писатель дозапишет в не-закрытый fd (докстринг close)


def test_close_grace_poisons_on_stuck_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close при застрявшей записи: grace ограничен, затем отравление без os.close."""
    monkeypatch.setattr(transport.os, "open", lambda *args, **kwargs: 42)
    closed: list[int] = []
    monkeypatch.setattr(transport.os, "close", lambda fd: closed.append(fd))
    release = threading.Event()

    def stuck(fd: int, data: bytes) -> int:
        release.wait(5.0)
        return len(data)

    monkeypatch.setattr(transport.os, "write", stuck)
    device = transport.HidrawTransport(
        "/dev/null", write_timeout_s=2.0, close_grace_s=0.1
    )
    future = device._submit_write(b"\x01")
    writer = device._writer
    assert writer is not None and writer.daemon  # писатель не блокирует выход
    device.close()
    assert device._poisoned is True  # транспорт отравлен
    assert closed == []  # fd сознательно не закрыт (утечка вместо блокировки)
    assert writer.is_alive()  # daemon-писатель не джойнился: жив до конца процесса
    release.set()
    future.result(2.0)  # фоновая запись завершилась


def test_read_report_eof_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустое чтение (EOF) — TransportError «устройство отключено», не None."""
    monkeypatch.setattr(transport.os, "open", lambda *args, **kwargs: 42)
    monkeypatch.setattr(transport.os, "close", lambda fd: None)
    monkeypatch.setattr(transport.select, "select", lambda *args: ([42], [], []))
    monkeypatch.setattr(transport.os, "read", lambda fd, size: b"")
    device = transport.HidrawTransport("/dev/null")
    with pytest.raises(transport.TransportError) as excinfo:
        device.read_report(10)
    assert "устройство отключено" in excinfo.value.message
    device.close()


def _enodev_device(monkeypatch: pytest.MonkeyPatch) -> transport.HidrawTransport:
    """Транспорт с замоканным fd: os.open/close подставлены, узел «жив»."""
    monkeypatch.setattr(transport.os, "open", lambda *args, **kwargs: 42)
    monkeypatch.setattr(transport.os, "close", lambda fd: None)
    return transport.HidrawTransport("/dev/null")


def test_write_report_enodev_is_recoverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ENODEV (Errno 19) на записи — «устройство отключено», hint про пере-скан.

    Узел hidraw пере-перечислился: маркер в сообщении позволяет
    is_recoverable_transport запустить авто-пере-скан (ретрай с новым fd).
    """
    device = _enodev_device(monkeypatch)

    def gone(fd: int, data: bytes) -> int:
        raise OSError(errno.ENODEV, "No such device")

    monkeypatch.setattr(transport.os, "write", gone)
    with pytest.raises(transport.TransportError) as excinfo:
        device.write_report(0x55, b"\x01")
    assert "устройство отключено" in excinfo.value.message
    assert "переподключено" in excinfo.value.message
    assert "пере-скан" in excinfo.value.hint
    device.close()


def test_write_late_enodev_is_recoverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ENODEV, дошедший после бюджета записи (future-ветка) — тот же маркер."""
    monkeypatch.setattr(transport.os, "open", lambda *args, **kwargs: 42)
    monkeypatch.setattr(transport.os, "close", lambda fd: None)

    def late_gone(fd: int, data: bytes) -> int:
        time.sleep(0.2)  # дольше write_timeout_s, в пределах grace
        raise OSError(errno.ENODEV, "No such device")

    monkeypatch.setattr(transport.os, "write", late_gone)
    device = transport.HidrawTransport(
        "/dev/null", write_timeout_s=0.05, write_grace_s=1.0
    )
    with pytest.raises(transport.TransportError) as excinfo:
        device.write_report(0x55, b"\x01")
    assert "устройство отключено" in excinfo.value.message
    assert "пере-скан" in excinfo.value.hint
    device.close()


def test_write_report_eio_keeps_plain_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не-ENODEV OSError сохраняет точный текст «ошибка записи в …» без маркера."""
    device = _enodev_device(monkeypatch)

    def io_err(fd: int, data: bytes) -> int:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(transport.os, "write", io_err)
    with pytest.raises(transport.TransportError) as excinfo:
        device.write_report(0x55, b"\x01")
    assert "ошибка записи в" in excinfo.value.message
    assert "устройство отключено" not in excinfo.value.message
    device.close()


@pytest.mark.parametrize("stage", ["select", "read"])
def test_read_report_enodev_is_recoverable(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """ENODEV при ожидании или чтении — «устройство отключено», hint про пере-скан."""
    device = _enodev_device(monkeypatch)
    enodev = OSError(errno.ENODEV, "No such device")
    if stage == "select":

        def select_fail(*args: object) -> list[list[int]]:
            raise enodev

        monkeypatch.setattr(transport.select, "select", select_fail)
    else:
        monkeypatch.setattr(transport.select, "select", lambda *args: ([42], [], []))

        def read_fail(fd: int, size: int) -> bytes:
            raise enodev

        monkeypatch.setattr(transport.os, "read", read_fail)
    with pytest.raises(transport.TransportError) as excinfo:
        device.read_report(10)
    assert "устройство отключено" in excinfo.value.message
    assert "пере-скан" in excinfo.value.hint
    device.close()


@pytest.mark.parametrize("mode", ["send", "get"])
def test_feature_enodev_is_recoverable(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """ENODEV на feature-ioctl — «устройство отключено», hint про пере-скан."""
    device = _enodev_device(monkeypatch)

    def gone(fd: int, request: int, buf: bytearray) -> int:
        raise OSError(errno.ENODEV, "No such device")

    monkeypatch.setattr(transport.fcntl, "ioctl", gone)
    with pytest.raises(transport.TransportError) as excinfo:
        if mode == "send":
            device.send_feature(0xAA, b"\x01\x01")
        else:
            device.get_feature(0xAA, 3)
    assert "устройство отключено" in excinfo.value.message
    assert "пере-скан" in excinfo.value.hint
    device.close()
