"""Вкладка «Звук»: EQ, шумоподавление микрофона и мониторинг через PipeWire.

Аудио-операции идут через воркеры с ключом device="audio" — только
subprocess (PipeWireDSP), без command_session/транспортного владения.
Все аудио-джобы сериализуются одним ключом: новый запрос становится в
pending-слот (последний выигрывает), шторм слайдеров недопустим —
set_eq_gains пересоздаёт фильтр.

Стартовое отражение: чекбоксы/слайдеры по dsp.state; живые факты
(нода в графе, поддержка RNNoise) доуточняются воркером. Автоприменения
фильтров при старте нет — только отражение факта; state молча поправляется,
если фильтра в графе нет.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..audio import (
    EQ_BANDS,
    EQ_NODE_INPUT,
    NR_NODE_OUTPUT,
    PRESETS,
    AudioError,
    PipeWireDSP,
)
from .helpers import (
    CUSTOM_PRESET,
    EQ_SLIDER_MAX,
    EQ_SLIDER_MIN,
    Debounce,
    gain_to_slider,
    preset_for_gains,
    slider_to_gain,
)

if TYPE_CHECKING:
    from .main_window import MainWindow
    from .workers import WorkerHost

_T = TypeVar("_T")


_DEBOUNCE_TICK_MS = 100
_NR_HINT = "Требуется пакет noise-suppression-for-voice (AUR)"
_EQ_ON = "Эквалайзер вкл"
_EQ_OFF = "выкл"
_NR_ON = "вкл"
_MONITOR_TITLE = "Мониторинг в наушниках"
_MUTE_BUTTON = "Заглушить"
_UNMUTE_BUTTON = "Включить"
_MIC_TITLE = "Громкость микрофона"


class SoundTab(QWidget):
    """Панель звука: EQ/NR/микрофон; PipeWire-вызовы только через воркеры."""

    def __init__(self, workers: WorkerHost, window: MainWindow) -> None:
        super().__init__()
        self._workers = workers
        self._window = window
        self._dsp = PipeWireDSP()
        self._debounce = Debounce()

        root = QVBoxLayout(self)
        root.addWidget(self._build_eq_group())
        root.addWidget(self._build_nr_group())
        root.addWidget(self._build_mic_group())
        self._status = QLabel("", self)
        self._status.setWordWrap(True)
        root.addWidget(self._status)
        root.addStretch(1)

        self._apply_stored_state()
        self._refresh_live()

        self._tick = QTimer(self)
        self._tick.setInterval(_DEBOUNCE_TICK_MS)
        self._tick.timeout.connect(self._on_debounce_tick)
        self._tick.start()

    # --- построение интерфейса ---

    def _build_eq_group(self) -> QGroupBox:
        group = QGroupBox("Эквалайзер")
        layout = QVBoxLayout(group)

        top = QHBoxLayout()
        self._eq_check = QCheckBox(_EQ_ON, group)
        self._eq_check.toggled.connect(self._on_eq_toggled)
        top.addWidget(self._eq_check)
        top.addWidget(QLabel("Пресет:", group))
        self._preset_combo = QComboBox(group)
        for name in PRESETS:
            self._preset_combo.addItem(name)
        self._preset_combo.addItem(CUSTOM_PRESET)
        self._preset_combo.currentIndexChanged.connect(self._on_preset_selected)
        top.addWidget(self._preset_combo)
        top.addStretch(1)
        layout.addLayout(top)

        bands = QGridLayout()
        self._band_sliders: list[QSlider] = []
        self._band_values: list[QLabel] = []
        for i, (_freq, caption) in enumerate(EQ_BANDS):
            bands.addWidget(QLabel(caption, group), i, 0)
            slider = QSlider(Qt.Orientation.Horizontal, group)
            slider.setRange(EQ_SLIDER_MIN, EQ_SLIDER_MAX)
            slider.valueChanged.connect(self._on_band_moved)
            self._band_sliders.append(slider)
            bands.addWidget(slider, i, 1)
            value = QLabel("0.0 дБ", group)
            self._band_values.append(value)
            bands.addWidget(value, i, 2)
        layout.addLayout(bands)
        return group

    def _build_nr_group(self) -> QGroupBox:
        group = QGroupBox("Шумоподавление микрофона")
        layout = QVBoxLayout(group)
        self._nr_check = QCheckBox("RNNoise шумоподавление", group)
        self._nr_check.setEnabled(False)  # до подтверждения поддержки воркером
        self._nr_check.toggled.connect(self._on_nr_toggled)
        layout.addWidget(self._nr_check)
        self._nr_hint = QLabel(_NR_HINT, group)
        self._nr_hint.setWordWrap(True)
        self._nr_hint.setVisible(False)
        layout.addWidget(self._nr_hint)
        return group

    def _build_mic_group(self) -> QGroupBox:
        group = QGroupBox("Микрофон")
        layout = QVBoxLayout(group)

        volume_row = QHBoxLayout()
        volume_row.addWidget(QLabel(_MIC_TITLE, group))
        self._mic_slider = QSlider(Qt.Orientation.Horizontal, group)
        self._mic_slider.setRange(0, 100)
        self._mic_value = QLabel("—", group)
        self._mic_slider.valueChanged.connect(self._on_mic_moved)
        self._mic_slider.sliderReleased.connect(self._on_mic_released)
        volume_row.addWidget(self._mic_slider, 1)
        volume_row.addWidget(self._mic_value)
        layout.addLayout(volume_row)

        buttons_row = QHBoxLayout()
        self._mute_button = QPushButton(_MUTE_BUTTON, group)
        self._mute_button.clicked.connect(self._on_mute_clicked)
        buttons_row.addWidget(self._mute_button)
        self._monitor_check = QCheckBox(_MONITOR_TITLE, group)
        self._monitor_check.toggled.connect(self._on_monitor_toggled)
        buttons_row.addWidget(self._monitor_check)
        buttons_row.addStretch(1)
        layout.addLayout(buttons_row)
        return group

    # --- стартовое отражение (только state, без применения фильтров) ---

    def _apply_stored_state(self) -> None:
        """Гейны/подписи из dsp.state (дёшево, без subprocess)."""
        stored = self._dsp.state.get("gains")
        gains = (
            [float(g) for g in stored]
            if isinstance(stored, list) and len(stored) == len(EQ_BANDS)
            else PRESETS["Flat"]
        )
        self._set_band_sliders(gains)
        self._update_band_labels()
        self._reflect_preset()

    def _refresh_live(self) -> None:
        """Живые факты графа воркером: чекбоксы — по факту, state — молча поправить."""

        def job() -> dict[str, object]:
            dsp = self._dsp
            try:
                mic = dsp.mic_volume()
            except AudioError:
                mic = None
            return {
                "eq_loaded": dsp.eq_loaded(),
                "nr_supported": dsp.nr_supported(),
                "nr_loaded": dsp.nr_loaded(),
                "monitoring": dsp.monitoring_active(),
                "mic_volume": mic,
            }

        self._run_audio_job(job, self._apply_live, self._window.report_error)

    def _fix_state_flag(self, key: str, live: bool) -> None:
        """State говорит «вкл», а в графе факта нет — молча поправить."""
        if self._dsp.state.get(key) and not live:
            self._dsp.state[key] = False
            self._dsp.save_state()

    def _apply_live(self, live: dict[str, object]) -> None:
        eq_on = bool(live["eq_loaded"])
        nr_on = bool(live["nr_loaded"])
        mon_on = bool(live["monitoring"])
        self._fix_state_flag("eq_enabled", eq_on)
        self._fix_state_flag("nr_enabled", nr_on)
        self._fix_state_flag("monitor_enabled", mon_on)

        nr_supported = bool(live["nr_supported"])
        for check, checked in (
            (self._eq_check, eq_on),
            (self._nr_check, nr_on),
            (self._monitor_check, mon_on),
        ):
            check.blockSignals(True)
            check.setChecked(checked)
            check.blockSignals(False)
        self._nr_check.setEnabled(nr_supported)
        self._nr_hint.setVisible(not nr_supported)

        mic = live["mic_volume"]
        if isinstance(mic, int):
            self._mic_slider.blockSignals(True)
            self._mic_slider.setValue(mic)
            self._mic_slider.blockSignals(False)
            self._mic_value.setText(f"{mic}%")

        self._update_status(eq_on, nr_on, mon_on)

    # --- статусы ---

    def _update_status(self, eq_on: bool, nr_on: bool, mon_on: bool) -> None:
        parts = [
            f"EQ: {EQ_NODE_INPUT if eq_on else _EQ_OFF}",
            f"NR: {NR_NODE_OUTPUT if nr_on else _EQ_OFF}",
            f"Мониторинг: {_NR_ON if mon_on else _EQ_OFF}",
        ]
        self._status.setText(" | ".join(parts))

    # --- слайдеры/пресеты EQ ---

    def _current_gains(self) -> list[float]:
        return [slider_to_gain(s.value()) for s in self._band_sliders]

    def _set_band_sliders(self, gains: list[float]) -> None:
        for slider, gain in zip(self._band_sliders, gains):
            slider.blockSignals(True)
            slider.setValue(gain_to_slider(gain))
            slider.blockSignals(False)

    def _update_band_labels(self) -> None:
        for label, gain in zip(self._band_values, self._current_gains()):
            label.setText(f"{gain:+.1f} дБ")

    def _reflect_preset(self) -> None:
        """Комбобокс — на совпавший пресет, иначе «Свои» (без сигналов)."""
        index = self._preset_combo.findText(preset_for_gains(self._current_gains()))
        if index >= 0:
            self._preset_combo.blockSignals(True)
            self._preset_combo.setCurrentIndex(index)
            self._preset_combo.blockSignals(False)

    def _on_band_moved(self, _value: int) -> None:
        self._update_band_labels()
        self._reflect_preset()
        self._debounce.touch(time.monotonic())

    def _on_preset_selected(self, index: int) -> None:
        name = self._preset_combo.itemText(index)
        if name == CUSTOM_PRESET or name not in PRESETS:
            return  # «Свои» — след ручных правок, применять нечего
        gains = [float(g) for g in PRESETS[name]]
        self._set_band_sliders(gains)
        self._update_band_labels()

        def apply_preset() -> None:
            # Живой EQ — обновить фильтр; выключенный — сохранить гейны в state.
            self._dsp.set_eq_gains(gains)

        self._run_audio_job(
            apply_preset,
            lambda _r: self._window.note(f"Пресет применён: {name}"),
            self._window.report_error,
        )

    def _on_debounce_tick(self) -> None:
        if not self._debounce.take(time.monotonic()):
            return
        gains = self._current_gains()
        self._run_audio_job(
            lambda: self._dsp.set_eq_gains(gains),
            None,
            self._window.report_error,
        )

    # --- EQ/NR/микрофон/мониторинг: тумблеры ---

    def _revert_check(self, check: QCheckBox) -> Callable[[str, str], None]:
        """Колбэк провала: вернуть чекбокс и показать плашку с подсказкой."""

        def on_failure(message: str, hint: str) -> None:
            check.blockSignals(True)
            check.setChecked(not check.isChecked())
            check.blockSignals(False)
            self._window.report_error(message, hint)

        return on_failure

    def _on_eq_toggled(self, checked: bool) -> None:
        if checked:
            gains = self._current_gains()
            self._run_audio_job(
                lambda: self._dsp.enable_eq(gains),
                lambda _name: self._update_status(
                    True, self._nr_check.isChecked(), self._monitor_check.isChecked()
                ),
                self._revert_check(self._eq_check),
            )
        else:
            self._run_audio_job(
                self._dsp.disable_eq,
                lambda _name: self._update_status(
                    False, self._nr_check.isChecked(), self._monitor_check.isChecked()
                ),
                self._revert_check(self._eq_check),
            )

    def _on_nr_toggled(self, checked: bool) -> None:
        if checked:
            self._run_audio_job(
                self._dsp.enable_nr,
                lambda _name: self._update_status(
                    self._eq_check.isChecked(), True, self._monitor_check.isChecked()
                ),
                self._revert_check(self._nr_check),
            )
        else:
            self._run_audio_job(
                self._dsp.disable_nr,
                lambda _name: self._update_status(
                    self._eq_check.isChecked(), False, self._monitor_check.isChecked()
                ),
                self._revert_check(self._nr_check),
            )

    def _on_monitor_toggled(self, checked: bool) -> None:
        if checked:
            self._run_audio_job(
                self._dsp.monitoring_enable,
                lambda _route: self._update_status(
                    self._eq_check.isChecked(), self._nr_check.isChecked(), True
                ),
                self._revert_check(self._monitor_check),
            )
        else:
            self._run_audio_job(
                self._dsp.monitoring_disable,
                lambda _msg: self._update_status(
                    self._eq_check.isChecked(), self._nr_check.isChecked(), False
                ),
                self._revert_check(self._monitor_check),
            )

    # --- микрофон: громкость и mute ---

    def _on_mic_moved(self, value: int) -> None:
        self._mic_value.setText(f"{value}%")
        if not self._mic_slider.isSliderDown():
            self._send_mic_volume(value)

    def _on_mic_released(self) -> None:
        self._send_mic_volume(self._mic_slider.value())

    def _send_mic_volume(self, percent: int) -> None:
        self._run_audio_job(
            lambda: self._dsp.mic_volume(percent),
            lambda actual: self._mic_value.setText(f"{actual}%"),
            self._window.report_error,
        )

    def _on_mute_clicked(self) -> None:
        muting = self._mute_button.text() == _MUTE_BUTTON

        def on_success(_name: str) -> None:
            self._mute_button.setText(_UNMUTE_BUTTON if muting else _MUTE_BUTTON)

        job = self._dsp.mic_mute if muting else self._dsp.mic_unmute
        self._run_audio_job(job, on_success, self._window.report_error)

    # --- общее ---

    def _run_audio_job(
        self,
        job: Callable[[], _T],
        on_success: Callable[[_T], None] | None,
        on_failure: Callable[[str, str], None] | None,
    ) -> None:
        """Аудио-джоб: только subprocess, без command_session; ключ device="audio"."""
        self._workers.run(job, on_success, on_failure, device="audio")
