"""Чистый протокольный слой MCHOSE V9 Turbo+.

Единственный источник раскладок байтов, констант и карт значений — PROTOCOL.md.
Модуль не выполняет I/O: только сборка и разбор кадров.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

# --- USB-идентификаторы (PROTOCOL.md, таблица устройств) ---
VID_MCHOSE = 0x3837
PID_DOCK = 0x6008
PID_HEADSET_DONGLE = 0x600A
PID_HEADSET_WIRED = 0x600D

# --- Размеры кадров: 63 байта данных, 64 на проводе с reportId ---
HEADSET_REPORT_ID = 0x55
DOCK_REPORT_ID = 0x00
HEADSET_FRAME_SIZE = 63
DOCK_FRAME_SIZE = 63

# --- Наушники: команды внутри output report 0x55 ---
_BATTERY_CMD = 0x65
_AUDIO_MODE_QUERY_CMD = 0x11
_AUDIO_MODE_SET_CMD = 0x21
_SETTINGS_CMD = 0x22

BATTERY_STATUS_DISCHARGE = 2
BATTERY_STATUS_CHARGING = 3
BATTERY_STATUS_FULL = 4
BATTERY_STATUS_ASLEEP = 26

SETTINGS_SUB_SET_AUTO_SHUTDOWN = 0
SETTINGS_SUB_FACTORY_RESET = 1
SETTINGS_SUB_READ_AUTO_SHUTDOWN = 2
SETTINGS_SUB_SET_PROMPT_VOLUME = 3
SETTINGS_SUB_READ_PROMPT_VOLUME = 4

AUTO_SHUTDOWN_MINUTES_TO_VALUE: dict[int, int] = {15: 1, 30: 4, 45: 0}
PROMPT_VOLUME_MAX = 15
PROMPT_VOLUME_DEFAULT = 10

# --- Док: кадр [0xAA, cmdId, msgType, seq, totalFrames, paramLen, params...] ---
_DOCK_START = 0xAA
_DOCK_PARAM_OFFSET = 6
DOCK_MSG_REQUEST = 0
DOCK_MSG_RESPONSE = 2

DOCK_CMD_READ_LIGHT_SWITCH = 1
DOCK_CMD_READ_SLEEP_INFO = 2
DOCK_CMD_READ_DEVICE_BASE_INFO = 3
DOCK_CMD_READ_DEVICE_VERSION = 4
DOCK_CMD_READ_DEVICE_NAME = 5
DOCK_CMD_READ_LIGHT_INFO = 6
DOCK_CMD_READ_GLOBAL_INFO = 7
DOCK_CMD_READ_LIGHT_COLORS = 8
DOCK_CMD_SET_LIGHT_SWITCH = 33
DOCK_CMD_SET_SLEEP_INFO = 34
DOCK_CMD_SET_LIGHT_MODE = 35
DOCK_CMD_SET_LIGHT_BRIGHTNESS = 36
DOCK_CMD_SET_LIGHT_SPEED = 37
DOCK_CMD_SET_BASE_COLOR = 38
DOCK_CMD_SET_GLOBAL_INFO = 39
DOCK_CMD_SET_LIGHT_SYNC = 40
DOCK_CMD_SET_LIGHT_COLORS = 41
DOCK_CMD_RESET_DOCK = 42
DOCK_CMD_ENTER_UPDATE_MODE = 43
DOCK_CMD_WRITE_FIRMWARE_PACKET = 116
DOCK_CMD_COMPLETE_FIRMWARE = 117


class AudioMode(IntEnum):
    """Режим звука наушников (PROTOCOL.md §1)."""

    GAME_1 = 0
    GAME_2 = 1
    MUSIC = 2


class LightMode(IntEnum):
    """Режимы подсветки дока (PROTOCOL.md §6)."""

    CONSTANT = 0
    BREATHING = 1
    STARRY = 2
    CYCLIC = 3
    WAVE = 4
    MUSIC = 5


AUDIO_MODE_NAMES: dict[int, str] = {0: "Game 1", 1: "Game 2", 2: "Music"}
LIGHT_MODE_NAMES: dict[int, str] = {
    LightMode.CONSTANT: "Constant On",
    LightMode.BREATHING: "Breathing",
    LightMode.STARRY: "Starry Night",
    LightMode.CYCLIC: "Cyclic Color Change",
    LightMode.WAVE: "Flowing Wave",
    LightMode.MUSIC: "Music Rhythm",
}


@dataclass(frozen=True)
class BatteryState:
    """Состояние батареи наушников."""

    percent: int
    charging: bool
    full: bool
    asleep: bool


@dataclass(frozen=True)
class DockFrame:
    """Разобранный кадр дока без заголовка report-слоя."""

    command_id: int
    message_type: int
    sequence: int
    total_frames: int
    params: bytes


@dataclass(frozen=True)
class DockLighting:
    """Снимок настроек подсветки дока (readGlobalInfo/readLightInfo)."""

    enabled: bool
    mode: int
    mode_count: int
    speed: int
    brightness: int
    light_sync: bool
    base_color: tuple[int, int, int]
    direction: int
    sleep_enabled: bool | None = None
    sleep_time: int | None = None
    version: str | None = None


def _headset_frame(header: bytes) -> bytes:
    """Кадр наушников: header + нулевой паддинг до 63 байт."""
    frame = bytearray(HEADSET_FRAME_SIZE)
    frame[: len(header)] = header
    return bytes(frame)


def _match_headset(data: bytes, cmd: int) -> int | None:
    """Индекс первого байта значения после заголовка [0x55, cmd].

    Принимается и полный payload с ведущим маркером 0x55, и усечённый
    буфер, начинающийся с командного байта (после снятия reportId).
    """
    if len(data) >= 2 and data[0] == HEADSET_REPORT_ID and data[1] == cmd:
        return 2
    if data and data[0] == cmd:
        return 1
    return None


def build_battery_query() -> bytes:
    """Запрос батареи: [0x55, 0x65, 0x01] + нули, 63 байта."""
    return _headset_frame(bytes((HEADSET_REPORT_ID, _BATTERY_CMD, 0x01)))


def parse_battery(data: bytes) -> BatteryState | None:
    """Разбор ответа на запрос батареи: percent и status после [0x55, 0x65]."""
    idx = _match_headset(data, _BATTERY_CMD)
    if idx is None or len(data) < idx + 2:
        return None
    percent, status = data[idx], data[idx + 1]
    if percent > 100:
        return None
    return BatteryState(
        percent=percent,
        charging=status == BATTERY_STATUS_CHARGING,
        full=status == BATTERY_STATUS_FULL,
        asleep=status == BATTERY_STATUS_ASLEEP,
    )


def build_audio_mode_query() -> bytes:
    """Запрос режима звука: [0x55, 0x11] + нули, 63 байта."""
    return _headset_frame(bytes((HEADSET_REPORT_ID, _AUDIO_MODE_QUERY_CMD)))


def build_set_audio_mode(mode: AudioMode) -> bytes:
    """Установка режима звука: [0x55, 0x21, 0x01, mode].

    PROTOCOL.md отмечает, что официальное ПО отправляет укороченный
    15-байтовый вариант кадра; здесь используется полный 63-байтовый.
    """
    return _headset_frame(
        bytes((HEADSET_REPORT_ID, _AUDIO_MODE_SET_CMD, 0x01, int(mode)))
    )


def parse_audio_mode(data: bytes) -> int | None:
    """Разбор ответа на запрос режима звука: mode после [0x55, 0x11]."""
    idx = _match_headset(data, _AUDIO_MODE_QUERY_CMD)
    if idx is None or len(data) <= idx:
        return None
    mode = data[idx]
    try:
        AudioMode(mode)
    except ValueError:
        return None
    return mode


def build_settings(sub_id: int, value: int | None = None) -> bytes:
    """Настройки: [0x55, 0x22, subId, value?] + нули, 63 байта."""
    header = [HEADSET_REPORT_ID, _SETTINGS_CMD, sub_id]
    if value is not None:
        header.append(value)
    return _headset_frame(bytes(header))


def parse_settings_response(data: bytes, expect_value: bool) -> int | None:
    """Разбор ответа settings: матч начала [0x55, 0x22], значение в байте 2.

    expect_value=True — ответ на read-подкоманду (значение обязательно);
    expect_value=False — подтверждение set-команды (байт интерпретируется
    как accept, но проверки значения не требуется).
    """
    idx = _match_headset(data, _SETTINGS_CMD)
    if idx is None or len(data) <= idx:
        return None
    return data[idx]


def auto_shutdown_value_to_minutes(value: int) -> int | None:
    """Обратная карта AUTO_SHUTDOWN_MINUTES_TO_VALUE."""
    for minutes, encoded in AUTO_SHUTDOWN_MINUTES_TO_VALUE.items():
        if encoded == value:
            return minutes
    return None


def crc8_maxim(data: bytes) -> int:
    """CRC-8/MAXIM (отражённый poly 0x8C, init 0) — PROTOCOL.md §2, OTA."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc


