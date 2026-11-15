"""Тесты сервисного слоя на FakeTransport."""

from __future__ import annotations

import threading
import time

import pytest
from conftest import FakeTransport

from mchose_hub import protocol as proto
from mchose_hub.service import DRAIN_TIMEOUT_MS, DockService, HeadsetService
from mchose_hub.transport import ETIMEDOUT_HEADSET_HINT, TransportError


def headset_frame(*body: int) -> bytes:
    """Кадр вида hidraw-чтения: reportId 0x55 + 63 байта данных."""
    payload = bytes(body)
    payload += bytes(proto.HEADSET_FRAME_SIZE - len(payload))
    return bytes((proto.HEADSET_REPORT_ID,)) + payload


def dock_response(command: int, params: bytes = b"") -> bytes:
    frame = proto.build_dock_frame(
        command, params, message_type=proto.DOCK_MSG_RESPONSE
    )
    return bytes((proto.DOCK_REPORT_ID,)) + frame


def test_set_audio_mode_readback(fake_transport: FakeTransport) -> None:
    fake_transport.queue_response(
        headset_frame(proto.HEADSET_REPORT_ID, 0x11, int(proto.AudioMode.MUSIC))
    )
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    assert service.set_audio_mode(proto.AudioMode.MUSIC) == proto.AudioMode.MUSIC
    assert fake_transport.written == [
        (proto.HEADSET_REPORT_ID, proto.build_set_audio_mode(proto.AudioMode.MUSIC)),
        (proto.HEADSET_REPORT_ID, proto.build_audio_mode_query()),
    ]
    assert len(fake_transport.reads) == 1


def test_set_audio_mode_timeout(fake_transport: FakeTransport) -> None:
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    assert service.set_audio_mode(proto.AudioMode.GAME_2) is None


def test_poll_status_success(fake_transport: FakeTransport) -> None:
    fake_transport.queue_response(
        headset_frame(proto.HEADSET_REPORT_ID, 0x65, 77, proto.BATTERY_STATUS_CHARGING)
    )
    fake_transport.queue_response(
        headset_frame(proto.HEADSET_REPORT_ID, 0x11, int(proto.AudioMode.GAME_1))
    )
    status = HeadsetService(fake_transport, settle_delay_s=0.0).poll_status()
    assert status == (
        proto.BatteryState(percent=77, charging=True, full=False, asleep=False),
        proto.AudioMode.GAME_1,
    )


def test_poll_status_battery_timeout(fake_transport: FakeTransport) -> None:
    assert HeadsetService(fake_transport).poll_status() is None


def test_poll_status_mode_timeout(fake_transport: FakeTransport) -> None:
    """Батарея ответила, режим молчит: (батарея, None), а не None целиком."""
    fake_transport.queue_response(
        headset_frame(proto.HEADSET_REPORT_ID, 0x65, 50, proto.BATTERY_STATUS_DISCHARGE)
    )
    battery, mode = HeadsetService(fake_transport, settle_delay_s=0.0).poll_status()
    assert battery == proto.BatteryState(
        percent=50, charging=False, full=False, asleep=False
    )
    assert mode is None


def test_poll_battery_and_mode_split(fake_transport: FakeTransport) -> None:
    """Частичный ответ: батарея есть, режим молчит."""
    fake_transport.queue_response(
        headset_frame(proto.HEADSET_REPORT_ID, 0x65, 64, proto.BATTERY_STATUS_FULL)
    )
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    assert service.poll_battery() == proto.BatteryState(
        percent=64, charging=False, full=True, asleep=False
    )
    assert service.poll_audio_mode() is None
    battery, mode = service.poll_status()
    assert (battery, mode) == (
        proto.BatteryState(percent=64, charging=False, full=True, asleep=False),
        None,
    )


def test_poll_audio_mode_only(fake_transport: FakeTransport) -> None:
    """Частичный ответ: режим есть, батарея молчит."""
    fake_transport.queue_response(
        headset_frame(proto.HEADSET_REPORT_ID, 0x11, int(proto.AudioMode.MUSIC))
    )
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    assert service.poll_audio_mode() == proto.AudioMode.MUSIC
    assert service.poll_battery() is None
    battery, mode = service.poll_status()
    assert (battery, mode) == (None, proto.AudioMode.MUSIC)


