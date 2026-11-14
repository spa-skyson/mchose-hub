"""Тесты чистого протокольного слоя."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from mchose_hub import protocol as proto


def test_build_battery_query_layout() -> None:
    frame = proto.build_battery_query()
    assert len(frame) == proto.HEADSET_FRAME_SIZE
    assert frame[:3] == bytes((proto.HEADSET_REPORT_ID, 0x65, 0x01))
    assert frame[3:] == bytes(proto.HEADSET_FRAME_SIZE - 3)


@pytest.mark.parametrize(
    ("status", "charging", "full", "asleep"),
    [
        (proto.BATTERY_STATUS_DISCHARGE, False, False, False),
        (proto.BATTERY_STATUS_CHARGING, True, False, False),
        (proto.BATTERY_STATUS_FULL, False, True, False),
        (proto.BATTERY_STATUS_ASLEEP, False, False, True),
    ],
)
def test_parse_battery_statuses(
    status: int, charging: bool, full: bool, asleep: bool
) -> None:
    payload = bytes((proto.HEADSET_REPORT_ID, 0x65, 87, status))
    payload += bytes(proto.HEADSET_FRAME_SIZE - len(payload))
    state = proto.parse_battery(payload)
    assert state == proto.BatteryState(
        percent=87, charging=charging, full=full, asleep=asleep
    )


def test_parse_battery_garbage() -> None:
    assert proto.parse_battery(b"") is None
    assert proto.parse_battery(bytes(proto.HEADSET_FRAME_SIZE)) is None
    assert proto.parse_battery(b"\x66\x65\x50\x02" + bytes(59)) is None
    assert proto.parse_battery(bytes((0x55, 0x65, 200, 2)) + bytes(59)) is None


def test_parse_battery_stripped_form() -> None:
    # Допустим буфер и без ведущего маркера 0x55 (после снятия reportId).
    state = proto.parse_battery(bytes((0x65, 42, proto.BATTERY_STATUS_FULL)))
    assert state == proto.BatteryState(
        percent=42, charging=False, full=True, asleep=False
    )


def test_audio_mode_query_and_parse() -> None:
    query = proto.build_audio_mode_query()
    assert query[:2] == b"\x55\x11"
    assert len(query) == proto.HEADSET_FRAME_SIZE
    assert not any(query[2:])
    for mode in proto.AudioMode:
        payload = bytes((proto.HEADSET_REPORT_ID, 0x11, int(mode))) + bytes(60)
        assert proto.parse_audio_mode(payload) == int(mode)
    assert proto.parse_audio_mode(bytes((0x55, 0x11, 7)) + bytes(60)) is None
    assert proto.parse_audio_mode(bytes(proto.HEADSET_FRAME_SIZE)) is None


def test_build_set_audio_mode_frame() -> None:
    frame = proto.build_set_audio_mode(proto.AudioMode.MUSIC)
    assert frame[:4] == bytes((0x55, 0x21, 0x01, int(proto.AudioMode.MUSIC)))
    assert len(frame) == proto.HEADSET_FRAME_SIZE


def test_settings_build_and_parse() -> None:
    set_frame = proto.build_settings(proto.SETTINGS_SUB_SET_AUTO_SHUTDOWN, 1)
    assert set_frame[:4] == bytes((0x55, 0x22, 0, 1))
    read_frame = proto.build_settings(proto.SETTINGS_SUB_READ_PROMPT_VOLUME)
    assert read_frame[:3] == bytes((0x55, 0x22, 4))
    assert len(read_frame) == proto.HEADSET_FRAME_SIZE

    response = bytes((0x55, 0x22, 4)) + bytes(60)
    assert proto.parse_settings_response(response, expect_value=True) == 4
    assert proto.parse_settings_response(response, expect_value=False) == 0
    assert proto.parse_settings_response(bytes((0x55, 0x22)), expect_value=True) is None
    assert proto.parse_settings_response(bytes((0x55, 0x22)), expect_value=False) == 0
    assert proto.parse_settings_response(bytes(8), expect_value=True) is None
    assert proto.parse_settings_response(bytes(8), expect_value=False) is None
    assert proto.parse_settings_response(bytes((0x66, 0x22, 4)), False) is None


def test_channel_feature_frames() -> None:
    """SET_FEATURE(0xAA): unlock/lock кадры по PROTOCOL.md §7."""
    open_frame = proto.build_channel_open()
    close_frame = proto.build_channel_close()
    assert proto.FEATURE_AA_REPORT_ID == 0xAA
    assert proto.FEATURE_AA_SIZE == 63
    assert len(open_frame) == proto.FEATURE_AA_SIZE
    assert len(close_frame) == proto.FEATURE_AA_SIZE
    assert open_frame[:3] == b"\xaa\x01\x01"
    assert close_frame[:3] == b"\xaa\x01\x00"
    assert open_frame[3:] == bytes(proto.FEATURE_AA_SIZE - 3)
    assert close_frame[3:] == bytes(proto.FEATURE_AA_SIZE - 3)


def test_auto_shutdown_maps() -> None:
    assert proto.AUTO_SHUTDOWN_MINUTES_TO_VALUE == {15: 1, 30: 4, 45: 0}
    for minutes, value in proto.AUTO_SHUTDOWN_MINUTES_TO_VALUE.items():
        assert proto.auto_shutdown_value_to_minutes(value) == minutes
    assert proto.auto_shutdown_value_to_minutes(7) is None


def test_prompt_volume_constants() -> None:
    assert proto.PROMPT_VOLUME_MAX == 15
    assert proto.PROMPT_VOLUME_DEFAULT == 10


def test_crc8_maxim_reference() -> None:
    # Независимая реализация по описанию PROTOCOL.md §2 (poly 0x8C, init 0):
    # таблица предвычислений + справочный проход.
    table = []
    for value in range(256):
        crc = value
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
        table.append(crc)

    def reference(data: bytes) -> int:
        crc = 0
        for byte in data:
            crc = table[byte ^ crc]
        return crc

    samples = (b"", b"\x00", b"\xff", b"MCHOSE", bytes(range(56)))
    for sample in samples:
        assert proto.crc8_maxim(sample) == reference(sample)
    # Эталонное значение CRC-8/MAXIM для контрольной последовательности.
    assert proto.crc8_maxim(b"123456789") == 0xA1


def test_dock_frame_build_layout() -> None:
    params = bytes(range(1, 11))
    frame = proto.build_dock_frame(
        proto.DOCK_CMD_SET_GLOBAL_INFO, params, sequence=2, total_frames=1
    )
    assert frame[:6] == bytes(
        (0xAA, proto.DOCK_CMD_SET_GLOBAL_INFO, proto.DOCK_MSG_REQUEST, 2, 1, 10)
    )
    assert frame[6:16] == params
    assert frame[16:] == bytes(proto.DOCK_FRAME_SIZE - 16)
    assert len(frame) == proto.DOCK_FRAME_SIZE


def test_dock_frame_build_unpadded() -> None:
    frame = proto.build_dock_frame(
        proto.DOCK_CMD_SET_LIGHT_COLORS, bytes(10), report_len=None
    )
    assert len(frame) == 16  # заголовок 6 байтов + 10 байтов params


def test_dock_frame_parse_roundtrip() -> None:
    params = bytes((1, 0, 5, 3, 80, 0, 255, 0, 0))
    frame = proto.build_dock_frame(proto.DOCK_CMD_READ_GLOBAL_INFO, params)
    parsed = proto.parse_dock_frame(frame)
    assert parsed == proto.DockFrame(
        command_id=proto.DOCK_CMD_READ_GLOBAL_INFO,
        message_type=proto.DOCK_MSG_REQUEST,
        sequence=0,
        total_frames=0,
        params=params,
    )
    assert proto.parse_dock_frame(bytes((proto.DOCK_REPORT_ID,)) + frame) == parsed


def test_dock_frame_parse_slices_by_param_len() -> None:
    raw = bytes((0xAA, proto.DOCK_CMD_READ_LIGHT_INFO, 2, 0, 0, 2, 7, 8, 0xEE))
    parsed = proto.parse_dock_frame(raw)
    assert parsed is not None
    assert parsed.message_type == proto.DOCK_MSG_RESPONSE
    assert parsed.params == b"\x07\x08"


def test_dock_frame_parse_garbage() -> None:
    assert proto.parse_dock_frame(b"") is None
    assert proto.parse_dock_frame(bytes(10)) is None
    assert proto.parse_dock_frame(b"\xaa\x07\x00") is None


@pytest.mark.parametrize(
    ("builder", "command", "params"),
    [
        (lambda: proto.build_set_light_switch(True), 33, b"\x01"),
        (lambda: proto.build_set_light_switch(False), 33, b"\x00"),
        (lambda: proto.build_set_sleep(True, 30), 34, b"\x01\x1e"),
        (lambda: proto.build_set_light_mode(3), 35, b"\x03"),
        (lambda: proto.build_set_light_brightness(2, 66), 36, b"\x02\x42"),
        (lambda: proto.build_set_light_speed(4), 37, b"\x04"),
        (lambda: proto.build_set_base_color((10, 20, 30)), 38, b"\x0a\x14\x1e"),
        (proto.build_reset_dock, 42, b""),
    ],
)
def test_dock_set_builders(
    builder: Callable[[], bytes], command: int, params: bytes
) -> None:
    frame = builder()
    assert frame[:6] == bytes(
        (0xAA, command, proto.DOCK_MSG_REQUEST, 0, 0, len(params))
    )
    assert frame[6 : 6 + len(params)] == params


@pytest.mark.parametrize(
    ("builder", "command"),
    [
        (proto.build_read_light_switch, proto.DOCK_CMD_READ_LIGHT_SWITCH),
        (proto.build_read_sleep_info, proto.DOCK_CMD_READ_SLEEP_INFO),
        (proto.build_read_device_version, proto.DOCK_CMD_READ_DEVICE_VERSION),
        (proto.build_read_device_name, proto.DOCK_CMD_READ_DEVICE_NAME),
        (proto.build_read_light_info, proto.DOCK_CMD_READ_LIGHT_INFO),
        (proto.build_read_global_info, proto.DOCK_CMD_READ_GLOBAL_INFO),
        (proto.build_read_light_colors, proto.DOCK_CMD_READ_LIGHT_COLORS),
    ],
)
def test_dock_read_builders(builder: Callable[[], bytes], command: int) -> None:
    frame = builder()
    assert frame[:6] == bytes((0xAA, command, proto.DOCK_MSG_REQUEST, 0, 0, 0))
    assert len(frame) == proto.DOCK_FRAME_SIZE


def test_build_set_global_info_layout() -> None:
    frame = proto.build_set_global_info(
        enabled=True,
        mode=4,
        mode_count=6,
        speed=2,
        brightness=70,
        light_sync=False,
        rgb=(10, 20, 30),
        direction=1,
    )
    assert frame[:6] == bytes((0xAA, proto.DOCK_CMD_SET_GLOBAL_INFO, 0, 0, 0, 10))
    assert frame[6:16] == bytes((1, 4, 6, 2, 70, 0, 10, 20, 30, 1))


def test_parse_global_info_layout() -> None:
    params = bytearray(proto.DOCK_FRAME_SIZE)
    params[0] = 1
    params[1] = 2
    params[2] = 6
    params[3] = 3
    params[4] = 88
    params[5] = 1
    params[6:9] = b"\x0c\x22\x38"
    params[9] = 1
    params[10] = 30
    params[12] = 2
    params[13] = 65
    params[29] = 1
    info = proto.parse_global_info(bytes(params))
    assert info == proto.DockLighting(
        enabled=True,
        mode=2,
        mode_count=6,
        speed=3,
        brightness=88,
        light_sync=True,
        base_color=(12, 34, 56),
        direction=1,
        sleep_enabled=True,
        sleep_time=30,
        version="065002",
    )


def test_parse_light_info_layout() -> None:
    params = bytes((1, 3, 6, 2, 45, 0, 255, 160, 0)) + bytes(20)
    info = proto.parse_light_info(params)
    assert info == proto.DockLighting(
        enabled=True,
        mode=3,
        mode_count=6,
        speed=2,
        brightness=45,
        light_sync=False,
        base_color=(255, 160, 0),
        direction=0,
    )
    assert proto.parse_light_info(bytes(4)) is None
    assert proto.parse_global_info(bytes(10)) is None
