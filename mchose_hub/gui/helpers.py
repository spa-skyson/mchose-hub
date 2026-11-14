"""Чистые хелперы GUI: слайдер↔устройство, лимиты, цвета, подписи.

Модуль не зависит от Qt: используется и виджетами, и тестами.
Все правила взяты из PROTOCOL.md §6 (карта эффектов дока V9 Turbo+).
"""

from __future__ import annotations

from ..protocol import BatteryState

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
