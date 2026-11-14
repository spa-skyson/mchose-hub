"""Вкладка «Подсветка дока»: эффекты, яркость, скорость, направление, цвет."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..protocol import DockLighting, LightMode
from .helpers import (
    BRIGHTNESS_MAX,
    BRIGHTNESS_MIN,
    PRESET_COLORS,
    clamp_brightness,
    device_speed_to_slider,
    effect_allows_custom_color,
    slider_to_device_speed,
)

if TYPE_CHECKING:
    from .app import DeviceManager
    from .main_window import MainWindow
    from .workers import WorkerHost

_EFFECT_NAMES: dict[int, str] = {
    int(LightMode.CONSTANT): "Постоянное свечение",
    int(LightMode.BREATHING): "Дыхание",
    int(LightMode.STARRY): "Звёздная ночь",
    int(LightMode.CYCLIC): "Циклическая смена цветов",
    int(LightMode.WAVE): "Бегущая волна",
    int(LightMode.MUSIC): "Музыкальный ритм",
}
_SWITCH_LABEL = "Подсветка включена"
_EFFECT_TITLE = "Эффект"
_BRIGHTNESS_TITLE = "Яркость"
_BRIGHTNESS_MIN_LABEL = "0 (минимум)"
_BRIGHTNESS_MAX_LABEL = f"{BRIGHTNESS_MAX} (максимум)"
_BRIGHTNESS_LEVEL_LABEL = "уровень {level}"
_SPEED_TITLE = "Скорость"
_DIRECTION_TITLE = "Направление (бегущая волна)"
_DIRECTION_CW = "По часовой"
_DIRECTION_CCW = "Против часовой"
_CUSTOM_COLOR = "Свой цвет…"
_COLOR_TITLE = "Цвет"
_NO_DATA = "Док не ответил на запрос состояния."
_NOT_AVAILABLE = "Док не найден"
_DIRECTION_NOTE = "Не удалось прочитать состояние дока для смены направления."
_MODE_COUNT_NOTE = "Эффекты с номерами выше {count} устройством не поддерживаются."


def _swatch_style(rgb: tuple[int, int, int], selected: bool) -> str:
    """Стиль кнопки-образца цвета; выбранный — с жирной рамкой."""
    border = "3px solid #222" if selected else "1px solid #888"
    r, g, b = rgb
    return (
        f"QPushButton {{ background-color: rgb({r}, {g}, {b}); "
        f"border: {border}; border-radius: 4px; min-width: 26px; max-width: 26px; }}"
    )


def _brightness_caption(level: int) -> str:
    """Подпись уровня яркости: 0 — минимум, 4 — максимум."""
    if level <= BRIGHTNESS_MIN:
        return _BRIGHTNESS_MIN_LABEL
    if level >= BRIGHTNESS_MAX:
        return _BRIGHTNESS_MAX_LABEL
    return _BRIGHTNESS_LEVEL_LABEL.format(level=level)


class DockTab(QWidget):
    """Панель подсветки дока; HID-вызовы только через воркеры."""

    def __init__(
        self,
        manager: DeviceManager,
        workers: WorkerHost,
        window: MainWindow,
    ) -> None:
        super().__init__()
        self._manager = manager
        self._workers = workers
        self._window = window
        self._lighting: DockLighting | None = None
        self._updating = False  # подавление обработчиков при программной установке
        self._mode_buttons: dict[int, QPushButton] = {}
        self._swatches: list[QPushButton] = []

        root = QVBoxLayout(self)
        self._availability = QLabel("")
        self._availability.setVisible(False)
        root.addWidget(self._availability)
        root.addWidget(self._build_switch_row())
        root.addWidget(self._build_effect_group())
        root.addWidget(self._build_tuning_group())
        root.addWidget(self._build_color_group())
        root.addStretch(1)

    # --- построение интерфейса ---

    def _build_switch_row(self) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        self._switch = QCheckBox(_SWITCH_LABEL)
        self._switch.toggled.connect(self._on_switch_toggled)
        layout.addWidget(self._switch)
        layout.addStretch(1)
        return row

    def _build_effect_group(self) -> QGroupBox:
        group = QGroupBox(_EFFECT_TITLE)
        layout = QHBoxLayout(group)
        for value, name in _EFFECT_NAMES.items():
            button = QPushButton(name)
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setToolTip(_MODE_COUNT_NOTE.format(count=value))
            button.clicked.connect(lambda _=False, v=value: self._on_mode_clicked(v))
            self._mode_buttons[value] = button
            layout.addWidget(button)
        return group

    def _build_tuning_group(self) -> QGroupBox:
        group = QGroupBox(_BRIGHTNESS_TITLE + " / " + _SPEED_TITLE)
        layout = QVBoxLayout(group)

        brightness_row = QHBoxLayout()
        brightness_row.addWidget(QLabel(_BRIGHTNESS_TITLE))
        self._brightness = QSlider(Qt.Orientation.Horizontal)
        # Шкала 0..4 измерена на живом доке: байт хранит 0..4, >=5 -> 4.
        self._brightness.setRange(BRIGHTNESS_MIN, BRIGHTNESS_MAX)
        self._brightness.setSingleStep(1)
        self._brightness.setPageStep(1)
        self._brightness.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._brightness.setTickInterval(1)
        self._brightness_label = QLabel(_brightness_caption(0))
        self._brightness.valueChanged.connect(self._on_brightness_moved)
        self._brightness.sliderReleased.connect(self._on_brightness_released)
        brightness_row.addWidget(self._brightness, 1)
        brightness_row.addWidget(self._brightness_label)
        layout.addLayout(brightness_row)

        self._speed_row = QWidget(group)
        speed_layout = QHBoxLayout(self._speed_row)
        speed_layout.setContentsMargins(0, 0, 0, 0)
        speed_layout.addWidget(QLabel(_SPEED_TITLE))
        self._speed = QSlider(Qt.Orientation.Horizontal)
        self._speed.setRange(1, 5)
        self._speed_label = QLabel("1")
        self._speed.valueChanged.connect(self._on_speed_moved)
        self._speed.sliderReleased.connect(self._on_speed_released)
        speed_layout.addWidget(self._speed, 1)
        speed_layout.addWidget(self._speed_label)
        layout.addWidget(self._speed_row)

        self._direction_row = QWidget(group)
        direction_layout = QHBoxLayout(self._direction_row)
        direction_layout.setContentsMargins(0, 0, 0, 0)
        direction_layout.addWidget(QLabel(_DIRECTION_TITLE))
        self._direction_cw = QRadioButton(_DIRECTION_CW)
        self._direction_ccw = QRadioButton(_DIRECTION_CCW)
        self._direction_cw.toggled.connect(lambda on: self._on_direction(0, on))
        self._direction_ccw.toggled.connect(lambda on: self._on_direction(1, on))
        direction_layout.addWidget(self._direction_cw)
        direction_layout.addWidget(self._direction_ccw)
        direction_layout.addStretch(1)
        layout.addWidget(self._direction_row)
        return group

    def _build_color_group(self) -> QGroupBox:
        group = QGroupBox(_COLOR_TITLE)
        layout = QHBoxLayout(group)
        for rgb in PRESET_COLORS:
            button = QPushButton()
            button.setFixedSize(30, 30)
            button.setStyleSheet(_swatch_style(rgb, selected=False))
            button.clicked.connect(lambda _=False, c=rgb: self._on_color_clicked(c))
            self._swatches.append(button)
            layout.addWidget(button)
        self._custom_color = QPushButton(_CUSTOM_COLOR)
        self._custom_color.clicked.connect(self._on_custom_color)
        layout.addWidget(self._custom_color)
        layout.addStretch(1)
        return group

    # --- доступность и данные ---

    def set_available(self, available: bool) -> None:
        """Док присутствует/отсутствует."""
        self._availability.setVisible(not available)
        self._availability.setText(_NOT_AVAILABLE)
        for widget in (
            self._switch,
            self._brightness,
            self._speed,
            self._speed_row,
            self._direction_row,
            self._custom_color,
            *self._mode_buttons.values(),
            *self._swatches,
        ):
            widget.setEnabled(available)

    def refresh(self) -> None:
        """Прочитать read_global_info и отразить фактическое состояние."""

        def job() -> DockLighting | None:
            with self._manager.command_session("dock") as service:
                if service is None:
                    return None
                return service.read_global_info()

        def on_success(info: DockLighting | None) -> None:
            if info is None:
                self._window.note(_NO_DATA)
            self.apply_lighting(info)

        self._workers.run(job, on_success, self._window.report_error, device="dock")

    def apply_lighting(self, info: DockLighting | None) -> None:
        """Отражение снимка DockLighting из устройства."""
        self._lighting = info
        if info is None:
            self._window.note(_NO_DATA)
            return
        self._updating = True
        try:
            self._switch.setChecked(info.enabled)
            for value, button in self._mode_buttons.items():
                button.setVisible(value < info.mode_count)
                button.setChecked(value == info.mode)
            self._mark_swatch(
                info.base_color if effect_allows_custom_color(info.mode) else None
            )
            self._apply_mode_dependent_controls(info)
        finally:
            self._updating = False

    def _apply_mode_dependent_controls(self, info: DockLighting) -> None:
        """Яркость, скорость, направление и цвет в зависимости от эффекта."""
        self._brightness.setValue(clamp_brightness(info.brightness))
        self._brightness_label.setText(_brightness_caption(self._brightness.value()))

        is_wave = info.mode == int(LightMode.WAVE)
        speed_supported = info.mode in (1, 2, 3, 5)
        self._speed_row.setVisible(not is_wave)
        self._speed.setEnabled(speed_supported)
        if speed_supported:
            try:
                slider_value = device_speed_to_slider(info.mode, info.speed)
            except ValueError:
                self._speed.setEnabled(False)
            else:
                self._speed.setValue(slider_value)
                self._speed_label.setText(str(slider_value))

        self._direction_row.setVisible(is_wave)
        if is_wave:
            (
                self._direction_cw if info.direction == 0 else self._direction_ccw
            ).setChecked(True)

        color_allowed = effect_allows_custom_color(info.mode)
        for widget in (self._custom_color, *self._swatches):
            widget.setEnabled(color_allowed)

    def _mark_swatch(self, rgb: tuple[int, int, int] | None) -> None:
        for index, button in enumerate(self._swatches):
            button.setStyleSheet(_swatch_style(PRESET_COLORS[index], selected=False))
        if rgb is None:
            return
        for index, preset in enumerate(PRESET_COLORS):
            if preset == rgb:
                self._swatches[index].setStyleSheet(
                    _swatch_style(preset, selected=True)
                )
                break

    # --- команды ---

    def _on_switch_toggled(self, enabled: bool) -> None:
        if self._updating:
            return

        def job() -> bool:
            with self._manager.command_session("dock") as service:
                if service is None:
                    return False
                service.set_light_switch(enabled)
                return True

        self._workers.run(job, lambda _ok: self.refresh(), device="dock")

    def _on_mode_clicked(self, value: int) -> None:
        if self._updating:
            return

        def job() -> bool:
            with self._manager.command_session("dock") as service:
                if service is None:
                    return False
                service.set_light_mode(value)
                return True

        self._workers.run(job, lambda _ok: self.refresh(), device="dock")

    def _on_brightness_moved(self, value: int) -> None:
        self._brightness_label.setText(_brightness_caption(clamp_brightness(value)))
        if not self._brightness.isSliderDown() and not self._updating:
            self._send_brightness()

    def _on_brightness_released(self) -> None:
        if not self._updating:
            self._send_brightness()

    def _send_brightness(self) -> None:
        mode = self._lighting.mode if self._lighting is not None else 0
        value = clamp_brightness(self._brightness.value())

        def job() -> bool:
            with self._manager.command_session("dock") as service:
                if service is None:
                    return False
                service.set_light_brightness(mode, value)
                return True

        self._workers.run(job, lambda _ok: self.refresh(), device="dock")

    def _on_speed_moved(self, value: int) -> None:
        self._speed_label.setText(str(value))
        if not self._speed.isSliderDown() and not self._updating:
            self._send_speed()

    def _on_speed_released(self) -> None:
        if not self._updating:
            self._send_speed()

    def _send_speed(self) -> None:
        if self._lighting is None:
            return
        mode = self._lighting.mode
        try:
            device_speed = slider_to_device_speed(mode, self._speed.value())
        except ValueError:
            return

        def job() -> bool:
            with self._manager.command_session("dock") as service:
                if service is None:
                    return False
                service.set_light_speed(device_speed)
                return True

        self._workers.run(job, lambda _ok: self.refresh(), device="dock")

    def _on_direction(self, value: int, on: bool) -> None:
        if self._updating or not on:
            return

        def job() -> bool:
            with self._manager.command_session("dock") as service:
                if service is None:
                    return False
                # Атомарный read-modify-write внутри сервиса (одна блокировка).
                return service.set_direction(value)

        self._workers.run(job, lambda _ok: self.refresh(), device="dock")

    def _on_color_clicked(self, rgb: tuple[int, int, int]) -> None:
        if self._updating:
            return
        self._send_color(rgb)

    def _on_custom_color(self) -> None:
        if self._updating:
            return
        initial = (
            QColor(*self._lighting.base_color) if self._lighting else QColor(255, 0, 0)
        )
        color = QColorDialog.getColor(initial, self, _COLOR_TITLE)
        if not color.isValid():
            return
        self._send_color((color.red(), color.green(), color.blue()))

    def _send_color(self, rgb: tuple[int, int, int]) -> None:
        def job() -> bool:
            with self._manager.command_session("dock") as service:
                if service is None:
                    return False
                service.set_base_color(rgb)
                return True

        self._workers.run(job, lambda _ok: self.refresh(), device="dock")