def build_dock_frame(
    command_id: int,
    params: bytes = b"",
    *,
    sequence: int = 0,
    total_frames: int = 0,
    message_type: int = DOCK_MSG_REQUEST,
    report_len: int | None = DOCK_FRAME_SIZE,
) -> bytes:
    """Кадр дока [0xAA, cmd, msgType, seq, total, paramLen, params...] + паддинг.

    report_len=None отключает паддинг (multi-кадровая передача цветов,
    где reportLength = len(params) + 6).
    """
    body = bytearray(_DOCK_PARAM_OFFSET + len(params))
    body[0] = _DOCK_START
    body[1] = command_id
    body[2] = message_type
    body[3] = sequence
    body[4] = total_frames
    body[5] = len(params)
    body[6:] = params
    if report_len is not None and len(body) < report_len:
        body.extend(bytes(report_len - len(body)))
    return bytes(body)


def parse_dock_frame(data: bytes) -> DockFrame | None:
    """Разбор кадра дока; params срезаются по paramLen с offset 6."""
    if len(data) > 1 and data[0] == DOCK_REPORT_ID:
        data = data[1:]  # необязательный ведущий report-байт hidraw
    if len(data) < _DOCK_PARAM_OFFSET or data[0] != _DOCK_START:
        return None
    param_len = data[5]
    return DockFrame(
        command_id=data[1],
        message_type=data[2],
        sequence=data[3],
        total_frames=data[4],
        params=bytes(data[6 : 6 + param_len]),
    )


