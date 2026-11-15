"""Аудио-DSP слой: EQ, шумоподавление и мониторинг через PipeWire."""

from __future__ import annotations

from .dsp import NR_NODE_OUTPUT, AudioError, PipeWireDSP
from .eq import (
    EQ_BANDS,
    EQ_NODE_INPUT,
    EQ_NODE_OUTPUT,
    GAIN_MAX,
    GAIN_MIN,
    PRESETS,
    build_filter_chain_args,
    sanitize_gains,
)

__all__ = [
    "EQ_BANDS",
    "EQ_NODE_INPUT",
    "EQ_NODE_OUTPUT",
    "GAIN_MAX",
    "GAIN_MIN",
    "NR_NODE_OUTPUT",
    "PRESETS",
    "AudioError",
    "PipeWireDSP",
    "build_filter_chain_args",
    "sanitize_gains",
]
