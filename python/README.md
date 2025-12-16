# Calendar Busy Light — Python GUI (Outlook → LED)

A Windows Python app that turns your **Outlook desktop calendar** into a **busy light** for an RGB LED.  
**No Azure, no Graph, no Teams.** Local-only via Outlook **COM** and **Serial**.

Pairs with the included Arduino **NeoPixel** sketch (or any MCU that accepts `R G B\n`).

## Features

- **Auto mode (Outlook → status → LED)**  
  - `Busy`/`Tentative` → **busy** (red)  
  - **Upcoming busy** (N minutes before start) → **blinking orange**  
  - `OOF` → **off** (configurable)  
  - `Free`/`WorkingElsewhere` → **free** (green)
- **Manual mode (status)**  
  Pick **Free / Busy / Upcoming busy (blinks) / OOF** directly in the UI.
- **Quick overrides**  
  - **Free until meeting ends** (for early finishes)  
  - **Busy for N minutes** (default 15 min)  
  *Upcoming-busy blink is never suppressed by overrides.*
- **Dynamic taskbar/window icon (files-only)**  
  Per-status **.ico/.png** files for **busy / upcoming / free / oof / unknown**.  
  (No auto-color generation; you control the icons.)
- **Color scheme editor**  
  Configure RGB for **busy / upcoming_busy / free / oof / unknown**.
- **Brightness slider** (0–100%, integer readout) + **smooth fade** transitions.
- **Meeting context at a glance**  
  Big status banner, **countdown** (to **start** during upcoming, to **end** during busy), **progress bar**, next/current meeting lines.
- **Ignore filters**  
  Skip calendar items whose subject contains any of your `;`-separated tokens (e.g. `lunch; focus; reminder`).
- **Working-hours model**  
  Weekdays-only toggle, start/end time, and **out-of-hours status** (e.g. `oof` to turn LED off after hours).
- **Heartbeat & ACK** to the device (`PING`/`PONG`) with auto-reconnect.
- **Serial auto-reconnect** when USB is unplugged/replugged.
- **Logging to file** (rotating), with **Open log file / Open logs folder** buttons.
- **About / Licenses** tab  
  Show MIT license + third-party attributions (e.g., Noun Project CC BY 3.0 icon credit).

## Requirements

- **Windows** with **Outlook desktop (classic)** installed and signed in (uses COM).
- **Python 3.9+** (tested with 3.13).
- Install dependencies:

  ```bash
  pip install pywin32 pyserial
  ```

- Optional hardware:  
  An Arduino/Pro Micro (ATmega32U4) with a **WS2812/NeoPixel** LED and the provided sketch, or any MCU that accepts ASCII `R G B\n` at **115200** baud.

> No cloud permissions. The app reads your **local Outlook calendar** only.

## Quick start

1. Clone/download the repo.
2. Install dependencies:

   ```bash
   pip install pywin32 pyserial
   ```

3. Run the GUI (no console):

   ```bash
   pythonw.exe calendar_busy_light.py
   ```

4. In **Settings → Serial / Hardware**, set your **COM port** and click **Save settings**.
5. On **Control**, switch to **Manual**, pick **Free/Busy/Upcoming/OOF** to test the LED.
6. Switch to **Auto** to let Outlook drive the light.

