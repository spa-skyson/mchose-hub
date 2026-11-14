# MCHOSE Hub

An unofficial Linux control hub for the MCHOSE V9 Turbo+ headset and its RGB dock.

## Features

| Device | Controls |
|---|---|
| V9 Turbo+ headset (2.4 GHz dongle) | Battery and charging state; Game 1, Game 2, and Music modes; 15/30/45-minute auto-shutdown; voice-prompt volume; factory reset. |
| RGB dock | Six lighting effects; on/off; brightness, speed, base colors, Flowing Wave direction, music-reactive mode, sleep settings, and firmware/device information. |

The PyQt6 application provides a window and system tray controls, and displays headset and dock firmware versions. A command-line interface is included for diagnostics and direct control.

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

The CLI also supports `shutdown`, `prompt`, and `light on|off|mode|color|brightness|speed|direction`. Run `python3 -m mchose_hub.cli <command> --help` for command-specific arguments.

## Troubleshooting

- **Permission denied opening a HID device:** install `packaging/99-mchose-hub.rules`, run the `udevadm` commands above, then reconnect the device. Check that `/dev/hidraw*` is present and accessible to your user.
- **The headset does not respond:** confirm that it is powered on and connected over the 2.4 GHz dongle. The headset command channel can park after inactivity; reconnect the dongle physically (unplug and plug it back in). See [PROTOCOL.md §7.1](PROTOCOL.md#71-keepalive-поведение-командного-канала-наушников--эмпирика-2026-11-15).
- **Looking for EQ settings:** EQ and other audio processing run on the host, not in the headset. Configure them with PipeWire tools such as EasyEffects; those settings stay on the host.

## How it works

The HID protocol was reverse-engineered from the official M HUB software. The 2.4 GHz headset dongle uses USB ID `3837:600a`; the RGB dock uses `3837:6008`. Headset commands require opening and closing the command channel with `SET_FEATURE` report `0xAA` (UNLOCK/LOCK). See [PROTOCOL.md](PROTOCOL.md) for the protocol details.

## Limitations

- Firmware OTA updates are not implemented.
- There is no Bluetooth transport; headset control uses the 2.4 GHz dongle.
- EQ and host-side DSP are configured through PipeWire (for example, EasyEffects), not stored in or processed by the headset.

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

Хаб показывает батарею и версии прошивок, переключает режимы Game 1/Game 2/Music, автоотключение и громкость голосовых подсказок. Для дока доступны шесть эффектов подсветки, яркость, скорость, цвета, направление и светомузыка. Отладочный CLI запускается как `python3 -m mchose_hub.cli`; EQ настраивается на хосте через PipeWire/EasyEffects. Протокол и ограничения описаны в [PROTOCOL.md](PROTOCOL.md).
