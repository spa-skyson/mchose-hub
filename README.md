# MCHOSE Hub

An unofficial Linux control hub for the MCHOSE V9 Turbo+ headset and its RGB dock.

## Features

| Device | Controls |
|---|---|
| V9 Turbo+ headset (2.4 GHz dongle) | Battery and charging state; Game 1, Game 2, and Music modes; 15/30/45-minute auto-shutdown; voice-prompt volume; factory reset. |
| RGB dock | Six lighting effects; on/off; brightness, speed, base colors, Flowing Wave direction, music-reactive mode, sleep settings, and firmware/device information. |

The PyQt6 application provides a window and system tray controls, and displays headset and dock firmware versions. Its **Sound** tab provides a 10-band host-side equalizer (31 Hz–16 kHz, ±12 dB), presets, microphone noise suppression, microphone volume/mute, and microphone monitoring in the headset. A command-line interface is included for diagnostics and direct control.

## Requirements

- Linux with `hidraw` support.
- Python 3.10 or newer.
- PyQt6 for the graphical application.
- Access to the relevant `/dev/hidraw*` device nodes (see [Installation](#installation)).

## Installation

Clone the repository and enter its directory:

```bash
git clone https://github.com/spa-skyson/mchose-hub.git
cd mchose-hub
```

Install PyQt6:

```bash
python3 -m pip install PyQt6
```

Grant access to the supported HID devices by installing the repository's udev rules:

```bash
sudo install -m 0644 packaging/99-mchose-hub.rules /etc/udev/rules.d/99-mchose-hub.rules
sudo udevadm control --reload
sudo udevadm trigger
```

Reconnect the dongle and dock after reloading the rules. Alternatively, install the udev rules and desktop-menu entry with the optional installer:

```bash
bash packaging/install.sh
```

The installer uses `sudo` when needed. To also install the optional `~/.local/bin/mchose-hub` launcher, run:

```bash
bash packaging/install.sh --entrypoint
```

## Usage

Start the GUI from the repository directory:

```bash
python3 -m mchose_hub.gui
```

The CLI help lists all available commands and arguments:

```bash
python3 -m mchose_hub.cli --help
```

Examples:

```bash
python3 -m mchose_hub.cli status
python3 -m mchose_hub.cli mode music
python3 -m mchose_hub.cli light info
python3 -m mchose_hub.cli eq preset Gaming
python3 -m mchose_hub.cli eq set 6 3
python3 -m mchose_hub.cli eq status
python3 -m mchose_hub.cli mic volume 75
python3 -m mchose_hub.cli mic mute
python3 -m mchose_hub.cli mic unmute
python3 -m mchose_hub.cli mic nr on
python3 -m mchose_hub.cli mic monitor on
python3 -m mchose_hub.cli mic status
```

Example output (values depend on the connected devices):

```text
Найдено: /dev/hidraw3 [3837:600a] — донгл 2.4G (управляется)
Батарея: 100% (разряд)
Режим звука: Music
Найдено: /dev/hidraw4 [3837:6008] — док RGB (управляется)
Док: MCHOSE, прошивка: 7.0.0
```

```text
Подсветка: вкл
Режим: 0 — Constant On
Эффектов в устройстве: 6
Скорость: 3, яркость: 4
Светомузыка: выкл
Базовый цвет: RGB(255, 0, 0)
Направление Flowing Wave: по часовой
Эффекты:
  0 — Constant On
  1 — Breathing
  2 — Starry Night
  3 — Cyclic Color Change
  4 — Flowing Wave
  5 — Music Rhythm
```

The CLI also supports `shutdown`, `prompt`, and `light on|off|mode|color|brightness|speed|direction`. EQ commands are `eq on|off|preset <name>|set <band> <gain>|status`; presets are `Flat`, `Gaming`, `Bass Boost`, `Vocal`, `Treble Boost`, plus the genre presets `Rock`, `Pop`, `Jazz`, `Classical`, `Electronic`, `Hip-Hop`, `Acoustic`, and `Speech`. Microphone commands are `mic volume [0–100]`, `mute`, `unmute`, `nr on|off`, `monitor on|off`, and `status`. Run `python3 -m mchose_hub.cli <command> --help` or `python3 -m mchose_hub.cli <command> <subcommand> --help` for command-specific arguments.

## Troubleshooting

- **Permission denied opening a HID device:** install `packaging/99-mchose-hub.rules`, run the `udevadm` commands above, then reconnect the device. Check that `/dev/hidraw*` is present and accessible to your user.
- **The headset does not respond:** confirm that it is powered on and connected over the 2.4 GHz dongle. The headset command channel can park after inactivity; reconnect the dongle physically (unplug and plug it back in). See [PROTOCOL.md §7.1](PROTOCOL.md#71-keepalive-поведение-командного-канала-наушников--эмпирика-2026-11-15).
- **EQ is not audible:** enable EQ and select `effect_input.mchose_eq` as the playback output in the application. `eq on` enables the filter-chain and makes its virtual sink the default; the filter-chain plays through the MCHOSE headset sink.
- **RNNoise is unavailable:** install the optional `noise-suppression-for-voice` package (AUR on Arch Linux). The application detects whether the RNNoise LADSPA plugin is available.
- **The microphone does not respond:** make sure the MCHOSE headset microphone is active and available as a PipeWire source.

## How it works

The HID protocol was reverse-engineered from the official M HUB software. The 2.4 GHz headset dongle uses USB ID `3837:600a`; the RGB dock uses `3837:6008`. Headset commands require opening and closing the command channel with `SET_FEATURE` report `0xAA` (UNLOCK/LOCK). See [PROTOCOL.md](PROTOCOL.md) for the protocol details. EQ and noise suppression are host-side PipeWire filter-chain effects; EQ is exposed as the virtual sink `effect_input.mchose_eq` and routed to the MCHOSE headset sink.

## Limitations

- Firmware OTA updates are not implemented.
- There is no Bluetooth transport; headset control uses the 2.4 GHz dongle.
- EQ and microphone noise-suppression state is stored in `~/.config/mchose-hub/audio.json`; processing runs on the host through PipeWire, not in the headset. EQ uses PipeWire's built-in biquad filters. RNNoise requires the optional `noise-suppression-for-voice` package (AUR on Arch Linux).
- There is no separate external package requirement for EQ/DSP beyond PipeWire's command-line tools (`pactl`, `wpctl`, `pw-cli`, and `pw-dump`).
- AUR package templates are provided in [`packaging/AUR/`](packaging/AUR/), for `mchose-hub-git` and the stable `mchose-hub` package. See [`packaging/AUR/README.md`](packaging/AUR/README.md) for publishing instructions.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).

## README на русском

Неофициальный Linux-хаб для MCHOSE V9 Turbo+ и RGB-дока. Нужны Linux с `hidraw`, Python 3.10+ и PyQt6:

```bash
git clone https://github.com/spa-skyson/mchose-hub.git
cd mchose-hub
python3 -m pip install PyQt6
sudo install -m 0644 packaging/99-mchose-hub.rules /etc/udev/rules.d/99-mchose-hub.rules
sudo udevadm control --reload
sudo udevadm trigger
python3 -m mchose_hub.gui
```

Также можно установить правила и пункт меню командой `bash packaging/install.sh`.

Хаб показывает батарею и версии прошивок, переключает режимы Game 1/Game 2/Music, автоотключение и громкость голосовых подсказок. Для дока доступны шесть эффектов подсветки, яркость, скорость, цвета, направление и светомузыка. В GUI есть вкладка «Звук»: 10-полосный EQ (31 Hz–16 kHz, ±12 дБ) с пресетами Flat, Gaming, Bass Boost, Vocal, Treble Boost, жанрами Rock, Pop, Jazz, Classical, Electronic, Hip-Hop, Acoustic, Speech и «Свои», RNNoise-шумоподавление микрофона, его громкость и mute, а также мониторинг в наушниках. EQ и DSP работают на хосте через PipeWire filter-chain; EQ доступен как виртуальный sink `effect_input.mchose_eq` и выводится на sink гарнитуры. CLI-примеры:

```bash
python3 -m mchose_hub.cli eq preset Gaming
python3 -m mchose_hub.cli eq set 6 3
python3 -m mchose_hub.cli eq on
python3 -m mchose_hub.cli eq status
python3 -m mchose_hub.cli mic volume 75
python3 -m mchose_hub.cli mic mute
python3 -m mchose_hub.cli mic unmute
python3 -m mchose_hub.cli mic nr on
python3 -m mchose_hub.cli mic monitor on
python3 -m mchose_hub.cli mic status
```

Команды RNNoise требуют опциональный пакет `noise-suppression-for-voice` (AUR в Arch Linux); микрофон MCHOSE должен быть активен. Состояние EQ и шумоподавления хранится в `~/.config/mchose-hub/audio.json`. Шаблоны AUR для `mchose-hub-git` и стабильного `mchose-hub` находятся в [`packaging/AUR/`](packaging/AUR/); инструкция публикации — в [`packaging/AUR/README.md`](packaging/AUR/README.md). После отравления транспорта или EOF GUI пере-сканирует устройства на следующем тике опроса и повторяет команду автоматически. Протокол устройства описан в [PROTOCOL.md](PROTOCOL.md).
