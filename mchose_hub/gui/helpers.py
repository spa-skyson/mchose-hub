"""Чистые хелперы GUI: слайдер↔устройство, лимиты, цвета, подписи.

Модуль не зависит от Qt: используется и виджетами, и тестами.
Все правила взяты из PROTOCOL.md §6 (карта эффектов дока V9 Turbo+).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from ..audio.eq import PRESETS
from ..protocol import BatteryState
from ..transport import TransportError

_R = TypeVar("_R")

# Режимы дока, для которых слайдер скорости инвертируется при отправке:
# Breathing (1), Starry (2), Cyclic (3), Music (5).
SPEED_INVERTED_MODES: frozenset[int] = frozenset((1, 2, 3, 5))
# Flowing Wave (4): слайдер скрыт, на устройство всегда уходит 1.
WAVE_MODE = 4

SPEED_SLIDER_MIN = 1
SPEED_SLIDER_MAX = 5
DEVICE_SPEED_MAX = 5

BRIGHTNESS_MIN = 0
# Шкала яркости дока измерена на живом устройстве (V9 Turbo, fw 7.0.0):
# байт хранит 0..4, значения >=5 устройство отсекает до 4 (см. PROTOCOL.md §6).
BRIGHTNESS_MAX = 4
PROMPT_VOLUME_MIN = 0
PROMPT_VOLUME_MAX = 15

# Палитра преднастроенных цветов M HUB (PROTOCOL.md §6).
PRESET_COLORS: tuple[tuple[int, int, int], ...] = (
    (255, 0, 0),
    (255, 160, 0),
    (255, 255, 0),
    (0, 255, 0),
    (0, 255, 255),
    (0, 0, 255),
    (255, 0, 255),
)

BATTERY_COLOR_OK = "#2ecc71"
BATTERY_COLOR_MID = "#f1c40f"
BATTERY_COLOR_LOW = "#e74c3c"
_BATTERY_OK_THRESHOLD = 50
_BATTERY_LOW_THRESHOLD = 20

# Режимы с индивидуальным цветом (PROTOCOL.md §6): Constant/Breathing/Starry/Music.
CUSTOM_COLOR_MODES: frozenset[int] = frozenset((0, 1, 2, 5))


def slider_to_device_speed(mode: int, slider: int) -> int:
    """Слайдер скорости 1..5 → байт скорости для DockService.set_light_speed.

    Для режимов 1, 2, 3, 5 отправляется 5 − slider (инверсия официального
    приложения); для Flowing Wave (4) — всегда 1. Прочие режимы скорость
    не используют.

    raises ValueError: slider вне 1..5 или режим не управляет скоростью.
    """
    if not SPEED_SLIDER_MIN <= slider <= SPEED_SLIDER_MAX:
        raise ValueError(
            f"слайдер скорости принимает {SPEED_SLIDER_MIN}..{SPEED_SLIDER_MAX}, "
            f"получено {slider}"
        )
    if mode == WAVE_MODE:
        return 1
    if mode in SPEED_INVERTED_MODES:
        return SPEED_SLIDER_MAX - slider
    raise ValueError(f"режим {mode} не управляет скоростью")


def device_speed_to_slider(mode: int, device_speed: int) -> int:
    """Байт скорости из DockLighting → значение слайдера 1..5 (обратно).

    Значение результата зажимается в 1..5; вне диапазона устройства
    (0..5) байт считается мусорным.

    raises ValueError: device_speed вне 0..5 или режим без скорости.
    """
    if not 0 <= device_speed <= DEVICE_SPEED_MAX:
        raise ValueError(f"скорость устройства принимает 0..{DEVICE_SPEED_MAX}")
    if mode == WAVE_MODE:
        return 1
    if mode in SPEED_INVERTED_MODES:
        return max(
            SPEED_SLIDER_MIN, min(SPEED_SLIDER_MAX, SPEED_SLIDER_MAX - device_speed)
        )
    raise ValueError(f"режим {mode} не управляет скоростью")


def clamp_brightness(value: int) -> int:
    """Яркость 0..4 — измеренная шкала дока (>=5 устройство само режет до 4)."""
    return max(BRIGHTNESS_MIN, min(BRIGHTNESS_MAX, value))


def clamp_prompt_volume(value: int) -> int:
    """Громкость голосовых подсказок 0..15."""
    return max(PROMPT_VOLUME_MIN, min(PROMPT_VOLUME_MAX, value))


def battery_color(percent: int) -> str:
    """HEX-цвет по уровню: >=50 зелёный, 20..49 жёлтый, <20 красный."""
    if percent >= _BATTERY_OK_THRESHOLD:
        return BATTERY_COLOR_OK
    if percent >= _BATTERY_LOW_THRESHOLD:
        return BATTERY_COLOR_MID
    return BATTERY_COLOR_LOW


def effect_allows_custom_color(mode: int) -> bool:
    """True, если эффект хранит индивидуальный цвет (0, 1, 2, 5)."""
    return mode in CUSTOM_COLOR_MODES


def battery_caption(state: BatteryState) -> str:
    """Короткая подпись состояния батареи: Разряд/Зарядка/Полная/Сон."""
    if state.charging:
        return "Зарядка"
    if state.full:
        return "Полная"
    if state.asleep:
        return "Сон"
    return "Разряд"


# --- EQ: слайдер↔гейн, пресеты, дебаунс (вкладка «Звук») ---

# Слайдер QSlider целочисленный: шаг 0.5 дБ, диапазон -24..24 (= -12..+12 дБ).
EQ_SLIDER_MIN = -24
EQ_SLIDER_MAX = 24
# Шторм движений слайдера недопустим: set_eq_gains пересоздаёт фильтр.
EQ_DEBOUNCE_S = 0.6

CUSTOM_PRESET = "Свои"


def gain_to_slider(gain: float) -> int:
    """Гейн в дБ → позиция слайдера (полушаги 0.5 дБ), clamp в диапазон."""
    return min(EQ_SLIDER_MAX, max(EQ_SLIDER_MIN, round(gain * 2)))


def slider_to_gain(slider: int) -> float:
    """Позиция слайдера → гейн в дБ (полушаги 0.5 дБ)."""
    return slider / 2.0


def preset_for_gains(gains: list[float]) -> str:
    """Имя пресета, точно совпадающего с гейнами; «Свои», если ни один."""
    normalized = [float(g) for g in gains]
    for name, preset in PRESETS.items():
        if [float(g) for g in preset] == normalized:
            return name
    return CUSTOM_PRESET


class Debounce:
    """Чистая логика дебаунса: готовность к отправке через интервал после
    последнего события; take() сбрасывает — следующий burst начинается заново.

    Виджет кормит её монотонным временем из таймера-поллера, поэтому
    тестируется и работает один и тот же код.
    """

    def __init__(self, interval_s: float = EQ_DEBOUNCE_S) -> None:
        self.interval_s = interval_s
        self._last: float | None = None

    def touch(self, now: float) -> None:
        """Зарегистрировать событие (движение слайдера)."""
        self._last = now

    def due(self, now: float) -> bool:
        """Прошёл ли интервал с последнего события."""
        return self._last is not None and now - self._last >= self.interval_s

    def take(self, now: float) -> bool:
        """True один раз, когда интервал истёк; следующий burst — заново."""
        if not self.due(now):
            return False
        self._last = None
        return True


# --- транспорт: авто-восстановление после отравления/EOF ---

# Бюджет пере-сканирования: не чаще одного discover() в секунду.
RECOVERY_DISCOVER_INTERVAL_S = 1.0


def is_recoverable_transport(message: str) -> bool:
    """Транспорт отравлен или устройство отключилось (EOF) — лечится пере-сканом."""
    return "отравлен" in message or "устройство отключено (EOF)" in message


def recovery_due(
    last: float | None,
    now: float,
    interval_s: float = RECOVERY_DISCOVER_INTERVAL_S,
) -> bool:
    """Разрешён ли discover() сейчас: не раньше, чем через interval_s после прошлого."""
    return last is None or now - last >= interval_s


def with_transport_retry(
    job: Callable[[], _R], recover: Callable[[], None]
) -> Callable[[], _R]:
    """Обернуть job одноразовым ретраем: при отравленном/EOF-транспорте —
    recover() (пере-скан, свежие сервисы) и повтор той же команды.

    Повторная ошибка уходит наружу как обычно.
    """

    def wrapped() -> _R:
        try:
            return job()
        except TransportError as exc:
            if not is_recoverable_transport(exc.message):
                raise
            recover()
            return job()

    return wrapped


# --- опрос устройства: подсказка про спящий командный канал ---

# Донгл паркует командный канал после простоя (PROTOCOL.md §7.1): пара
# первых NAK — обычная тишина, с третьего подряд канал считается спящим.
PARKED_CHANNEL_THRESHOLD = 3

PARKED_CHANNEL_HINT = (
    "Канал управления спит после простоя. Выньте и вставьте донгл, "
    "пока наушники включены и играют звук — приложение подхватит автоматически."
)


def parked_channel_hint(streak: int) -> str | None:
    """Подсказка после streak неответов опроса подряд; None — тихий текст."""
    if streak >= PARKED_CHANNEL_THRESHOLD:
        return PARKED_CHANNEL_HINT
    return None