def test_poll_battery_timeout(fake_transport: FakeTransport) -> None:
    assert HeadsetService(fake_transport).poll_battery() is None
    assert HeadsetService(fake_transport).poll_audio_mode() is None


def test_set_settings_reads_accept(fake_transport: FakeTransport) -> None:
    """set-подкоманды потребляют accept-кадр и возвращают True."""
    fake_transport.queue_response(headset_frame(0x55, 0x22, 0))
    fake_transport.queue_response(headset_frame(0x55, 0x22, 0))
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    assert service.set_auto_shutdown(15) is True
    assert service.set_prompt_volume(10) is True
    assert len(fake_transport.reads) == 2


def test_set_commands_without_accept_return_false(
    fake_transport: FakeTransport,
) -> None:
    """Тишина вместо accept-кадра — set-команды сообщают False."""
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    assert service.set_auto_shutdown(15) is False
    assert service.set_prompt_volume(5) is False
    assert service.factory_reset() is False


def test_set_commands_with_foreign_frame_return_false(
    fake_transport: FakeTransport,
) -> None:
    """Кадр без заголовка [0x55,0x22] подтверждением не считается."""
    fake_transport.queue_response(headset_frame(0x66, 0x22, 0))
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    assert service.set_auto_shutdown(15) is False
    assert service.factory_reset() is False


def test_auto_shutdown_set_and_get(fake_transport: FakeTransport) -> None:
    fake_transport.queue_response(headset_frame(0x55, 0x22, 1))  # accept на set
    fake_transport.queue_response(headset_frame(0x55, 0x22, 4))  # read: 4 -> 30 мин
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    service.set_auto_shutdown(30)
    assert service.get_auto_shutdown() == 30
    assert fake_transport.written == [
        (
            proto.HEADSET_REPORT_ID,
            proto.build_settings(proto.SETTINGS_SUB_SET_AUTO_SHUTDOWN, 4),
        ),
        (
            proto.HEADSET_REPORT_ID,
            proto.build_settings(proto.SETTINGS_SUB_READ_AUTO_SHUTDOWN),
        ),
    ]


def test_auto_shutdown_invalid_value(fake_transport: FakeTransport) -> None:
    with pytest.raises(ValueError):
        HeadsetService(fake_transport).set_auto_shutdown(60)
    assert fake_transport.written == []


def test_prompt_volume_set_and_get(fake_transport: FakeTransport) -> None:
    fake_transport.queue_response(headset_frame(0x55, 0x22, 7))
    fake_transport.queue_response(headset_frame(0x55, 0x22, 7))
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    service.set_prompt_volume(7)
    assert service.get_prompt_volume() == 7


@pytest.mark.parametrize("level", [-1, 16])
def test_prompt_volume_invalid(fake_transport: FakeTransport, level: int) -> None:
    with pytest.raises(ValueError):
        HeadsetService(fake_transport).set_prompt_volume(level)


def test_factory_reset_consumes_accept(fake_transport: FakeTransport) -> None:
    """factory_reset: write -> read(accept); канал открывает транспорт."""
    fake_transport.queue_response(headset_frame(0x55, 0x22, 0))
    assert HeadsetService(fake_transport).factory_reset() is True
    assert fake_transport.written == [
        (
            proto.HEADSET_REPORT_ID,
            proto.build_settings(proto.SETTINGS_SUB_FACTORY_RESET),
        )
    ]
    assert fake_transport.reads == [DRAIN_TIMEOUT_MS]
    assert fake_transport.events == ["write", "read"]


def test_command_session_order(fake_transport: FakeTransport) -> None:
    """Команда наушников: write -> read; канал открывает транспорт (§7)."""
    fake_transport.queue_response(
        headset_frame(proto.HEADSET_REPORT_ID, 0x65, 66, proto.BATTERY_STATUS_DISCHARGE)
    )
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    state = service.poll_battery()
    assert state == proto.BatteryState(
        percent=66, charging=False, full=False, asleep=False
    )
    assert fake_transport.events == ["write", "read"]
    assert fake_transport.features == []  # канал открывает транспорт, не команды