> Config and logs live in **`%APPDATA%\CalendarBusyLight\`** and persist across restarts.

## How it works

- The app queries Outlook via **pywin32 COM** using a time-window Restrict filter around “now”.
- It derives a **calendar status**:
  - Overlaps now and `Busy/Tentative` → **busy**
  - Starts within **Upcoming busy (N minutes)** → **upcoming_busy** (blink)
  - Overlaps now and `OOF` → **oof**
  - Otherwise → **free**
- Status maps to your **configured RGB**, then the app sends `R G B\n` to the serial device.  
  Heartbeat sends `PING\n`; if **Expect ACK** is enabled, it looks for `PONG`.
- **Brightness** (0–100%) and **fade (ms)** are applied in software.

## UI guide

### Control

- **Mode**: **Auto** (Outlook) / **Manual** (pick status: free/busy/upcoming/oof)
- **Quick override**:  
  - **Free until meeting end**  
  - **Busy for *N* minutes** (configurable default)  
  Shows a live **override countdown**.
- **Status banner** (FREE / BUSY / UPCOMING / OOF) + big colored indicator.
- **Current / Next** meeting lines and **countdown**.
- **Progress bar**:  
  - Fills toward **start** when upcoming  
  - Fills toward **end** when busy
- **Preview circle** mirrors the LED output.

### Settings

- **Serial / Hardware**: COM port, baud, **Simulate**, retry seconds.
- **Heartbeat & ACK**: enable, interval, **Expect ACK**, command/response, timeout.
- **Timing**: calendar refresh (s), blink period (s), GUI tick (ms).
- **Calendar logic**: upcoming window (min), past grace (min), **Ignore subjects** (`;`-separated).
- **Working hours**: weekdays-only, start/end, **out-of-hours status** (`oof`/free/`busy`/`unknown`).
- **Appearance**: **Brightness %** (integer display), **Fade ms**.
- **Color scheme**: edit hex colors for busy / upcoming_busy / free / oof / unknown.
- **Taskbar icon (per status files)**: enable, browse/clear **.ico/.png** for each status.  
  (No auto-color generation; uses your files. `.ico` with 16/32/48/256px is recommended on Windows.)
- **Logging**: verbose calendar overview, status summary, **log color changes**, **Open log file**, **Open logs folder**.
- **Application**: choose a window icon file (fallback if no per-status icon applies).
- **Save settings** writes JSON to `%APPDATA%\CalendarBusyLight\calendar_busy_light_config.json`.

## Configuration

Path: **`%APPDATA%\CalendarBusyLight\calendar_busy_light_config.json`**

```jsonc
{
  "serial_port": "COM2",
  "baudrate": 115200,
  "simulate": false,
  "serial_retry_seconds": 5,

  "status_refresh_seconds": 15.0,
  "blink_period_seconds": 1.0,
  "gui_tick_ms": 100,

  "upcoming_busy_minutes": 5,
  "past_grace_minutes": 1,
  "ignore_subjects": "lunch; focus; reminder",

  "work_start_hour": 8, "work_start_minute": 0,
  "work_end_hour": 17,  "work_end_minute": 0,
  "weekdays_only": true,
  "out_of_hours_status": "oof",

  "verbose_calendar": false,
  "show_status_summary": true,
  "log_color_changes": true,
  "brightness_percent": 100,
  "fade_ms": 300,

  "colors": {
    "busy": [255, 0, 0],
    "upcoming_busy": [255, 128, 0],
    "free": [0, 255, 0],
    "oof": [0, 0, 0],
    "unknown": [0, 0, 0]
  },

  "heartbeat_enabled": true,
  "heartbeat_interval_seconds": 10,
  "heartbeat_expect_ack": false,
  "heartbeat_command": "PING",
  "heartbeat_response": "PONG",
  "heartbeat_timeout_ms": 500,

  "quick_override_enabled": true,
  "quick_override_default_minutes": 15,

  "app_icon_path": "",                       // fallback window icon
  "dynamic_status_icon_enabled": true,       // files-only dynamic icons
  "dynamic_status_icon_mode": "files",
  "status_icon_files": {
    "busy": "",
    "upcoming_busy": "",
    "free": "",
    "oof": "",
    "unknown": ""
  }
}
```

> Edit via the GUI when possible; manual edits are fine (the app clamps values on save).

## Logs

- Rotating log at: **`%APPDATA%\CalendarBusyLight\logs\busy_light.log`**  
  Open directly from **Settings → Logging → Open log file / Open logs folder**.

## Device protocol (reference)

- Commands: `R G B`, `RGB r g b`, `#RRGGBB`, `OFF`, `TEST`, `PING`  
- Optional heartbeat watchdog: turns LED **off** if no `PING` seen within timeout.
- Baud: **115200**. Only one program can open the COM port at a time.

## Troubleshooting

- **No light but serial is connected**  
  Check **Brightness > 0%**, **out-of-hours status**, and your **Color scheme** for the current status.
- **Manual works, Auto stays off**  
  Likely outside working hours with `out_of_hours_status = "oof"`.
- **Outlook not read**  
  Ensure Outlook desktop is running. Try toggling Outlook. Enable **Verbose calendar** in Logging.
- **Taskbar icon doesn’t switch**  
  Make sure per-status `.ico/.png` paths are set and files exist. For pinned shortcuts, unpin → run → pin.
- **Blink looks “soft”**  
  Set **Fade ms = 0** to test hard on/off edges.

## Privacy

- The app reads **only your local Outlook calendar** via COM.  
  **No network calls, no Graph/Azure, no tokens.**

## License

This project is released as open-source software under the [MIT License](./../LICENSE.md), Copyright (c) 2025 Allan Juhl Kristensen

The software is provided “as is,” without any express or implied warranty.

## Acknowledgements

- freeconvert [PNG to ICO Converter](https://www.freeconvert.com/png-to-ico)
- `pywin32` for Outlook COM access
- `pyserial` for device communication
