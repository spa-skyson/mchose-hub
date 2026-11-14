"""Тесты сервисного слоя на FakeTransport."""

from __future__ import annotations

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
    fake_transport.queue_response(
        headset_frame(proto.HEADSET_REPORT_ID, 0x65, 50, proto.BATTERY_STATUS_DISCHARGE)
    )
    assert HeadsetService(fake_transport, settle_delay_s=0.0).poll_status() is None


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
    assert service.poll_status() is None


def test_poll_audio_mode_only(fake_transport: FakeTransport) -> None:
    """Частичный ответ: режим есть, батарея молчит."""
    fake_transport.queue_response(
        headset_frame(proto.HEADSET_REPORT_ID, 0x11, int(proto.AudioMode.MUSIC))
    )
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    assert service.poll_audio_mode() == proto.AudioMode.MUSIC
    assert service.poll_battery() is None
    assert service.poll_status() is None


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
    """factory_reset в сессии канала: unlock -> write -> read(accept) -> lock."""
    fake_transport.queue_response(headset_frame(0x55, 0x22, 0))
    assert HeadsetService(fake_transport).factory_reset() is True
    assert fake_transport.written == [
        (
            proto.HEADSET_REPORT_ID,
            proto.build_settings(proto.SETTINGS_SUB_FACTORY_RESET),
        )
    ]
    assert fake_transport.reads == [DRAIN_TIMEOUT_MS]
    assert fake_transport.events == ["unlock", "write", "read", "lock"]


def test_command_session_order(fake_transport: FakeTransport) -> None:
    """Каждая команда наушников: unlock -> write -> read -> lock (§7)."""
    fake_transport.queue_response(
        headset_frame(proto.HEADSET_REPORT_ID, 0x65, 66, proto.BATTERY_STATUS_DISCHARGE)
    )
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    state = service.poll_battery()
    assert state == proto.BatteryState(
        percent=66, charging=False, full=False, asleep=False
    )
    assert fake_transport.events == ["unlock", "write", "read", "lock"]
    assert fake_transport.features == [
        (proto.FEATURE_AA_REPORT_ID, proto.build_channel_open()),
        (proto.FEATURE_AA_REPORT_ID, proto.build_channel_close()),
    ]


def test_session_locks_on_transport_error(fake_transport: FakeTransport) -> None:
    """ETIMEDOUT записи пробрасывается, но LOCK выполняется (finally)."""
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
    assert fake_transport.events == ["unlock", "write", "lock"]


def test_set_audio_mode_session_order(fake_transport: FakeTransport) -> None:
    """set_audio_mode: одна сессия на write + settle + query + read."""
    fake_transport.queue_response(
        headset_frame(proto.HEADSET_REPORT_ID, 0x11, int(proto.AudioMode.GAME_1))
    )
    service = HeadsetService(fake_transport, settle_delay_s=0.0)
    assert service.set_audio_mode(proto.AudioMode.GAME_1) == proto.AudioMode.GAME_1
    assert fake_transport.events == [
        "unlock",
        "write",
        "write",
        "read",
        "lock",
    ]
    assert fake_transport.features == [
        (proto.FEATURE_AA_REPORT_ID, proto.build_channel_open()),
        (proto.FEATURE_AA_REPORT_ID, proto.build_channel_close()),
    ]


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