def test_session_propagates_write_error(fake_transport: FakeTransport) -> None:
    """ETIMEDOUT записи пробрасывается наружу (lock-обёртки больше нет)."""
    service = HeadsetService(fake_transport, settle_delay_s=0.0)

    def stalled_write(report_id: int, payload: bytes) -> int:
        fake_transport.written.append((report_id, payload))
        fake_transport.events.append("write")
        raise TransportError(
            "ошибка записи в /dev/hidraw9: [Errno 110] Connection timed out",
            ETIMEDOUT_HEADSET_HINT,
        )

    fake_transport.write_report = stalled_write  # type: ignore[method-assign]
    with pytest.raises(TransportError):
        service.poll_battery()
    assert fake_transport.events == ["write"]


def test_set_audio_mode_session_order(fake_transport: FakeTransport) -> None:
    """set_audio_mode: одна сессия на write + settle + query + read."""
    fake_transport.queue_response(
        headset_frame(proto.HEADSET_REPORT_ID, 0x11, int(proto.AudioMode.GAME_1))
    )
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    assert service.set_audio_mode(proto.AudioMode.GAME_1) == proto.AudioMode.GAME_1
    assert fake_transport.events == ["write", "write", "read"]
    assert fake_transport.features == []  # канал открывает транспорт, не команды


def test_dock_set_light_switch_frame(fake_transport: FakeTransport) -> None:
    DockService(fake_transport).set_light_switch(True)
    report_id, frame = fake_transport.written[0]
    assert report_id == proto.DOCK_REPORT_ID
    assert frame[:7] == bytes((0xAA, proto.DOCK_CMD_SET_LIGHT_SWITCH, 0, 0, 0, 1, 1))


def test_dock_read_global_info(fake_transport: FakeTransport) -> None:
    params = bytearray(proto.DOCK_FRAME_SIZE)
    params[0] = 1
    params[1] = 4
    params[2] = 6
    params[3] = 2
    params[4] = 70
    params[5] = 1
    params[6:9] = bytes((0x1E, 0x28, 0x32))
    params[9] = 1
    params[10] = 45
    params[12] = 2
    params[13] = 65
    params[29] = 1
    fake_transport.queue_response(
        dock_response(proto.DOCK_CMD_READ_GLOBAL_INFO, bytes(params))
    )
    info = DockService(fake_transport).read_global_info()
    assert info == proto.DockLighting(
        enabled=True,
        mode=4,
        mode_count=6,
        speed=2,
        brightness=70,
        light_sync=True,
        base_color=(30, 40, 50),
        direction=1,
        sleep_enabled=True,
        sleep_time=45,
        version="065002",
    )
    report_id, frame = fake_transport.written[0]
    assert report_id == proto.DOCK_REPORT_ID
    assert frame[1] == proto.DOCK_CMD_READ_GLOBAL_INFO


def test_dock_read_global_info_wrong_command(fake_transport: FakeTransport) -> None:
    fake_transport.queue_response(
        dock_response(proto.DOCK_CMD_READ_LIGHT_INFO, bytes((1, 0, 5, 1, 40, 0)))
    )
    assert DockService(fake_transport).read_global_info() is None


def test_dock_set_global_info_frame(fake_transport: FakeTransport) -> None:
    lighting = proto.DockLighting(
        enabled=True,
        mode=1,
        mode_count=6,
        speed=3,
        brightness=55,
        light_sync=True,
        base_color=(255, 160, 0),
        direction=1,
    )
    DockService(fake_transport).set_global_info(lighting)
    report_id, frame = fake_transport.written[0]
    assert report_id == proto.DOCK_REPORT_ID
    assert frame[:6] == bytes((0xAA, proto.DOCK_CMD_SET_GLOBAL_INFO, 0, 0, 0, 10))
    assert frame[6:16] == bytes((1, 1, 6, 3, 55, 1, 255, 160, 0, 1))


def _global_info_params(direction: int) -> bytes:
    """Снимок global_info для read-ответа; direction — params[29]."""
    params = bytearray(proto.DOCK_FRAME_SIZE)
    params[0] = 1
    params[1] = 4
    params[2] = 6
    params[3] = 2
    params[4] = 70
    params[5] = 1
    params[6:9] = bytes((0x1E, 0x28, 0x32))
    params[9] = 1
    params[10] = 45
    params[12] = 2
    params[13] = 65
    params[29] = direction
    return bytes(params)


