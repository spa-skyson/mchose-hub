"""Эквалайзер: полосы, пресеты и генерация args для PipeWire filter-chain.

Чистая логика без subprocess — исполняет `dsp.PipeWireDSP`. Формат args —
JSON-объект (SPA JSON, который понимает libpipewire-module-filter-chain):
10 узлов bq_peaking цепочкой, capture/playback привязаны к физическому
sink (target.object) — EQ подключается именно к MCHOSE-карте, а не к
текущему default (PROTOCOL-независимая аудио-логика, см. README §Audio).

Контролы фильтра PipeWire публикует как "<имя узла>:<имя контрола>"
("eq_band_N:Gain") — отдельных "Band N Gain"-имён в фильтр-графе нет;
dsp.set_eq_gains находит фактические имена через enum-params.
"""

from __future__ import annotations

import json
from typing import Any

EQ_BANDS: list[tuple[float, str]] = [
    (31.25, "31 Hz"),
    (62.5, "62 Hz"),
    (125.0, "125 Hz"),
    (250.0, "250 Hz"),
    (500.0, "500 Hz"),
    (1000.0, "1 kHz"),
    (2000.0, "2 kHz"),
    (4000.0, "4 kHz"),
    (8000.0, "8 kHz"),
    (16000.0, "16 kHz"),
]
"""10 полос 10-полосного EQ: (частота Гц, подпись)."""

GAIN_MIN = -12.0
GAIN_MAX = 12.0
BAND_COUNT = len(EQ_BANDS)

PRESETS: dict[str, list[float]] = {
    "Flat": [0.0] * 10,
    "Gaming": [-2.0, -2.0, -2.0, 0.0, 0.0, 0.0, 4.0, 4.0, 4.0, 0.0],
    "Bass Boost": [8.0, 8.0, 8.0, 0.0, 0.0, 0.0, 0.0, -3.0, -3.0, -3.0],
    "Vocal": [0.0, 0.0, 0.0, 0.0, 5.0, 5.0, 5.0, -3.0, -3.0, -3.0],
    "Treble Boost": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 6.0, 6.0, 6.0],
}
"""Имя -> 10 гейнов в дБ; порядок полос — EQ_BANDS."""

EQ_NODE_INPUT = "effect_input.mchose_eq"
EQ_NODE_OUTPUT = "effect_output.mchose_eq"


def sanitize_gains(values: list[float]) -> list[float]:
    """Привести гейны к формату EQ: ровно 10 значений, clamp в [-12, +12] дБ.

    ValueError — если длина не равна 10.
    """
    if len(values) != BAND_COUNT:
        raise ValueError(f"ожидалось {BAND_COUNT} гейнов, получено {len(values)}")
    return [min(GAIN_MAX, max(GAIN_MIN, float(g))) for g in values]


def build_filter_chain_args(bands_gains: list[float], sink_name: str) -> str:
    """Собрать args-JSON для filter-chain EQ-синка.

    Возвращает единую строку (JSON-объект args): filter.graph — 10 узлов
    bq_peaking цепочкой (links), контролы {"Freq", "Q", "Gain"}; capture —
    виртуальный sink (media.class Audio/Sink, node effect_input.mchose_eq),
    playback — пассивный выход, привязанный к sink_name (target.object).
    """
    gains = sanitize_gains(bands_gains)
    nodes = [
        {
            "type": "builtin",
            "name": f"eq_band_{i}",
            "label": "bq_peaking",
            "control": {"Freq": freq, "Q": 1.0, "Gain": gains[i - 1]},
        }
        for i, (freq, _label) in enumerate(EQ_BANDS, start=1)
    ]
    links = [
        {"output": f"eq_band_{i}:Out", "input": f"eq_band_{i + 1}:In"}
        for i in range(1, BAND_COUNT)
    ]
    args: dict[str, Any] = {
        "node.description": "MCHOSE EQ",
        "media.name": "MCHOSE EQ",
        "filter.graph": {"nodes": nodes, "links": links},
        "audio.channels": 2,
        "audio.position": ["FL", "FR"],
        "capture.props": {
            "node.name": EQ_NODE_INPUT,
            "media.class": "Audio/Sink",
        },
        "playback.props": {
            "node.name": EQ_NODE_OUTPUT,
            "node.passive": True,
            "target.object": sink_name,
        },
    }
    return json.dumps(args, separators=(",", ":"))
