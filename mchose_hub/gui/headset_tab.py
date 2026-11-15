"""Вкладка «Наушники»: батарея, режим звука, автоотключение, подсказки, сброс."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..protocol import PROMPT_VOLUME_MAX, AudioMode, BatteryState
from .helpers import (
    battery_caption,
    battery_color,
    clamp_prompt_volume,
    with_transport_retry,
)

if TYPE_CHECKING:
    from .app import DeviceManager
    from .main_window import MainWindow
    from .workers import WorkerHost

_WINDOW_TITLE = "MCHOSE V9 Turbo+"
_BATTERY_PLACEHOLDER = "—"
_NO_LINK = "Нет связи с донглом"
_MODE_TITLE = "Режим звука"
_MODE_NAMES: dict[int, str] = {
    int(AudioMode.GAME_1): "Игра 1",
    int(AudioMode.GAME_2): "Игра 2",
    int(AudioMode.MUSIC): "Музыка",
}
_SHUTDOWN_TITLE = "Автоотключение"
_SHUTDOWN_ITEMS = ((15, "15 минут"), (30, "30 минут"), (45, "45 минут"))
_VOLUME_TITLE = "Громкость голосовых подсказок"
_RESET_BUTTON = "Сброс к заводским настройкам"
_RESET_DETAILS = (
    "Будут восстановлены заводские значения:\n"
    "• эквалайзер — выключен;\n"
    "• Surround — выключен;\n"
    "• режим звука — Игра 1;\n"
    "• подсветка наушников — включена;\n"
    "• эффект подсветки — «Циклическая смена цветов»;\n"
    "• автоотключение — 45 минут;\n"
    "• громкость подсказок — 10."
)
_RESET_CONFIRM_1 = "Выполнить сброс наушников к заводским настройкам?"
_RESET_CONFIRM_2 = "Точно выполнить сброс? Действие необратимо."
_MODE_CONFIRM_NOTE = "Наушники не подтвердили режим — повтор при следующем опросе."
_SET_CONFIRM_NOTE = "Установлено, но подтвердить чтением не удалось."
_SET_REJECTED_NOTE = "Устройство не подтвердило установку."
_RESET_DONE = "Сброс выполнен."
_RESET_NOT_CONFIRMED = "Устройство не подтвердило сброс."
_RESET_NO_SERVICE = "Донгл недоступен — сброс не выполнен."


def _mark_active(buttons: dict[int, QPushButton], active: int) -> None:
    """Подчёркивание активного режима, снятие с остальных."""
    for value, button in buttons.items():
        checked = value == active
        button.setChecked(checked)
        font = button.font()
        font.setUnderline(checked)
        button.setFont(font)


class HeadsetTab(QWidget):
    """Панель наушников; HID-вызовы только через воркеры."""

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
        self._mode_buttons: dict[int, QPushButton] = {}

        root = QVBoxLayout(self)
        root.addWidget(self._build_battery_card())
        root.addWidget(self._build_mode_group())
        root.addWidget(self._build_settings_group())
        root.addWidget(self._build_reset_button())
        root.addStretch(1)

    # --- построение интерфейса ---

    def _build_battery_card(self) -> QGroupBox:
        card = QGroupBox("Батарея")
        layout = QHBoxLayout(card)
        self._battery_value = QLabel(_BATTERY_PLACEHOLDER)
        self._battery_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = self._battery_value.font()
        font.setPointSize(32)
        font.setBold(True)
        self._battery_value.setFont(font)
        self._battery_caption = QLabel(_NO_LINK)
        self._battery_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._battery_value)
        layout.addWidget(self._battery_caption, 1)
        return card

    def _build_mode_group(self) -> QGroupBox:
        group = QGroupBox(_MODE_TITLE)
        layout = QHBoxLayout(group)
        for value, name in _MODE_NAMES.items():
            button = QPushButton(name)
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.clicked.connect(lambda _=False, v=value: self._on_mode_clicked(v))
            self._mode_buttons[value] = button
            layout.addWidget(button)
        return group

    def _build_settings_group(self) -> QGroupBox:
        group = QGroupBox("Настройки")
        layout = QVBoxLayout(group)

        shutdown_row = QHBoxLayout()
        shutdown_row.addWidget(QLabel(_SHUTDOWN_TITLE))
        self._shutdown_combo = QComboBox()
        for minutes, label in _SHUTDOWN_ITEMS:
            self._shutdown_combo.addItem(label, minutes)
        self._shutdown_combo.currentIndexChanged.connect(self._on_shutdown_changed)
        shutdown_row.addWidget(self._shutdown_combo)
        shutdown_row.addStretch(1)
        layout.addLayout(shutdown_row)

        volume_row = QHBoxLayout()
        volume_row.addWidget(QLabel(_VOLUME_TITLE))
        self._volume_slider = QSlider(Qt.Orientation.Horizontal)
        self._volume_slider.setRange(0, PROMPT_VOLUME_MAX)
        self._volume_label = QLabel("0")
        self._volume_slider.valueChanged.connect(self._on_volume_moved)
        self._volume_slider.sliderReleased.connect(self._on_volume_released)
        volume_row.addWidget(self._volume_slider, 1)
        volume_row.addWidget(self._volume_label)
        layout.addLayout(volume_row)
        return group

    def _build_reset_button(self) -> QPushButton:
        button = QPushButton(_RESET_BUTTON)
        button.clicked.connect(self._on_reset_clicked)
        return button

    # --- обновление состояния ---

    def set_available(self, available: bool) -> None:
        """Донгл присутствует/отсутствует."""
        for widget in (self._shutdown_combo, self._volume_slider):
            widget.setEnabled(available)
        if not available:
            self._battery_value.setText(_BATTERY_PLACEHOLDER)
            self._battery_value.setStyleSheet("")
            self._battery_caption.setText(_NO_LINK)
            _mark_active(self._mode_buttons, -1)

    def apply_battery(self, battery: BatteryState) -> None:
        """Батарея из poll_battery(); вызывается даже при молчащем режиме."""
        self._battery_value.setText(f"{battery.percent}%")
        self._battery_value.setStyleSheet(f"color: {battery_color(battery.percent)};")
        self._battery_caption.setText(battery_caption(battery))

    def apply_mode(self, mode: AudioMode) -> None:
        """Режим звука из poll_audio_mode()/read-back."""
        _mark_active(self._mode_buttons, int(mode))

    def mark_mode_unknown(self) -> None:
        """Режим неизвестен: снять выделение и подчёркивание."""
        _mark_active(self._mode_buttons, -1)

    def apply_shutdown(self, minutes: int | None) -> None:
        """Отражение фактического автоотключения (read-back)."""
        if minutes is None:
            return
        index = self._shutdown_combo.findData(minutes)
        if index >= 0:
            self._shutdown_combo.blockSignals(True)
            self._shutdown_combo.setCurrentIndex(index)
            self._shutdown_combo.blockSignals(False)

    def apply_volume(self, level: int | None) -> None:
        """Отражение фактической громкости подсказок (read-back)."""
        if level is None:
            return
        self._volume_slider.blockSignals(True)
        self._volume_slider.setValue(clamp_prompt_volume(level))
        self._volume_slider.blockSignals(False)
        self._volume_label.setText(str(clamp_prompt_volume(level)))

    def request_audio_mode(self, value: int) -> None:
        """Установка режима (из вкладки и из трея) + отражение read-back."""

        def job() -> AudioMode | None:
            with self._manager.command_session("headset") as service:
                if service is None:
                    return None
                return service.set_audio_mode(AudioMode(value))

        def on_success(actual: AudioMode | None) -> None:
            if actual is None:
                self._window.note(_MODE_CONFIRM_NOTE)
            else:
                _mark_active(self._mode_buttons, int(actual))
            self._window.poll_now()

        # Отравленный транспорт/EOF: пере-скан + одноразовый ретрай команды.
        self._workers.run(
            with_transport_retry(
                lambda: self._set_audio_mode_job(value),
                self._window.recover_transport,
            ),
            on_success,
            device="headset",
        )

    def _set_audio_mode_job(self, value: int) -> AudioMode | None:
        with self._manager.command_session("headset") as service:
            if service is None:
                return None
            return service.set_audio_mode(AudioMode(value))

    def request_shutdown(self, minutes: int) -> None:
        """Установка автоотключения + подтверждение чтением."""

        def job() -> tuple[bool, int | None]:
            with self._manager.command_session("headset") as service:
                if service is None:
                    return False, None
                accepted = service.set_auto_shutdown(minutes)
                return accepted, service.get_auto_shutdown()

        def on_success(result: tuple[bool, int | None]) -> None:
            accepted, minutes_actual = result
            if not accepted:
                self._window.note(_SET_REJECTED_NOTE)
            elif minutes_actual is None:
                self._window.note(_SET_CONFIRM_NOTE)
            self.apply_shutdown(minutes_actual)
            self._window.tray_update_shutdown(minutes_actual)

        self._workers.run(
            with_transport_retry(job, self._window.recover_transport),
            on_success,
            device="headset",
        )

    def reload_settings(self) -> None:
        """Перечитать автоотключение и громкость из устройства."""

        def job() -> tuple[int | None, int | None]:
            with self._manager.command_session("headset") as service:
                if service is None:
                    return None, None
                return service.get_auto_shutdown(), service.get_prompt_volume()

        def on_success(result: tuple[int | None, int | None]) -> None:
            shutdown, volume = result
            self.apply_shutdown(shutdown)
            self.apply_volume(volume)
            self._window.tray_update_shutdown(shutdown)

        self._workers.run(job, on_success, device="headset")

    # --- обработчики ---

    def _on_mode_clicked(self, value: int) -> None:
        self.request_audio_mode(value)

    def _on_shutdown_changed(self, index: int) -> None:
        minutes = self._shutdown_combo.itemData(index)
        if isinstance(minutes, int):
            self.request_shutdown(minutes)

    def _on_volume_moved(self, value: int) -> None:
        level = clamp_prompt_volume(value)
        self._volume_label.setText(str(level))
        if not self._volume_slider.isSliderDown():
            self._send_volume(level)

    def _on_volume_released(self) -> None:
        self._send_volume(clamp_prompt_volume(self._volume_slider.value()))

    def _send_volume(self, level: int) -> None:
        def job() -> int | None:
            with self._manager.command_session("headset") as service:
                if service is None:
                    return None
                if not service.set_prompt_volume(level):
                    return None
                return service.get_prompt_volume()

        def on_success(actual: int | None) -> None:
            if actual is None:
                self._window.note(_SET_CONFIRM_NOTE)
            else:
                self.apply_volume(actual)

        self._workers.run(
            with_transport_retry(job, self._window.recover_transport),
            on_success,
            device="headset",
        )

    def _on_reset_clicked(self) -> None:
        if not self._confirm_reset():
            return

        def job() -> tuple[bool, bool]:
            with self._manager.command_session("headset") as service:
                if service is None:
                    return False, False
                return True, service.factory_reset()

        def on_success(result: tuple[bool, bool]) -> None:
            performed, accepted = result
            if not performed:
                self._window.note(_RESET_NO_SERVICE)
            elif accepted:
                self._window.note(_RESET_DONE)
            else:
                self._window.note(_RESET_NOT_CONFIRMED)
            self._window.poll_now()
            self.reload_settings()

        self._workers.run(
            with_transport_retry(job, self._window.recover_transport),
            on_success,
            device="headset",
        )

    def _confirm_reset(self) -> bool:
        first = QMessageBox.warning(
            self,
            _RESET_BUTTON,
            _RESET_CONFIRM_1 + "\n\n" + _RESET_DETAILS,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if first != QMessageBox.StandardButton.Yes:
            return False
        second = QMessageBox.question(
            self,
            _RESET_BUTTON,
            _RESET_CONFIRM_2,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return second == QMessageBox.StandardButton.Yes