def test_dock_set_direction_read_modify_write(
    fake_transport: FakeTransport,
) -> None:
    """set_direction: read → set в одной сессии; direction применён к свежему снимку."""
    fake_transport.queue_response(
        dock_response(proto.DOCK_CMD_READ_GLOBAL_INFO, _global_info_params(direction=0))
    )
    service = DockService(fake_transport)
    assert service.set_direction(1) is True
    assert [frame[1] for _, frame in fake_transport.written] == [
        proto.DOCK_CMD_READ_GLOBAL_INFO,
        proto.DOCK_CMD_SET_GLOBAL_INFO,
    ]
    # Set-кадр: direction — байт 15, остальной снимок переносится без изменений.
    _, set_frame = fake_transport.written[1]
    assert set_frame[15] == 1
    assert set_frame[6:15] == bytes((1, 4, 6, 2, 70, 1, 0x1E, 0x28, 0x32))


def test_dock_set_direction_silent_read_returns_false(
    fake_transport: FakeTransport,
) -> None:
    """Устройство молчит на чтение: set_direction → False, set-записи нет."""
    assert DockService(fake_transport).set_direction(1) is False
    assert [frame[1] for _, frame in fake_transport.written] == [
        proto.DOCK_CMD_READ_GLOBAL_INFO
    ]


def test_dock_read_device_info(fake_transport: FakeTransport) -> None:
    fake_transport.queue_response(
        dock_response(proto.DOCK_CMD_READ_DEVICE_VERSION, bytes((3, 0, 1)))
    )
    fake_transport.queue_response(
        dock_response(proto.DOCK_CMD_READ_DEVICE_NAME, b"MCHOSE DOCK\x00\x00")
    )
    info = DockService(fake_transport).read_device_info()
    assert info == {"version": "3.0.1", "name": "MCHOSE DOCK"}


def test_dock_read_device_info_timeout(fake_transport: FakeTransport) -> None:
    assert DockService(fake_transport).read_device_info() is None


def test_dock_setters_use_expected_commands(fake_transport: FakeTransport) -> None:
    service = DockService(fake_transport)
    service.set_light_mode(2)
    service.set_light_brightness(2, 80)
    service.set_light_speed(5)
    service.set_base_color((1, 2, 3))
    service.set_sleep(True, 30)
    service.reset()
    commands = [frame[1] for _, frame in fake_transport.written]
    assert commands == [
        proto.DOCK_CMD_SET_LIGHT_MODE,
        proto.DOCK_CMD_SET_LIGHT_BRIGHTNESS,
        proto.DOCK_CMD_SET_LIGHT_SPEED,
        proto.DOCK_CMD_SET_BASE_COLOR,
        proto.DOCK_CMD_SET_SLEEP_INFO,
        proto.DOCK_CMD_RESET_DOCK,
    ]
    assert all(
        report_id == proto.DOCK_REPORT_ID for report_id, _ in fake_transport.written
    )


# --- Регрессия двойного reportId: точные байты на проводе (как в usbmon) ---
# Баг 0.3.0: строители клали reportId в payload, транспорт добавлял ещё раз —
# на проводе уходило «55 55 65 …» / «aa aa 01 …» вместо эталона Windows
# «55 65 …» / «aa 01 …». FakeTransport.wire фиксирует bytes([id]) + payload.


def _pad64(*head: int) -> bytes:
    """Кадр на проводе: header + нулевой паддинг до 64 байт."""
    return bytes(head) + bytes(64 - len(head))


def test_wire_battery_session_exact_bytes(fake_transport: FakeTransport) -> None:
    """Сессия опроса батареи: aa 01 01 / 55 65 01 / aa 01 00, все по 64 байта."""
    fake_transport.queue_response(
        headset_frame(0x55, 0x65, 87, proto.BATTERY_STATUS_CHARGING)
    )
    state = HeadsetService(fake_transport, settle_delay_s=0.0).poll_battery()
    assert state is not None
    assert fake_transport.wire == [
        _pad64(
            0x55, 0x65, 0x01
        ),  # запрос батареи — ID один раз; канал открыт транспортом
    ]
    assert all(len(frame) == 64 for frame in fake_transport.wire)
    assert not [
        f for f in fake_transport.wire if f[0] == 0xAA
    ]  # 0xAA на провод не уходит


