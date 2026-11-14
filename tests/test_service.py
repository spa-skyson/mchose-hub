"""Тесты сервисного слоя на FakeTransport."""

from __future__ import annotations

import pytest
from conftest import FakeTransport

from mchose_hub import protocol as proto
from mchose_hub.service import DockService, HeadsetService


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


def test_factory_reset_no_wait(fake_transport: FakeTransport) -> None:
    HeadsetService(fake_transport).factory_reset()
    assert fake_transport.written == [
        (
            proto.HEADSET_REPORT_ID,
            proto.build_settings(proto.SETTINGS_SUB_FACTORY_RESET),
        )
    ]
    assert fake_transport.reads == []


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
