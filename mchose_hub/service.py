"""Сервисный слой: логика запрос-ответ над транспортом.

HeadsetService управляет наушниками через донгл (reportId 0x55),
DockService — RGB-доком (кадры 0xAA, reportId 0). Печать внутри
библиотеки не выполняется; ошибки записи отдают TransportError.
"""

from __future__ import annotations

import threading
import time

from . import protocol
from .transport import HidTransport

READ_TIMEOUT_MS = 800
SETTLE_DELAY_S = 0.05
DRAIN_TIMEOUT_MS = 200


def _strip_report_id(frame: bytes, frame_size: int) -> bytes:
    """Убирает ведущий report-байт hidraw, если кадр длиннее data-части."""
    return frame[1:] if len(frame) > frame_size else frame


class HeadsetService:
    """Управление наушниками через донгл (PROTOCOL.md §1)."""

    def __init__(
        self,
        transport: HidTransport,
        *,
        read_timeout_ms: int = READ_TIMEOUT_MS,
        settle_delay_s: float = SETTLE_DELAY_S,
    ) -> None:
        self._transport = transport
        self._read_timeout_ms = read_timeout_ms
        self._settle_delay_s = settle_delay_s
        self._lock = threading.Lock()

    def _query(self, payload: bytes) -> bytes | None:
        """Атомарная пара запись-чтение; payload без report-байта."""
        with self._lock:
            self._transport.write_report(protocol.HEADSET_REPORT_ID, payload)
            frame = self._transport.read_report(self._read_timeout_ms)
        if frame is None:
            return None
        return _strip_report_id(frame, protocol.HEADSET_FRAME_SIZE)

    def _drain(self) -> None:
        """Короткое чтение: подтверждение не должно остаться в буфере."""
        with self._lock:
            self._transport.read_report(DRAIN_TIMEOUT_MS)

    def poll_status(self) -> tuple[protocol.BatteryState, protocol.AudioMode] | None:
        """Батарея и режим звука; None, если любой шаг не ответил."""
        battery_frame = self._query(protocol.build_battery_query())
        battery = (
            protocol.parse_battery(battery_frame) if battery_frame is not None else None
        )
        if battery is None:
            return None
        mode_frame = self._query(protocol.build_audio_mode_query())
        mode_raw = (
            protocol.parse_audio_mode(mode_frame) if mode_frame is not None else None
        )
        if mode_raw is None:
            return None
        return battery, protocol.AudioMode(mode_raw)

    def set_audio_mode(self, mode: protocol.AudioMode) -> protocol.AudioMode | None:
        """Установить режим и прочитать фактический; None без ответа."""
        with self._lock:
            self._transport.write_report(
                protocol.HEADSET_REPORT_ID, protocol.build_set_audio_mode(mode)
            )
            if self._settle_delay_s > 0:
                time.sleep(self._settle_delay_s)
            self._transport.write_report(
                protocol.HEADSET_REPORT_ID, protocol.build_audio_mode_query()
            )
            frame = self._transport.read_report(self._read_timeout_ms)
        if frame is None:
            return None
        mode_raw = protocol.parse_audio_mode(
            _strip_report_id(frame, protocol.HEADSET_FRAME_SIZE)
        )
        if mode_raw is None:
            return None
        return protocol.AudioMode(mode_raw)

    def set_auto_shutdown(self, minutes: int) -> None:
        """Автоотключение: 15/30/45 минут, иначе ValueError."""
        value = protocol.AUTO_SHUTDOWN_MINUTES_TO_VALUE.get(minutes)
        if value is None:
            supported = ", ".join(
                str(item) for item in sorted(protocol.AUTO_SHUTDOWN_MINUTES_TO_VALUE)
            )
            raise ValueError(f"автоотключение поддерживает только {supported} минут")
        with self._lock:
            self._transport.write_report(
                protocol.HEADSET_REPORT_ID,
                protocol.build_settings(protocol.SETTINGS_SUB_SET_AUTO_SHUTDOWN, value),
            )
            self._transport.read_report(DRAIN_TIMEOUT_MS)

    def get_auto_shutdown(self) -> int | None:
        """Текущее автоотключение в минутах."""
        frame = self._query(
            protocol.build_settings(protocol.SETTINGS_SUB_READ_AUTO_SHUTDOWN)
        )
        if frame is None:
            return None
        value = protocol.parse_settings_response(frame, expect_value=True)
        if value is None:
            return None
        return protocol.auto_shutdown_value_to_minutes(value)

    def set_prompt_volume(self, level: int) -> None:
        """Громкость голосовых подсказок, 0..15."""
        if not 0 <= level <= protocol.PROMPT_VOLUME_MAX:
            raise ValueError(
                f"громкость подсказок принимает значение 0..{protocol.PROMPT_VOLUME_MAX}"
            )
        with self._lock:
            self._transport.write_report(
                protocol.HEADSET_REPORT_ID,
                protocol.build_settings(protocol.SETTINGS_SUB_SET_PROMPT_VOLUME, level),
            )
            self._transport.read_report(DRAIN_TIMEOUT_MS)

    def get_prompt_volume(self) -> int | None:
        """Текущая громкость подсказок."""
        frame = self._query(
            protocol.build_settings(protocol.SETTINGS_SUB_READ_PROMPT_VOLUME)
        )
        if frame is None:
            return None
        return protocol.parse_settings_response(frame, expect_value=True)

    def factory_reset(self) -> None:
        """Сброс настроек наушников; ответ не ждётся."""
        with self._lock:
            self._transport.write_report(
                protocol.HEADSET_REPORT_ID,
                protocol.build_settings(protocol.SETTINGS_SUB_FACTORY_RESET),
            )