def test_wire_audio_mode_exact_bytes(fake_transport: FakeTransport) -> None:
    """Установка режима: 55 21 01 <mode> и запрос 55 11 — без дубля ID."""
    fake_transport.queue_response(headset_frame(0x55, 0x11, int(proto.AudioMode.MUSIC)))
    mode = HeadsetService(fake_transport, settle_delay_s=0.0).set_audio_mode(
        proto.AudioMode.MUSIC
    )
    assert mode == proto.AudioMode.MUSIC
    assert fake_transport.wire == [
        _pad64(0x55, 0x21, 0x01, int(proto.AudioMode.MUSIC)),
        _pad64(0x55, 0x11),
    ]


def test_wire_settings_exact_bytes(fake_transport: FakeTransport) -> None:
    """set-подкоманда настроек: 55 22 <sub> <val> — ID ровно один."""
    fake_transport.queue_response(headset_frame(0x55, 0x22, 0))
    assert HeadsetService(fake_transport, settle_delay_s=0.0).set_auto_shutdown(30)
    assert fake_transport.wire == [
        _pad64(0x55, 0x22, 0x00, 0x04),  # sub 0: 30 мин -> value 4
    ]


def test_wire_dock_bytes_unchanged(fake_transport: FakeTransport) -> None:
    """Путь дока не задет фиксом: 0x00 + кадр с START 0xAA, байты как были."""
    DockService(fake_transport).set_light_switch(True)
    DockService(fake_transport).read_global_info()
    assert fake_transport.wire == [
        _pad64(0x00, 0xAA, proto.DOCK_CMD_SET_LIGHT_SWITCH, 0, 0, 0, 1, 1),
        _pad64(0x00, 0xAA, proto.DOCK_CMD_READ_GLOBAL_INFO, 0, 0, 0, 0),
    ]


# --- Пассивный приём: push-кадры донгла обновляют кэш (захват M HUB) ---


def _push(*body: int) -> bytes:
    """Пуш-кадр вида hidraw-чтения: reportId + payload, 64 байта."""
    return headset_frame(*body)


def test_passive_pushes_update_cache(fake_transport: FakeTransport) -> None:
    """Пуш-кадры из захвата: 55 11 02 / 55 22 04 0a / 55 65 64 02 / 55 22 02 00."""
    fake_transport.queue_response(_push(0x55, 0x11, 0x02))
    fake_transport.queue_response(_push(0x55, 0x22, 0x04, 0x0A))
    fake_transport.queue_response(_push(0x55, 0x65, 0x64, 0x02))
    fake_transport.queue_response(_push(0x55, 0x22, 0x02, 0x00))
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    assert service.pump_passive() == 4
    assert service.cached_audio_mode() == proto.AudioMode.MUSIC
    assert service.cached_battery() == proto.BatteryState(
        percent=100, charging=False, full=False, asleep=False
    )
    assert service.cached_prompt_volume() == 10
    assert service.cached_auto_shutdown() == 45  # value 0 -> 45 минут
    assert fake_transport.written == []  # приём пассивный: записей нет
    assert fake_transport.reads == [10, 0, 0, 0, 0]  # 4 кадра + финальный drain-тик


def test_poll_falls_back_to_cache(fake_transport: FakeTransport) -> None:
    """Нет ответа на явный запрос — GUI-методы отдают последнее из кэша."""
    fake_transport.queue_response(_push(0x55, 0x65, 0x50, proto.BATTERY_STATUS_FULL))
    fake_transport.queue_response(_push(0x55, 0x11, int(proto.AudioMode.GAME_2)))
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    service.pump_passive()
    # ответы кончились: явные запросы молчат
    assert service.poll_battery() == proto.BatteryState(
        percent=80, charging=False, full=True, asleep=False
    )
    assert service.poll_audio_mode() == proto.AudioMode.GAME_2
    battery, mode = service.poll_status()
    assert (battery, mode) == (
        proto.BatteryState(percent=80, charging=False, full=True, asleep=False),
        proto.AudioMode.GAME_2,
    )


