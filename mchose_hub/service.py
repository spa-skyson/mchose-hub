"""Сервисный слой: логика запрос-ответ над транспортом.

HeadsetService управляет наушниками через донгл (reportId 0x55),
DockService — RGB-доком (кадры 0xAA, reportId 0). Печать внутри
библиотеки не выполняется; ошибки записи отдают TransportError.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace

from . import protocol
from .transport import HidTransport

READ_TIMEOUT_MS = 800
SETTLE_DELAY_S = 0.05
DRAIN_TIMEOUT_MS = 200
# Пассивный приём: бюджет ожидания первого пуш-кадра за тик pump_passive.
# Донгл присылает статус сам (PROTOCOL.md §1 push-уведомления, §7 захват
# M HUB: 55 11 02, 55 22 04 0a, 55 65 64 02, 55 22 02 00).
PASSIVE_TIMEOUT_MS = 10
PASSIVE_MAX_FRAMES = 16


def _strip_report_id(frame: bytes, frame_size: int) -> bytes:
    """Убирает ведущий report-байт hidraw, если кадр длиннее data-части."""
    return frame[1:] if len(frame) > frame_size else frame


class HeadsetService:
    """Управление наушниками через донгл (PROTOCOL.md §1).

    Командный канал донгла открывается транспортом ОДИН раз при подключении
    (эталон M HUB) — команды уходят простой interrupt OUT записью. Донгл
    присылает статус и сам (push-кадры 0x55 65/11/22): всё прочитанное
    разбирается и обновляет кэш состояния, который отдаётся GUI между
    явными запросами.
    """

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
        # Кэш последнего известного состояния: пишется из любых чтений
        # (ответы и пуш-кадры), читается GUI между явными запросами.
        self._cache_lock = threading.Lock()
        self._cached_battery: protocol.BatteryState | None = None
        self._cached_audio_mode: protocol.AudioMode | None = None
        self._cached_auto_shutdown: int | None = None  # минуты
        self._cached_prompt_volume: int | None = None
        # Счётчик разобранных кадров: признак живого канала для GUI.
        self._frames_rx = 0

    def _ingest(self, frame: bytes) -> None:
        """Разобрать прочитанный кадр 0x55 и обновить кэш состояния.

        Годится для ответов и пассивных пуш-кадров (батарея/режим/настройки):
        parse_* сами отсеивают чужие команды. Кадры настроек — форма
        [0x55, 0x22, sub, value] с read-подкомандами 2/4.
        """
        data = _strip_report_id(frame, protocol.HEADSET_FRAME_SIZE)
        with self._cache_lock:
            self._frames_rx += 1
            if (state := protocol.parse_battery(data)) is not None:
                self._cached_battery = state
            elif (mode := protocol.parse_audio_mode(data)) is not None:
                self._cached_audio_mode = protocol.AudioMode(mode)
            elif (push := protocol.parse_settings_push(data)) is not None:
                sub, value = push
                if sub == protocol.SETTINGS_SUB_READ_AUTO_SHUTDOWN:
                    self._cached_auto_shutdown = (
                        protocol.auto_shutdown_value_to_minutes(value)
                    )
                elif sub == protocol.SETTINGS_SUB_READ_PROMPT_VOLUME:
                    self._cached_prompt_volume = value

    def pump_passive(self, timeout_ms: int = PASSIVE_TIMEOUT_MS) -> int:
        """Принять пассивные кадры донгла БЕЗ записи в устройство.

        Один тик GUI-опроса: короткое чтение, разбор пуш-кадров, кэш.
        Возвращает число разобранных кадров; TransportError (мёртвый
        транспорт) пробрасывается — лечится ретраем с пере-сканом.
        Блокировка сервиса держится на весь цикл чтения: пассивный приём
        не должен забрать ответ параллельной команды — она дождётся
        своего ответа, пуши останутся на следующий тик.
        """
        with self._lock:
            count = 0
            while count < PASSIVE_MAX_FRAMES:
                frame = self._transport.read_report(
                    timeout_ms if count == 0 else 0  # хвост дренируем без ожидания
                )
                if frame is None:
                    break
                self._ingest(frame)
                count += 1
            return count

    def frames_received(self) -> int:
        """Сколько кадров 0x55 разобрано за жизнь сервиса (ответы и пуши).

        Признак живого канала для GUI: счётчик растёт — кадр пришёл
        (пуш-кадр или свежий ответ на явный запрос).
        """
        with self._cache_lock:
            return self._frames_rx

    def cached_battery(self) -> protocol.BatteryState | None:
        """Последнее известное состояние батареи (ответ или пуш)."""
        with self._cache_lock:
            return self._cached_battery

    def cached_audio_mode(self) -> protocol.AudioMode | None:
        """Последний известный режим звука (ответ или пуш)."""
        with self._cache_lock:
            return self._cached_audio_mode

    def cached_auto_shutdown(self) -> int | None:
        """Последнее известное автоотключение в минутах."""
        with self._cache_lock:
            return self._cached_auto_shutdown

    def cached_prompt_volume(self) -> int | None:
        """Последняя известная громкость подсказок."""
        with self._cache_lock:
            return self._cached_prompt_volume

    def _query(self, payload: bytes) -> bytes | None:
        """Команда-ответ под блокировкой сервиса; канал уже открыт транспортом.

        UNLOCK/LOCK вокруг отдельных команд не выполняются: M HUB открывает
        канал ОДИН раз на подключение (PROTOCOL.md §7), пере-открытие —
        только при новом транспорте после восстановления. Каждый прочитанный
        кадр разбирается в кэш: между ответами приходят и пассивные пуш-кадры.
        """
        with self._lock:
            self._transport.write_report(protocol.HEADSET_REPORT_ID, payload)
            frame = self._transport.read_report(self._read_timeout_ms)
        if frame is None:
            return None
        self._ingest(frame)
        return _strip_report_id(frame, protocol.HEADSET_FRAME_SIZE)

    def _read_accept(self) -> bool:
        """Прочитать accept-подтверждение set-подкоманды (под блокировкой).

        True — устройство подтвердило команду (кадр с заголовком [0x55,0x22]);
        False — ответа нет или кадр не распознан. Кадр разбирается в кэш.
        """
        frame = self._transport.read_report(DRAIN_TIMEOUT_MS)
        if frame is None:
            return False
        self._ingest(frame)
        return (
            protocol.parse_settings_response(
                _strip_report_id(frame, protocol.HEADSET_FRAME_SIZE),
                expect_value=False,
            )
            is not None
        )

    def poll_battery(self) -> protocol.BatteryState | None:
        """Свежая батарея; без ответа — последний известный статус из кэша."""
        frame = self._query(protocol.build_battery_query())
        if frame is not None:
            state = protocol.parse_battery(frame)
            if state is not None:
                return state
        return self.cached_battery()

    def poll_audio_mode(self) -> protocol.AudioMode | None:
        """Свежий режим звука; без ответа — последний известный из кэша."""
        frame = self._query(protocol.build_audio_mode_query())
        if frame is not None:
            mode_raw = protocol.parse_audio_mode(frame)
            if mode_raw is not None:
                return protocol.AudioMode(mode_raw)
        return self.cached_audio_mode()

    def poll_status(
        self,
    ) -> tuple[protocol.BatteryState | None, protocol.AudioMode | None] | None:
        """Батарея и режим из свежего опроса или кэша.

        None — не известно ни то, ни другое (кэш пуст и опрос молчит).
        """
        battery = self.poll_battery()
        mode = self.poll_audio_mode()
        if battery is None and mode is None:
            return None
        return battery, mode

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
        self._ingest(frame)
        mode_raw = protocol.parse_audio_mode(
            _strip_report_id(frame, protocol.HEADSET_FRAME_SIZE)
        )
        if mode_raw is None:
            return None
        return protocol.AudioMode(mode_raw)

    def set_auto_shutdown(self, minutes: int) -> bool:
        """Автоотключение: 15/30/45 минут, иначе ValueError.

        True — устройство подтвердило команду accept-кадром.
        """
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
            accepted = self._read_accept()
        return accepted

    def get_auto_shutdown(self) -> int | None:
        """Свежее автоотключение в минутах; без ответа — из кэша."""
        frame = self._query(
            protocol.build_settings(protocol.SETTINGS_SUB_READ_AUTO_SHUTDOWN)
        )
        if frame is not None:
            value = protocol.parse_settings_response(frame, expect_value=True)
            if value is not None:
                minutes = protocol.auto_shutdown_value_to_minutes(value)
                if minutes is not None:
                    return minutes
        return self.cached_auto_shutdown()

    def set_prompt_volume(self, level: int) -> bool:
        """Громкость голосовых подсказок, 0..15.

        True — устройство подтвердило команду accept-кадром.
        """
        if not 0 <= level <= protocol.PROMPT_VOLUME_MAX:
            raise ValueError(
                f"громкость подсказок принимает значение 0..{protocol.PROMPT_VOLUME_MAX}"
            )
        with self._lock:
            self._transport.write_report(
                protocol.HEADSET_REPORT_ID,
                protocol.build_settings(protocol.SETTINGS_SUB_SET_PROMPT_VOLUME, level),
            )
            accepted = self._read_accept()
        return accepted

    def get_prompt_volume(self) -> int | None:
        """Свежая громкость подсказок; без ответа — из кэша."""
        frame = self._query(
            protocol.build_settings(protocol.SETTINGS_SUB_READ_PROMPT_VOLUME)
        )
        if frame is not None:
            value = protocol.parse_settings_response(frame, expect_value=True)
            if value is not None:
                return value
        return self.cached_prompt_volume()

    def factory_reset(self) -> bool:
        """Сброс настроек наушников; accept-кадр потребляется внутри сессии.

        True — устройство подтвердило сброс; False — подтверждения нет
        (наушники не ответили или кадр не распознан).
        """
        with self._lock:
            self._transport.write_report(
                protocol.HEADSET_REPORT_ID,
                protocol.build_settings(protocol.SETTINGS_SUB_FACTORY_RESET),
            )
            accepted = self._read_accept()
        return accepted


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
        # RLock: составные операции (set_direction) переиспользуют блокировку
        # внутри, оставаясь атомарными для внешних вызовов.
        self._lock = threading.RLock()

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

    def set_direction(self, direction: int) -> bool:
        """Направление Flowing Wave: атомарный read-modify-write.

        Отдельной команды direction нет: под одной блокировкой сервиса
        читается свежий снимок и сразу пишется с новым направлением —
        параллельная команда не вклинится между чтением и записью.
        True — снимок прочитан и записан; False — устройство не ответило.
        """
        with self._lock:
            info = self.read_global_info()
            if info is None:
                return False
            self.set_global_info(replace(info, direction=direction))
            return True

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