class DockService:
    """Управление RGB-доком (PROTOCOL.md §2)."""

    def __init__(
        self,
        transport: HidTransport,
        *,
        read_timeout_ms: int = READ_TIMEOUT_MS,
    ) -> None:
        self._transport = transport
        self._read_timeout_ms = read_timeout_ms
        self._lock = threading.Lock()

    def _exchange(self, frame: bytes, command_id: int) -> protocol.DockFrame | None:
        """Запрос-ответ; ответ обязан быть response-кадром той же команды."""
        with self._lock:
            self._transport.write_report(protocol.DOCK_REPORT_ID, frame)
            response = self._transport.read_report(self._read_timeout_ms)
        if response is None:
            return None
        parsed = protocol.parse_dock_frame(
            _strip_report_id(response, protocol.DOCK_FRAME_SIZE)
        )
        if (
            parsed is None
            or parsed.message_type != protocol.DOCK_MSG_RESPONSE
            or parsed.command_id != command_id
        ):
            return None
        return parsed

    def _send(self, frame: bytes) -> None:
        """Запись set-команды с коротким чтением подтверждения."""
        with self._lock:
            self._transport.write_report(protocol.DOCK_REPORT_ID, frame)
            self._transport.read_report(DRAIN_TIMEOUT_MS)

    def read_global_info(self) -> protocol.DockLighting | None:
        """Полный снимок подсветки, сна, версии и направления."""
        parsed = self._exchange(
            protocol.build_read_global_info(), protocol.DOCK_CMD_READ_GLOBAL_INFO
        )
        if parsed is None:
            return None
        return protocol.parse_global_info(parsed.params)

    def set_global_info(self, lighting: protocol.DockLighting) -> None:
        """Записать все настройки подсветки (cmd 39, mode_count из lighting)."""
        self._send(
            protocol.build_set_global_info(
                enabled=lighting.enabled,
                mode=lighting.mode,
                mode_count=lighting.mode_count,
                speed=lighting.speed,
                brightness=lighting.brightness,
                light_sync=lighting.light_sync,
                rgb=lighting.base_color,
                direction=lighting.direction,
            )
        )

    def set_light_switch(self, enabled: bool) -> None:
        """Включить/выключить подсветку (cmd 33)."""
        self._send(protocol.build_set_light_switch(enabled))

    def set_light_mode(self, mode: int) -> None:
        """Режим подсветки (cmd 35)."""
        self._send(protocol.build_set_light_mode(mode))

    def set_light_brightness(self, mode: int, brightness: int) -> None:
        """Яркость для режима (cmd 36)."""
        self._send(protocol.build_set_light_brightness(mode, brightness))

    def set_light_speed(self, speed: int) -> None:
        """Скорость эффекта (cmd 37)."""
        self._send(protocol.build_set_light_speed(speed))

    def set_base_color(self, rgb: tuple[int, int, int]) -> None:
        """Базовый цвет RGB (cmd 38)."""
        self._send(protocol.build_set_base_color(rgb))

    def set_sleep(self, enabled: bool, minutes: int) -> None:
        """Сон дока (cmd 34)."""
        self._send(protocol.build_set_sleep(enabled, minutes))

    def reset(self) -> None:
        """Сброс дока (cmd 42)."""
        self._send(protocol.build_reset_dock())

    def read_device_info(self) -> dict[str, str] | None:
        """Имя и прошивка дока (cmd 4, 5); None, если не ответил ни один."""
        info: dict[str, str] = {}
        version = self._exchange(
            protocol.build_read_device_version(), protocol.DOCK_CMD_READ_DEVICE_VERSION
        )
        if version is not None and len(version.params) >= 3:
            low, middle, high = version.params[0], version.params[1], version.params[2]
            info["version"] = f"{low}.{middle}.{high}"
        name = self._exchange(
            protocol.build_read_device_name(), protocol.DOCK_CMD_READ_DEVICE_NAME
        )
        if name is not None:
            label = (
                name.params.split(b"\x00", 1)[0]
                .decode("ascii", errors="replace")
                .strip()
            )
            if label:
                info["name"] = label
        return info or None