def build_read_light_switch() -> bytes:
    return build_dock_frame(DOCK_CMD_READ_LIGHT_SWITCH)


def build_read_sleep_info() -> bytes:
    return build_dock_frame(DOCK_CMD_READ_SLEEP_INFO)


def build_read_device_version() -> bytes:
    return build_dock_frame(DOCK_CMD_READ_DEVICE_VERSION)


def build_read_device_name() -> bytes:
    return build_dock_frame(DOCK_CMD_READ_DEVICE_NAME)


def build_read_light_info() -> bytes:
    return build_dock_frame(DOCK_CMD_READ_LIGHT_INFO)


def build_read_global_info() -> bytes:
    return build_dock_frame(DOCK_CMD_READ_GLOBAL_INFO)


def build_read_light_colors() -> bytes:
    return build_dock_frame(DOCK_CMD_READ_LIGHT_COLORS)


def build_set_light_switch(on: bool) -> bytes:
    return build_dock_frame(DOCK_CMD_SET_LIGHT_SWITCH, bytes((1 if on else 0,)))


def build_set_sleep(enabled: bool, minutes: int) -> bytes:
    return build_dock_frame(
        DOCK_CMD_SET_SLEEP_INFO, bytes((1 if enabled else 0, minutes))
    )


def build_set_light_mode(mode: int) -> bytes:
    return build_dock_frame(DOCK_CMD_SET_LIGHT_MODE, bytes((mode,)))


def build_set_light_brightness(mode: int, brightness: int) -> bytes:
    return build_dock_frame(DOCK_CMD_SET_LIGHT_BRIGHTNESS, bytes((mode, brightness)))


def build_set_light_speed(speed: int) -> bytes:
    return build_dock_frame(DOCK_CMD_SET_LIGHT_SPEED, bytes((speed,)))


def build_set_base_color(rgb: tuple[int, int, int]) -> bytes:
    return build_dock_frame(DOCK_CMD_SET_BASE_COLOR, bytes(rgb))


def build_set_global_info(
    *,
    enabled: bool,
    mode: int,
    mode_count: int = 5,
    speed: int,
    brightness: int,
    light_sync: bool,
    rgb: tuple[int, int, int],
    direction: int = 0,
) -> bytes:
    """setGlobalInfo (cmd 39): 10 байтов параметров, см. PROTOCOL.md §2."""
    params = bytes(
        (
            1 if enabled else 0,
            mode,
            mode_count,
            speed,
            brightness,
            1 if light_sync else 0,
            rgb[0],
            rgb[1],
            rgb[2],
            direction,
        )
    )
    return build_dock_frame(DOCK_CMD_SET_GLOBAL_INFO, params)


def build_reset_dock() -> bytes:
    return build_dock_frame(DOCK_CMD_RESET_DOCK)


def parse_light_info(params: bytes) -> DockLighting | None:
    """readLightInfo: enabled@0, mode@1, modeCount@2, speed@3, brightness@4,
    lightSync@5, rgb@6-8."""
    if len(params) < 9:
        return None
    return DockLighting(
        enabled=bool(params[0]),
        mode=params[1],
        mode_count=params[2],
        speed=params[3],
        brightness=params[4],
        light_sync=bool(params[5]),
        base_color=(params[6], params[7], params[8]),
        direction=0,
    )


def _format_version(first: int, second: int) -> str:
    """Версия из пары байтов: каждое число в десятичном виде дополняется
    нулями до 3 цифр, результаты конкатенируются (логика M HUB)."""
    return f"{first:03d}{second:03d}"


def parse_global_info(params: bytes) -> DockLighting | None:
    """readGlobalInfo: как lightInfo + sleepEnabled@9, sleepTime@10,
    version из байтов 13,12, direction@29."""
    if len(params) < 30:
        return None
    return DockLighting(
        enabled=bool(params[0]),
        mode=params[1],
        mode_count=params[2],
        speed=params[3],
        brightness=params[4],
        light_sync=bool(params[5]),
        base_color=(params[6], params[7], params[8]),
        direction=params[29],
        sleep_enabled=bool(params[9]),
        sleep_time=params[10],
        version=_format_version(params[13], params[12]),
    )
