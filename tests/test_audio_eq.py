"""Тесты eq.py: полосы, пресеты, генерация args filter-chain (чистая логика)."""

from __future__ import annotations

import json

import pytest

from mchose_hub.audio.eq import (
    EQ_BANDS,
    EQ_NODE_INPUT,
    EQ_NODE_OUTPUT,
    GAIN_MAX,
    GAIN_MIN,
    PRESETS,
    build_filter_chain_args,
    sanitize_gains,
)


def test_eq_bands_labels() -> None:
    """10 полос от 31 Hz до 16 kHz с подписями."""
    assert len(EQ_BANDS) == 10
    assert EQ_BANDS[0] == (31.25, "31 Hz")
    assert EQ_BANDS[-1] == (16000.0, "16 kHz")
    assert [label for _freq, label in EQ_BANDS] == [
        "31 Hz",
        "62 Hz",
        "125 Hz",
        "250 Hz",
        "500 Hz",
        "1 kHz",
        "2 kHz",
        "4 kHz",
        "8 kHz",
        "16 kHz",
    ]


def test_sanitize_gains_clamps() -> None:
    """Гейны clamp в [-12, +12] дБ; числа приводятся к float."""
    assert sanitize_gains([-20.0, 20.0, 0, 0, 0, 0, 0, 0, 0, 5.5]) == [
        -12.0,
        12.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        5.5,
    ]
    assert sanitize_gains([GAIN_MIN] * 10) == [-12.0] * 10
    assert sanitize_gains([GAIN_MAX] * 10) == [12.0] * 10


@pytest.mark.parametrize("count", [9, 11, 0])
def test_sanitize_gains_wrong_length(count: int) -> None:
    """Длина гейнов строго 10: иначе ValueError."""
    with pytest.raises(ValueError, match="10"):
        sanitize_gains([0.0] * count)


def test_presets_shape() -> None:
    """Пресеты: 5 штук, длина 10, значения в границах."""
    assert list(PRESETS) == ["Flat", "Gaming", "Bass Boost", "Vocal", "Treble Boost"]
    for gains in PRESETS.values():
        assert len(gains) == 10
        assert all(GAIN_MIN <= g <= GAIN_MAX for g in gains)


def test_preset_values() -> None:
    """Ключевые значения пресетов по полосам."""
    assert PRESETS["Flat"] == [0.0] * 10
    gaming = PRESETS["Gaming"]
    assert gaming[6:9] == [4.0, 4.0, 4.0]  # 2k/4k/8k — шаги
    assert all(g == -2.0 for g in gaming[:3])  # 125 Hz и ниже приглушены
    assert PRESETS["Bass Boost"][:3] == [8.0, 8.0, 8.0]
    assert PRESETS["Bass Boost"][7:] == [-3.0, -3.0, -3.0]
    assert PRESETS["Vocal"][4:7] == [5.0, 5.0, 5.0]
    assert PRESETS["Treble Boost"][7:] == [6.0, 6.0, 6.0]


def test_build_filter_chain_args_json() -> None:
    """args парсится как JSON: 10 bq_peaking узлов цепочкой (9 links)."""
    args = json.loads(build_filter_chain_args([0.0] * 10, "alsa_out"))
    graph = args["filter.graph"]
    assert len(graph["nodes"]) == 10
    assert all(node["label"] == "bq_peaking" for node in graph["nodes"])
    assert [node["name"] for node in graph["nodes"]] == [
        f"eq_band_{i}" for i in range(1, 11)
    ]
    assert len(graph["links"]) == 9
    assert graph["links"][0] == {"output": "eq_band_1:Out", "input": "eq_band_2:In"}


def test_build_filter_chain_args_controls() -> None:
    """Контролы узлов: частота полосы, Q=1.0, гейн по индексу."""
    gains = [1.0, -2.0, 3.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 7.5]
    args = json.loads(build_filter_chain_args(gains, "alsa_out"))
    nodes = args["filter.graph"]["nodes"]
    for i, node in enumerate(nodes):
        control = node["control"]
        assert control["Freq"] == EQ_BANDS[i][0]
        assert control["Q"] == 1.0
        assert control["Gain"] == gains[i]


def test_build_filter_chain_args_binding() -> None:
    """Capture — виртуальный sink; playback — пассивный выход на sink_name."""
    args = json.loads(build_filter_chain_args([0.0] * 10, "alsa_out.mchose"))
    assert args["capture.props"] == {
        "node.name": EQ_NODE_INPUT,
        "media.class": "Audio/Sink",
    }
    assert args["playback.props"]["node.name"] == EQ_NODE_OUTPUT
    assert args["playback.props"]["node.passive"] is True
    assert args["playback.props"]["target.object"] == "alsa_out.mchose"
    assert args["node.description"] == "MCHOSE EQ"
    assert args["audio.position"] == ["FL", "FR"]


def test_build_filter_chain_args_rejects_bad_gains() -> None:
    """Неверная длина гейнов — ValueError, args не строится."""
    with pytest.raises(ValueError):
        build_filter_chain_args([0.0] * 9, "alsa_out")