def test_poll_status_none_when_cache_empty(fake_transport: FakeTransport) -> None:
    """Кэш пуст и опрос молчит — poll_status None (как раньше)."""
    assert HeadsetService(fake_transport, settle_delay_s=0.0).poll_status() is None


def test_query_ingests_push_frame_while_waiting(fake_transport: FakeTransport) -> None:
    """Пуш вместо ответа: кэш обновился, poll вернул кэш режима."""
    fake_transport.queue_response(
        _push(0x55, 0x65, 0x2D, proto.BATTERY_STATUS_CHARGING)
    )
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    assert service.poll_audio_mode() is None  # кадр не про режим, кэша нет
    assert service.cached_battery() == proto.BatteryState(
        percent=45, charging=True, full=False, asleep=False
    )


def test_push_of_unknown_command_ignored(fake_transport: FakeTransport) -> None:
    """Пуш чужой команды (66 65 ...) кэш не трогает."""
    fake_transport.queue_response(_push(0x66, 0x65, 99, 2))
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    assert service.pump_passive() == 1
    assert service.cached_battery() is None


# --- pump_passive держит блокировку сервиса (не крадёт ответ команды) ---


def test_pump_passive_holds_lock_against_command(
    fake_transport: FakeTransport,
) -> None:
    """Команда и pump_passive одновременно: команда получает СВОЙ ответ.

    pump «висит внутри чтения» (slow_read), команда стартует и ждёт
    на сервисной блокировке: пока lock удержан, записи команды нет;
    после отпускания команда уходит первой и читает свой ответ.
    """
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    entered = threading.Event()
    release = threading.Event()
    original_read = fake_transport.read_report
    calls = {"n": 0}

    def slow_read(timeout_ms: int) -> bytes | None:
        calls["n"] += 1
        if calls["n"] > 1:
            return original_read(timeout_ms)  # чтению команды — честный FIFO
        entered.set()
        release.wait(5)  # pump «читает» — всё это время lock удержан
        return None  # кадр команде не отдаём: он ждёт её чтение

    fake_transport.read_report = slow_read  # type: ignore[method-assign]

    pump = threading.Thread(target=lambda: service.pump_passive(timeout_ms=50))
    pump.start()
    assert entered.wait(2)  # pump внутри чтения

    answers: list[proto.BatteryState | None] = []

    def command() -> None:
        answers.append(service.poll_battery())

    cmd = threading.Thread(target=command)
    cmd.start()
    time.sleep(0.15)  # команда дошла до сервисной блокировки и ждёт
    # Пока lock удержан pump'ом: команда не сделала ни записи, ни чтения.
    assert fake_transport.written == []
    assert fake_transport.reads == []

    fake_transport.queue_response(
        headset_frame(proto.HEADSET_REPORT_ID, 0x65, 66, proto.BATTERY_STATUS_DISCHARGE)
    )
    release.set()
    pump.join(5)
    cmd.join(5)

    assert answers == [
        proto.BatteryState(percent=66, charging=False, full=False, asleep=False)
    ]
    # Порядок: чтение pump (в lock, через хук) -> write -> read команды.
    assert fake_transport.events == ["write", "read"]
    assert fake_transport.reads == [800]  # чтение команды со своим таймаутом


# --- frames_received: признак живого канала для GUI ---


def test_frames_received_counts_answers_and_pushes(
    fake_transport: FakeTransport,
) -> None:
    """Счётчик растёт и от пушей (pump), и от свежих ответов (запрос)."""
    fake_transport.queue_response(headset_frame(0x55, 0x11, 0x02))  # пуш режима
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    assert service.frames_received() == 0
    assert service.pump_passive() == 1
    assert service.frames_received() == 1
    fake_transport.queue_response(
        headset_frame(proto.HEADSET_REPORT_ID, 0x65, 66, proto.BATTERY_STATUS_DISCHARGE)
    )
    service.poll_battery()  # свежий ответ на явный запрос
    assert service.frames_received() == 2
