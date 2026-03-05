# Standard library
import ctypes
import json
import logging
import os
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from typing import Optional, TypedDict, cast
from enum import IntEnum
from serial.tools import list_ports

# GUI (Tkinter)
import tkinter as tk
from tkinter import colorchooser as cc
from tkinter import messagebox, ttk

# Third-party
import serial  # pip install pyserial

# NOTE: Outlook COM (pywin32) is imported lazily where needed:
#   import win32com.client  # inside get_calendar_info()

# -----------------------------------------------------------------------------
# App / config paths
# -----------------------------------------------------------------------------
__version__ = "1.0.1"
APP_NAME = "CalendarBusyLight"
CONFIG_BASENAME = "calendar_busy_light_config.json"

def _config_path() -> str:
    appdata = os.getenv("APPDATA") or os.path.expanduser("~")
    cfg_dir = os.path.join(appdata, APP_NAME)
    os.makedirs(cfg_dir, exist_ok=True)
    return os.path.join(cfg_dir, CONFIG_BASENAME)

CONFIG_PATH = _config_path()

# -----------------------------------------------------------------------------
# Defaults
# -----------------------------------------------------------------------------
DEFAULT_COLORS = {
    "busy": [255, 0, 0],
    "upcoming_busy": [255, 64, 0],
    "free": [0, 255, 0],
    "oof": [0, 0, 0],
    "unknown": [0, 0, 0],
}

DEFAULT_CONFIG = {
    # per-status icon paths to .ico or .png; empty = default icon
    "status_icon_files": {  
        "busy": "./assets/busy.ico",
        "upcoming_busy": "./assets/upcoming_busy.ico",
        "free": "./assets/free.ico",
        "oof": "./assets/out_of_office.ico",
        "unknown": ""
    }, 

    # Serial / hardware
    "serial_port": "COM26",
    "baudrate": 115200,
    "simulate": False,
    "serial_retry_seconds": 5,

    # Timing
    "status_refresh_seconds": 15.0,
    "blink_period_seconds": 1.0,
    "gui_tick_ms": 100,

    # Calendar logic
    "upcoming_busy_minutes": 5,
    "past_grace_minutes": 1,
    "ignore_subjects": "",   # e.g. "lunch; focus; reminder"

    # Working hours
    "work_start_hour": 8,
    "work_start_minute": 0,
    "work_end_hour": 17,
    "work_end_minute": 0,
    "weekdays_only": True,
    "out_of_hours_status": "oof",  # "oof","free","busy","unknown"

    # Logging (UI booleans)
    "verbose_calendar": False,
    "show_status_summary": True,
    "log_color_changes": True,

    # Appearance
    "brightness_percent": 100,
    "fade_ms": 300,  # NOTE: 0 = instant

    # Color scheme
    "colors": DEFAULT_COLORS,

    # Heartbeat / ACK
    "heartbeat_enabled": True,
    "heartbeat_interval_seconds": 10,
    "heartbeat_expect_ack": False,   # set True if your Arduino answers
    "heartbeat_command": "PING",
    "heartbeat_response": "PONG",
    "heartbeat_timeout_ms": 500,

    # Quick Override (we now use this for BUSY-for-N-min)
    "quick_override_enabled": True,
    "quick_override_default_minutes": 15,  # used for Busy for N minutes
}

# Logging behavior defaults
LOG_DEFAULTS = {
    "log_level": "INFO",          # DEBUG, INFO, WARNING, ERROR
    "log_to_console": False,      # echo to console in addition to file
    "log_file_max_mb": 2,         # rotate after ~2 MB
    "log_file_backups": 5,        # keep 5 backups
}

# Module-level logger holder (populated in run())
LOGGER = logging.getLogger("busy_light")

# -----------------------------------------------------------------------------
# Config load / save
# -----------------------------------------------------------------------------
def load_config() -> dict:
    # deep copy defaults
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    path = CONFIG_PATH
    if not os.path.exists(path):
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k, v in data.items():
                if k != "colors":
                    cfg[k] = v
            colors = cfg.get("colors", {}) or {}
            loaded_colors = data.get("colors", {})
            if not isinstance(loaded_colors, dict):
                loaded_colors = {}
            for key, default_rgb in DEFAULT_COLORS.items():
                rgb = loaded_colors.get(key, default_rgb)
                if not isinstance(rgb, (list, tuple)) or len(rgb) < 3:
                    rgb = default_rgb
                colors[key] = [max(0, min(255, int(x))) for x in rgb[:3]]
            cfg["colors"] = colors
    except Exception as e:
        print(f"Failed to load config from {path}, using defaults: {e}")
    return cfg

def save_config(config: dict) -> None:
    path = CONFIG_PATH
    # sanitize + deep copy
    safe_cfg = json.loads(json.dumps(config))
    colors = safe_cfg.get("colors", {}) or {}
    for k, rgb in colors.items():
        if not isinstance(rgb, (list, tuple)) or len(rgb) < 3:
            colors[k] = list(DEFAULT_COLORS.get(k, [0, 0, 0]))
        else:
            colors[k] = [max(0, min(255, int(x))) for x in rgb[:3]]
    safe_cfg["colors"] = colors

    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(safe_cfg, f, indent=2)
        os.replace(tmp, path)  # atomic on Windows 10+
        print("Configuration saved to", path)
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        print("Failed to save config:", e)
# -----------------------------------------------------------------------------
# Helpers: that safely narrows the Meeting type before index it
# -----------------------------------------------------------------------------
class BusyStatus(IntEnum):
    """Outlook BusyStatus code (0=Free, 1=Tentative, 2=Busy, 3=OOF, 4=WorkingElsewhere)."""
    FREE = 0
    TENTATIVE = 1
    BUSY = 2
    OOF = 3
    WORKING_ELSEWHERE = 4
    UNKNOWN = 255  # fallback for unexpected values

def to_busy_status(value: int) -> BusyStatus:
    try:
        return BusyStatus(value)
    except ValueError:
        return BusyStatus.UNKNOWN
    
class Meeting(TypedDict):
    """Calendar meeting snapshot used by the GUI and LED logic.

    Fields:
        start: Local (naive) start time of the meeting.
        end:   Local (naive) end time of the meeting.
        subject: Subject/title text (may be empty).
        busy_status: Outlook BusyStatus code (0=Free, 1=Tentative, 2=Busy, 3=OOF, 4=WorkingElsewhere).
        location: Location text (may be empty).
    """
    start: datetime
    end: datetime
    subject: str
    busy_status: BusyStatus
    location: str



def as_meeting(obj: object) -> Optional[Meeting]:
    """Return obj as Meeting if it looks like the right dict, else None (helps linters)."""
    if isinstance(obj, dict):
        keys = {"start", "end", "subject", "busy_status", "location"}
        if keys.issubset(obj.keys()):
            return cast(Meeting, obj)
    return None

def pick_earliest(cur: Optional[Meeting], cand: Meeting) -> Meeting:
    """Return the earliest-by-start meeting (cand if cur is None or cand is earlier)."""
    if cur is None:
        return cand
    return cand if cand["start"] < cur["start"] else cur
# -----------------------------------------------------------------------------
# Helpers: time / colors / working hours
# -----------------------------------------------------------------------------
def in_working_hours(dt: datetime, config: dict) -> bool:
    if config.get("weekdays_only", True) and dt.weekday() >= 5:
        return False
    start_dt = dt.replace(hour=int(config.get("work_start_hour", 8)),
                          minute=int(config.get("work_start_minute", 0)),
                          second=0, microsecond=0)
    end_dt = dt.replace(hour=int(config.get("work_end_hour", 17)),
                        minute=int(config.get("work_end_minute", 0)),
                        second=0, microsecond=0)
    return start_dt <= dt <= end_dt

def clamp_rgb(rgb):
    r, g, b = [int(x) for x in rgb[:3]]
    return max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))

def rgb_to_hex(rgb):
    r, g, b = clamp_rgb(rgb)
    return f"#{r:02x}{g:02x}{b:02x}"

def hex_to_rgb(hex_str, fallback=(0, 0, 0)):
    try:
        s = hex_str.strip().lstrip("#")
        if len(s) == 6:
            return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except Exception:
        pass
    return fallback

def get_status_color_from_config(status: str, config: dict):
    colors = config.get("colors", DEFAULT_COLORS)
    status = (status or "unknown").lower()
    rgb = colors.get(status, colors.get("unknown", [0, 0, 0]))
    return clamp_rgb(rgb)

# -----------------------------------------------------------------------------
# Quick Override helpers
#   - Now supports forcing either 'free' (until meeting end) OR 'busy' (for N min).
#   - UPCOMING blink is never suppressed.
# -----------------------------------------------------------------------------
def start_override_until(ts_epoch: float, reason: str, state: dict, forced_status: str):
    """Activate override until a specific epoch timestamp; forced_status in {'free','busy'}."""
    if forced_status not in ("free", "busy"):
        forced_status = "free"
    state["override_active"] = True
    state["override_until_ts"] = float(ts_epoch)
    state["override_reason"] = reason
    state["override_status"] = forced_status
    try:
        LOGGER.info("Override → %s until %s (%s)",
                    forced_status.upper(),
                    time.strftime("%H:%M:%S", time.localtime(ts_epoch)), reason)
    except Exception:
        pass

def start_busy_override_for_minutes(mins: int, state: dict):
    """Activate override to BUSY for N minutes from now."""
    secs = max(1, int(mins) * 60)
    start_override_until(time.time() + secs, f"{mins} min", state, forced_status="busy")

def cancel_override(state: dict):
    """Cancel any active override."""
    if state.get("override_active"):
        try:
            LOGGER.info("Override cancelled")
        except Exception:
            pass
    state["override_active"] = False
    state["override_until_ts"] = None
    state["override_reason"] = ""
    state["override_status"] = None

def apply_override(status: str, state: dict) -> str:
    """
    While override is active, force status to the requested override status,
    but NEVER suppress 'upcoming_busy' (keep blinking for the next meeting).
    """
    if not state.get("override_active"):
        return status

    # Preserve UPCOMING blink
    if status == "upcoming_busy":
        return "upcoming_busy"

    until_ts = state.get("override_until_ts")
    if until_ts is None or time.time() >= float(until_ts):
        cancel_override(state)
        return status

    forced = (state.get("override_status") or "").lower()
    if forced in ("free", "busy"):
        return forced
    return status

# -----------------------------------------------------------------------------
# Helper to open links
# -----------------------------------------------------------------------------
def open_url(url: str):
    try:
        webbrowser.open(url)
    except Exception:
        pass

# -----------------------------------------------------------------------------
# Helper to UI
# -----------------------------------------------------------------------------
def make_scrolled_tab(notebook: ttk.Notebook, title: str):
    """Create a tab with a right-hand vertical scrollbar.
    Returns (outer_frame_added_to_notebook, inner_content_frame).
    Put all your widgets into inner_content_frame.
    """
    outer = ttk.Frame(notebook)
    notebook.add(outer, text=title)  # <-- adds the tab

    canvas = tk.Canvas(outer, highlightthickness=0)
    vbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vbar.set)

    vbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    inner = ttk.Frame(canvas)
    window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

    def _on_inner_config(_):
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.itemconfigure(window_id, width=canvas.winfo_width())

    def _on_outer_config(event):
        canvas.itemconfigure(window_id, width=event.width)

    inner.bind("<Configure>", _on_inner_config)
    outer.bind("<Configure>", _on_outer_config)

    # Mouse wheel (Windows)
    def _on_wheel(event):
        canvas.yview_scroll(-int(event.delta / 120), "units")

    inner.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_wheel))
    inner.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

    return outer, inner

# -----------------------------------------------------------------------------
# Helpers to apply the icon
# -----------------------------------------------------------------------------
_last_status_icon = {"status": None, "path": None}

def set_status_taskbar_icon(root: tk.Tk, cfg: dict, status: str):
    """
    Files-only dynamic icon:
      1) If a file is set for 'status', use it (.ico preferred on Windows).
      2) Else fall back to cfg['app_icon_path'] if present.
      3) Otherwise do nothing.
    Resolves relative paths and logs what happens.
    """
    # Map 'manual' to an existing key (use 'free' by default)
    files = cfg.get("status_icon_files") or {}
    if status in files:
        key = status
    elif status == "manual":
        key = "free"
    else:
        key = "unknown"

    chosen = _resolve_icon_path((files.get(key) or "").strip())
    fallback = _resolve_icon_path((cfg.get("app_icon_path") or "").strip() if "app_icon_path" in cfg else "")

    path = chosen or fallback
    if (_last_status_icon["status"], _last_status_icon["path"]) == (status, path):
        return  # unchanged

    try:
        if path:
            ext = os.path.splitext(path)[1].lower()
            if ext == ".ico" and sys.platform.startswith("win"):
                root.iconbitmap(path)              # best for Windows taskbar/title bar
            else:
                img = tk.PhotoImage(file=path)     # PNG fallback
                root.iconphoto(True, img)
                root._dyn_status_icon_img = img    # keep reference
            try:
                LOGGER.info("Applied icon for status '%s' → %s", status, path)
            except Exception:
                pass
        else:
            try:
                LOGGER.info("No icon applied for status '%s' (missing file)", status)
            except Exception:
                pass
        _last_status_icon["status"], _last_status_icon["path"] = status, path
    except Exception as e:
        try:
            LOGGER.error("Icon apply failed for status '%s': %s", status, e)
        except Exception:
            pass

def _resolve_icon_path(p: str | None) -> str | None:
    """Return absolute existing path for icon. Tries:
       1) as-is if absolute,
       2) relative to script directory,
       3) relative to %APPDATA%/CalendarBusyLight
    """
    if not p:
        return None
    p = p.strip().strip('"')
    if os.path.isabs(p) and os.path.isfile(p):
        return p
    try:
        base = os.path.dirname(os.path.abspath(sys.argv[0]))
        cand = os.path.join(base, p)
        if os.path.isfile(cand):
            return cand
    except Exception:
        pass
    try:
        appdir = os.path.dirname(CONFIG_PATH)  # %APPDATA%/CalendarBusyLight
        cand2 = os.path.join(appdir, p)
        if os.path.isfile(cand2):
            return cand2
    except Exception:
        pass
    return None

def apply_windows_taskbar_id(app_id: str = "com.myUniqueAppUserModelID"):
    """Set a unique AppUserModelID so Windows taskbar treats this as a distinct app."""
    if sys.platform.startswith("win"):
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except Exception:
            pass

# -----------------------------------------------------------------------------
# Helpers serial ports
# -----------------------------------------------------------------------------
def scan_serial_ports() -> list[tuple[str, str]]:
    """Return [(device, label), ...] for available serial ports."""
    ports: list[tuple[str, str]] = []
    try:
        for p in list_ports.comports():
            desc = p.description or "USB Serial"
            label = f"{p.device} — {desc}"
            if getattr(p, "vid", None) is not None and getattr(p, "pid", None) is not None:
                label += f" (VID:{p.vid:04X} PID:{p.pid:04X})"
            ports.append((p.device, label))
        ports.sort(key=lambda t: t[0].upper())
    except Exception:
        pass
    return ports


def _log(msg: str, logger=None, level: str = "info") -> None:
    """Small logger/print shim."""
    if logger is None:
        print(msg)
        return
    fn = getattr(logger, level, logger.info)
    fn(msg)


def _probe_ping_pong(ser: serial.Serial, ping_timeout_s: float = 1.2, tries: int = 3) -> bool:
    """
    Verify this is our Busy Light by sending PING and expecting PONG.
    Handles ATmega32U4 reset-on-open by trying a few times.
    """
    # Use a small blocking timeout *during probe* to simplify reading.
    old_timeout = ser.timeout
    ser.timeout = 0.25  # short blocking read for readline()

    try:
        # Flush anything the device prints on boot
        try:
            ser.reset_input_buffer()
        except Exception:
            pass

        for _ in range(max(1, tries)):
            # Send PING
            try:
                ser.write(b"PING\n")
                ser.flush()
            except Exception:
                return False

            deadline = time.time() + ping_timeout_s
            while time.time() < deadline:
                try:
                    line = ser.readline()  # respects ser.timeout
                except Exception:
                    return False

                if not line:
                    continue

                txt = line.decode("ascii", errors="ignore").strip().upper()
                if "PONG" in txt:
                    return True

            # Small pause before retry (board might still be booting)
            time.sleep(0.25)

        return False
    finally:
        ser.timeout = old_timeout


def _try_open_and_probe(port: str, baudrate: int, logger=None) -> Optional[serial.Serial]:
    """Open a port and return the Serial object if it answers PING/PONG."""
    try:
        ser = serial.Serial(port, baudrate, timeout=0, write_timeout=1.0)  # runtime non-blocking
    except Exception as e:
        _log(f"Could not open {port}: {e}", logger, "debug")
        return None

    try:
        # 32U4 boards often reset on open
        time.sleep(1.2)

        if _probe_ping_pong(ser, ping_timeout_s=1.2, tries=3):
            return ser

    except Exception as e:
        _log(f"Probe failed on {port}: {e}", logger, "debug")

    # Not the Busy Light
    try:
        ser.close()
    except Exception:
        pass
    return None


def open_serial_port(config: dict, logger=None) -> Optional[serial.Serial]:
    """
    Open serial port.
    Strategy:
      1) Try configured port first
      2) If missing/fails, scan all available ports and probe with PING/PONG
    """
    if config.get("simulate", False):
        _log("Simulation mode: not opening serial port.", logger)
        return None

    preferred = str(config.get("serial_port", "")).strip()
    baudrate = int(config.get("baudrate", 115200))

    # Build candidate list: preferred first, then all other available ports
    available = [dev for dev, _label in scan_serial_ports()]
    candidates: list[str] = []
    if preferred:
        candidates.append(preferred)
    for dev in available:
        if dev not in candidates:
            candidates.append(dev)

    if not candidates:
        _log("No serial ports found.", logger, "warning")
        return None

    _log(f"Serial candidates: {candidates}", logger)

    for dev in candidates:
        _log(f"Trying {dev} ...", logger)
        ser = _try_open_and_probe(dev, baudrate, logger=None)
        if ser is not None:
            _log(f"Busy Light connected on {dev} @ {baudrate}", logger)
            # Keep track of where we actually connected (don’t force Settings selection)
            config["active_serial_port"] = dev
            return ser

    _log("Busy Light not found (no PONG on any port).", logger, "warning")
    return None


def send_color(serial_port, r: int, g: int, b: int, config: dict) -> bool:
    """
    Send color command. Returns True if sent, False if port missing/broken.
    """
    if config.get("simulate", False) or serial_port is None:
        return True

    cmd = f"{r} {g} {b}\n"
    serial_port.write(cmd.encode("ascii"))
    serial_port.flush()


# -----------------------------------------------------------------------------
# Calendar via Outlook COM (local, no Graph/Azure)
# -----------------------------------------------------------------------------
def _build_ignore_tokens(config: dict):
    raw = (config.get("ignore_subjects") or "").strip()
    if not raw:
        return []
    parts = [p.strip().lower() for p in raw.replace(",", ";").split(";")]
    return [p for p in parts if p]

def _subject_ignored(subject: str, tokens: list[str]) -> bool:
    s = (subject or "").lower()
    for t in tokens:
        if t and t in s:
            return True
    return False

def get_calendar_info(config: dict) -> tuple[str, Optional[Meeting], Optional[Meeting]]:
    """
    Return (overall_status, next_meeting, current_meeting)
    overall_status in {"busy","free","oof","upcoming_busy","unknown"}
    next_meeting: {"start","subject","busy_status","location"} or None
    current_meeting: {"start","end","subject","busy_status","location"} or None
    """
    past_grace = int(config.get("past_grace_minutes", 1))
    upcoming_minutes = int(config.get("upcoming_busy_minutes", 5))
    verbose = bool(config.get("verbose_calendar", False))
    show_summary = bool(config.get("show_status_summary", True))
    LOOKAHEAD_HOURS = 12
    ignore_tokens = _build_ignore_tokens(config)

    try:
        try:
            import win32com.client
        except ImportError:
            print("ERROR: pywin32 is not installed. Run: pip install pywin32")
            return "unknown", None, None

        outlook = win32com.client.Dispatch("Outlook.Application")
        ns = outlook.GetNamespace("MAPI")
        calendar = ns.GetDefaultFolder(9)  # Calendar

        items = calendar.Items
        items.IncludeRecurrences = True
        items.Sort("[Start]")

        now = datetime.now()
        now_naive = now.replace(tzinfo=None)
        begin = now_naive - timedelta(minutes=past_grace)
        end = now_naive + timedelta(minutes=upcoming_minutes)
        begin_str = begin.strftime("%d/%m/%Y %H:%M")
        end_str = end.strftime("%d/%m/%Y %H:%M")

        restricted_items = items.Restrict(
            f"[Start] <= '{end_str}' AND [End] >= '{begin_str}'"
        )

        currently_busy = False
        have_oof_now = False
        upcoming_busy = False
        current_meeting: Optional[Meeting] = None

        if verbose:
            print("\n--- Calendar window ---")
            print(f"Now:   {now_naive}")
            print(f"Range: {begin} -> {end}")
            print("Appointments in range:")

        for appt in restricted_items:
            try:
                start = appt.Start
                end_t = appt.End
                subject = appt.Subject
                busy_status = getattr(appt, "BusyStatus", 0)
                location = getattr(appt, "Location", "")

                if _subject_ignored(subject, ignore_tokens):
                    if verbose:
                        print(f"  [IGNORED] {subject}")
                    continue

                start_naive = start.replace(tzinfo=None) if hasattr(start, "tzinfo") else start
                end_naive = end_t.replace(tzinfo=None) if hasattr(end_t, "tzinfo") else end_t

                overlaps_now = (start_naive <= now_naive <= end_naive)
                starts_soon = now_naive < start_naive <= now_naive + timedelta(minutes=upcoming_minutes)

                tag = ""
                if overlaps_now and busy_status in (BusyStatus.TENTATIVE, BusyStatus.BUSY):
                    currently_busy = True
                    tag = "[NOW BUSY]"
                    candidate: Meeting = {
                        "start": start_naive,
                        "end": end_naive,
                        "subject": subject or "",
                        "busy_status": busy_status,
                        "location": location or "",
                    }
                    current_meeting = pick_earliest(current_meeting, candidate)

                elif overlaps_now and busy_status == BusyStatus.OOF:
                    have_oof_now = True
                    tag = "[NOW OOF]"

                elif starts_soon and busy_status in (BusyStatus.TENTATIVE, BusyStatus.BUSY):
                    upcoming_busy = True
                    tag = "[UPCOMING BUSY]"

                if verbose:
                    print(f"  {start_naive} -> {end_naive}  {subject}  {tag}")

            except Exception as appt_err:
                print("  Error reading appointment:", appt_err)

        if currently_busy:
            overall = "busy"
        elif have_oof_now:
            overall = "oof"
        elif upcoming_busy:
            overall = "upcoming_busy"
        else:
            overall = "free"

        # Next meeting (Busy/Tentative) ahead
        items2 = calendar.Items
        items2.IncludeRecurrences = True
        items2.Sort("[Start]")

        search_end = now_naive + timedelta(hours=LOOKAHEAD_HOURS)
        now_str = now_naive.strftime("%d/%m/%Y %H:%M")
        search_end_str = search_end.strftime("%d/%m/%Y %H:%M")
        future_items = items2.Restrict(
            f"[Start] >= '{now_str}' AND [Start] <= '{search_end_str}'"
        )

        next_meeting: Optional[Meeting] = None
        for appt in future_items:
            try:
                start = appt.Start
                end_t = appt.End
                subject = appt.Subject
                busy_status = getattr(appt, "BusyStatus", 0)
                location = getattr(appt, "Location", "")

                if _subject_ignored(subject, ignore_tokens):
                    continue

                start_naive = start.replace(tzinfo=None) if hasattr(start, "tzinfo") else start
                end_naive   = end_t.replace(tzinfo=None) if hasattr(end_t, "tzinfo") else end_t
                if busy_status not in (BusyStatus.TENTATIVE, BusyStatus.BUSY):
                    continue
                if start_naive < now_naive:
                    continue
                if next_meeting is None:
                    next_meeting = {
                        "start": start_naive,
                        "end": end_naive,
                        "subject": subject or "",
                        "busy_status": busy_status,
                        "location": location or "",
                    }
                else:
                    if start_naive < next_meeting["start"]:  # safe: not None here
                        next_meeting = {
                            "start": start_naive,
                            "end": end_naive,
                            "subject": subject or "",
                            "busy_status": busy_status,
                            "location": location or "",
                        }
            except Exception:
                continue

        if show_summary:
            print(
                f"Summary: status={overall}, "
                f"next={'None' if not next_meeting else next_meeting['start']}, "
                f"current={'None' if not current_meeting else current_meeting['end']}"
            )

        return overall, next_meeting, current_meeting

    except Exception as e:
        print("Calendar check failed (likely Outlook closed or restarting):", e)
        return "unknown", None, None

# -----------------------------------------------------------------------------
# Tooltips
# -----------------------------------------------------------------------------
class ToolTip:
    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.tipwindow = None
        self.widget.bind("<Enter>", self.show_tip)
        self.widget.bind("<Leave>", self.hide_tip)
    def show_tip(self, event=None):
        if self.tipwindow or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + 20
        self.tipwindow = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(tw, text=self.text, justify="left",
                         background="#ffffe0", relief="solid", borderwidth=1,
                         font=("TkDefaultFont", 9))
        label.pack(ipadx=4, ipady=2)
    def hide_tip(self, event=None):
        tw = self.tipwindow
        self.tipwindow = None
        if tw is not None:
            tw.destroy()

def create_tooltip(widget, text: str):
    ToolTip(widget, text)

# -----------------------------------------------------------------------------
# Logging: file (rotating) + helpers to open file/folder
# -----------------------------------------------------------------------------
def _logs_path() -> str:
    appdata = os.getenv("APPDATA") or os.path.expanduser("~")
    logs_dir = os.path.join(appdata, APP_NAME, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    return os.path.join(logs_dir, "busy_light.log")

class _StreamToLogger:
    """Redirect writes (from print / tracebacks) into logging."""
    def __init__(self, logger, level):
        self.logger = logger
        self.level = level
        self._buf = ""
    def write(self, message):
        if not message:
            return
        self._buf += message
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip()
            if line:
                self.logger.log(self.level, line)
    def flush(self):
        if self._buf:
            self.logger.log(self.level, self._buf.rstrip())
            self._buf = ""

def setup_logging(cfg: dict | None = None) -> logging.Logger:
    cfg = cfg or {}
    merged = {**LOG_DEFAULTS, **{k: v for k, v in cfg.items() if k in LOG_DEFAULTS}}
    level_name = str(merged.get("log_level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    log_path = _logs_path()
    fh = RotatingFileHandler(
        log_path,
        maxBytes=int(merged.get("log_file_max_mb", 2) * 1024 * 1024),
        backupCount=int(merged.get("log_file_backups", 5)),
        encoding="utf-8",
    )
    fmt = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh.setFormatter(fmt)
    fh.setLevel(level)

    logger = logging.getLogger("busy_light")
    logger.handlers.clear()
    logger.setLevel(level)
    logger.addHandler(fh)

    if merged.get("log_to_console", False):
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        ch.setLevel(level)
        logger.addHandler(ch)

    # Redirect print() and unhandled tracebacks to file
    sys.stdout = _StreamToLogger(logger, logging.INFO)   # type: ignore[assignment]
    sys.stderr = _StreamToLogger(logger, logging.ERROR)  # type: ignore[assignment]

    logger.info("Logging started → %s (level=%s)", log_path, level_name)
    return logger

def open_path_in_explorer(path: str):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        messagebox.showerror("Open failed", f"Couldn't open:\n{path}\n\n{e}")

def open_log_file():
    log_path = _logs_path()
    if not os.path.exists(log_path):
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a", encoding="utf-8"):
                pass
        except Exception as e:
            messagebox.showerror("Open log", f"Couldn't create log file:\n{log_path}\n\n{e}")
            return
    open_path_in_explorer(log_path)

def open_logs_folder():
    folder = os.path.dirname(_logs_path())
    if not os.path.isdir(folder):
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Open folder", f"Couldn't create folder:\n{folder}\n\n{e}")
            return
    open_path_in_explorer(folder)

# -----------------------------------------------------------------------------
# GUI
# -----------------------------------------------------------------------------
def run():
    global LOGGER
    config = load_config()
    LOGGER = setup_logging(config)
    apply_windows_taskbar_id("com.mycalendarbusylight")
    root = tk.Tk()
    root.title("Calendar Busy Light")
    #apply_app_icon(root, config)
    set_status_taskbar_icon(root, config, "unknown")
    
    # ---------------- Runtime state ----------------
    state = {
        "ser": None,
        "next_serial_retry": 0.0,
        "serial_needs_resync": True,  # force sending color after (re)connect

        # LED / color
        "last_color": (0, 0, 0),      # last sent RGB after brightness/fade
        "fade_target": (0, 0, 0),     # target color after brightness/blink
        "fade_start": (0, 0, 0),
        "fade_start_ts": 0.0,

        # status & meetings
        "current_status": "unknown",
        "calendar_status": "unknown",
        "next_status_refresh": 0.0,
        "next_meeting": None,
        "current_meeting": None,

        # heartbeat
        "next_heartbeat": 0.0,
        "hb_pending": False,
        "hb_deadline": 0.0,
        "hb_buffer": "",

        # override
        "override_active": False,
        "override_until_ts": None,
        "override_reason": "",
        "override_status": None,   # 'free' or 'busy'
    }

    # ---------------- UI variables ----------------
    mode_var = tk.StringVar(value="auto") # status selection from Outlook
    manual_status_var = tk.StringVar(value="free")  # manual status selection

    status_text = tk.StringVar(value="Status: unknown")
    status_big_text = tk.StringVar(value="UNKNOWN")
    serial_status_text = tk.StringVar(
        value="Serial: simulation mode" if config.get("simulate", False) else "Serial: disconnected"
    )
    current_meeting_text = tk.StringVar(value="Current: none")
    next_meeting_text = tk.StringVar(value="Next: none")
    countdown_text = tk.StringVar(value="")

    # Settings vars
    serial_port_var = tk.StringVar(value=config.get("serial_port", "COM26"))
    baudrate_var = tk.IntVar(value=int(config.get("baudrate", 115200)))
    simulate_var = tk.BooleanVar(value=bool(config.get("simulate", False)))
    serial_retry_var = tk.IntVar(value=int(config.get("serial_retry_seconds", 5)))

    status_refresh_var = tk.DoubleVar(value=float(config.get("status_refresh_seconds", 15.0)))
    blink_period_var = tk.DoubleVar(value=float(config.get("blink_period_seconds", 1.0)))
    gui_tick_var = tk.IntVar(value=int(config.get("gui_tick_ms", 100)))

    upcoming_busy_var = tk.IntVar(value=int(config.get("upcoming_busy_minutes", 5)))
    past_grace_var = tk.IntVar(value=int(config.get("past_grace_minutes", 1)))
    ignore_subjects_var = tk.StringVar(value=config.get("ignore_subjects", ""))

    work_start_hour_var = tk.IntVar(value=int(config.get("work_start_hour", 8)))
    work_start_min_var = tk.IntVar(value=int(config.get("work_start_minute", 0)))
    work_end_hour_var = tk.IntVar(value=int(config.get("work_end_hour", 17)))
    work_end_min_var = tk.IntVar(value=int(config.get("work_end_minute", 0)))
    weekdays_only_var = tk.BooleanVar(value=bool(config.get("weekdays_only", True)))

    allowed_ooh = ["oof", "free", "busy", "unknown"]
    ooh_val = config.get("out_of_hours_status", "oof")
    if ooh_val not in allowed_ooh:
        ooh_val = "oof"
    out_of_hours_status_var = tk.StringVar(value=ooh_val)

    verbose_var = tk.BooleanVar(value=bool(config.get("verbose_calendar", False)))
    summary_var = tk.BooleanVar(value=bool(config.get("show_status_summary", True)))
    log_color_changes_var = tk.BooleanVar(value=bool(config.get("log_color_changes", True)))

    brightness_var = tk.DoubleVar(value=float(config.get("brightness_percent", 100)))
    brightness_display_var = tk.IntVar(value=int(config.get("brightness_percent", 100)))
    fade_ms_var = tk.IntVar(value=int(config.get("fade_ms", 300)))

    color_state = {}
    for key, default_rgb in DEFAULT_COLORS.items():
        color_state[key] = tuple(config.get("colors", {}).get(key, default_rgb)[:3])

    # ---------------- Notebook ----------------
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)
    control_tab = ttk.Frame(notebook)
    about_tab = ttk.Frame(notebook)
    notebook.add(control_tab, text="Control")
    settings_outer, settings_inner = make_scrolled_tab(notebook, "Settings")
    notebook.add(about_tab, text="About / Licenses")

    # ---------------- Control tab ----------------
    mode_frame = ttk.LabelFrame(control_tab, text="Mode"); mode_frame.pack(fill="x", padx=10, pady=5)
    ttk.Radiobutton(mode_frame, text="Auto (Outlook calendar)", variable=mode_var, value="auto").pack(side="left", padx=5, pady=5)
    ttk.Radiobutton(mode_frame, text="Manual", variable=mode_var, value="manual").pack(side="left", padx=5, pady=5)

    # Manual status
    manual_frame = ttk.LabelFrame(control_tab, text="Manual status")
    manual_frame.pack(fill="x", padx=10, pady=5)

    # Manual status - Radio buttons for statuses
    for key, label in [
        ("free", "Free"),
        ("busy", "Busy"),
        ("upcoming_busy", "Upcoming busy (blink)"),
        ("oof", "Out of office"),
    ]:
        ttk.Radiobutton(
            manual_frame, text=label, variable=manual_status_var, value=key
        ).pack(side="left", padx=6, pady=5)

    # Quick override section
    override_frame = ttk.LabelFrame(control_tab, text="Quick override")
    override_frame.pack(fill="x", padx=10, pady=8)
    override_info_var = tk.StringVar(value="No override active")
    ttk.Label(override_frame, textvariable=override_info_var).pack(anchor="w", padx=8, pady=(4, 6))

    def update_override_banner():
        """Refresh small banner text + countdown."""
        if state.get("override_active"):
            secs_left = max(0, int(state["override_until_ts"] - time.time()))
            mm, ss = divmod(secs_left, 60)
            reason = state.get("override_reason", "")
            forced = (state.get("override_status") or "").upper()
            override_info_var.set(f"{forced} (override: {reason}) — {mm:02d}:{ss:02d} left")
        else:
            override_info_var.set("No override active")

    def do_free_until_meeting_end():
        """Override to FREE until current meeting ends."""
        cur = as_meeting(state.get("current_meeting"))
        if cur is None:
            messagebox.showinfo("No active meeting", "There is no ongoing meeting.")
            return
        until_ts = cur["end"].timestamp()  # datetime → epoch seconds
        start_override_until(until_ts, "until meeting end", state, forced_status="free")
        update_override_banner()

    def do_busy_for_n_minutes():
        """Override to BUSY for N minutes from settings."""
        mins = int(config.get("quick_override_default_minutes", 15))
        start_busy_override_for_minutes(mins, state)
        update_override_banner()

    def do_cancel_override():
        cancel_override(state)
        update_override_banner()

    btns = ttk.Frame(override_frame); btns.pack(anchor="w", padx=8, pady=(0,6))
    ttk.Button(btns, text="Free until meeting end", command=do_free_until_meeting_end).pack(side="left", padx=(0,6))
    ttk.Button(btns, text=f"Busy for {int(config.get('quick_override_default_minutes',15))} min",
               command=do_busy_for_n_minutes).pack(side="left", padx=(0,6))
    ttk.Button(btns, text="Cancel override", command=do_cancel_override).pack(side="left")

    # Big status label
    big_label = tk.Label(control_tab, textvariable=status_big_text, font=("Segoe UI", 18, "bold"))
    big_label.pack(pady=(6, 2))

    status_frame = ttk.Frame(control_tab); status_frame.pack(fill="x", padx=10, pady=5)
    ttk.Label(status_frame, textvariable=status_text).pack(anchor="w")
    ttk.Label(status_frame, textvariable=serial_status_text).pack(anchor="w")
    ttk.Label(status_frame, textvariable=current_meeting_text).pack(anchor="w")
    ttk.Label(status_frame, textvariable=next_meeting_text).pack(anchor="w")
    ttk.Label(status_frame, textvariable=countdown_text).pack(anchor="w")

    # Big circle indicator
    preview_canvas = tk.Canvas(control_tab, width=120, height=120, highlightthickness=0)
    preview_canvas.pack(pady=10)
    preview_circle = preview_canvas.create_oval(10, 10, 110, 110, fill="black", outline="#444", width=2)

    # Progress bar
    progress_frame = ttk.Frame(control_tab); progress_frame.pack(fill="x", padx=10, pady=(0,10))
    ttk.Label(progress_frame, text="Progress:").pack(side="left")
    progress_bar = ttk.Progressbar(progress_frame, orient="horizontal", mode="determinate",
                                   length=220, maximum=100)
    progress_bar.pack(side="left", padx=8)

    # ---------------- Settings tab ----------------
    # Serial / Hardware
    serial_group = ttk.LabelFrame(settings_inner, text="Serial / Hardware")
    serial_group.pack(fill="x", padx=10, pady=5)

    # existing vars you likely already have:
    simulate_var = tk.BooleanVar(value=bool(config.get("simulate", False)))
    baud_var = tk.IntVar(value=int(config.get("baudrate", 115200)))
    serial_port_var = tk.StringVar(value=(config.get("serial_port") or ""))

    # --- Port dropdown ---
    ttk.Label(serial_group, text="Serial port:").grid(row=0, column=0, sticky="w", padx=6, pady=4)

    # Keep a mapping of label -> device so we can store only the device in config
    _port_label_to_dev: dict[str, str] = {}
    _port_combo = ttk.Combobox(serial_group, state="readonly", width=50)
    _port_combo.grid(row=0, column=1, sticky="w", padx=6, pady=4)

    # mapping label -> device (e.g., "COM5 — Arduino Leonardo ..." -> "COM5")
    _port_label_to_dev: dict[str, str] = {}

    def _select_by_device(dev: str) -> bool:
        """Select a combobox row by device path (e.g., 'COM5'). Returns True if found."""
        if not dev:
            return False
        for label, d in _port_label_to_dev.items():
            if d == dev:
                _port_combo.set(label)
                serial_port_var.set(d)   # keep var in sync even if <<ComboboxSelected>> doesn't fire
                return True
        return False

    def _current_combo_device() -> str:
        """Device currently shown in the combobox (empty if none)."""
        lbl = _port_combo.get()
        return _port_label_to_dev.get(lbl, "")

    def scan_serial_ports() -> list[tuple[str, str]]:
        ports = []
        try:
            for p in list_ports.comports():
                desc = p.description or "USB Serial"
                label = f"{p.device} — {desc}"
                if getattr(p, "vid", None) is not None and getattr(p, "pid", None) is not None:
                    label += f" (VID:{p.vid:04X} PID:{p.pid:04X})"
                ports.append((p.device, label))
            ports.sort(key=lambda t: t[0].upper())
        except Exception:
            pass
        return ports

    def _populate_ports(keep_selection: bool = True, fallback_to_first: bool = False):
        """
        Refresh the combobox items.
        - If keep_selection is True: preserve the current device if still present.
        - Else: try saved device (serial_port_var) if present.
        - If neither present (or not found) and fallback_to_first is True: select first available.
        - Otherwise leave blank (no forced change).
        """
        # Remember what's currently selected in the UI and what is saved
        current_dev = _current_combo_device()
        saved_dev = (serial_port_var.get() or "").strip()

        # Rebuild mapping
        _port_label_to_dev.clear()
        ports = scan_serial_ports()
        labels = []
        for dev, label in ports:
            _port_label_to_dev[label] = dev
            labels.append(label)

        # Update list
        _port_combo["values"] = labels

        # Try to keep the current selection if requested
        if keep_selection and current_dev and _select_by_device(current_dev):
            return

        # Else try saved device
        if saved_dev and _select_by_device(saved_dev):
            return

        # Else optionally pick first, or clear
        if fallback_to_first and labels:
            first_label = labels[0]
            _port_combo.set(first_label)
            serial_port_var.set(_port_label_to_dev[first_label])
        else:
            _port_combo.set("")
            serial_port_var.set("")

    def _on_port_change(event=None):
        label = _port_combo.get()
        serial_port_var.set(_port_label_to_dev.get(label, ""))

    _port_combo.bind("<<ComboboxSelected>>", _on_port_change)

    # Initial population: keep saved/selected if possible, don't force first
    _populate_ports(keep_selection=True, fallback_to_first=False)

    # Refresh button
    ttk.Button(serial_group, text="Refresh",
        command=lambda: _populate_ports(keep_selection=True, fallback_to_first=False)
        ).grid(row=0, column=2, sticky="w", padx=6, pady=4)

    # Simulate + Baud
    ttk.Checkbutton(serial_group, text="Simulate (no serial)", variable=simulate_var)\
    .grid(row=1, column=0, sticky="w", padx=6, pady=4)
    ttk.Label(serial_group, text="Baud:").grid(row=1, column=1, sticky="w", padx=(6,2), pady=4)
    ttk.Entry(serial_group, width=10, textvariable=baud_var).grid(row=1, column=1, sticky="e", padx=(0,6), pady=4)

    # Initial population & selection
    _populate_ports(keep_selection=True)
    # If a saved port exists but isn't in the list yet, keep it in the var; it will auto-reconnect later.


    # Heartbeat / ACK
    hb_cfg = ttk.LabelFrame(settings_inner, text="Heartbeat & ACK")
    hb_cfg.pack(fill="x", padx=10, pady=5)
    heartbeat_enabled_var = tk.BooleanVar(value=bool(config.get("heartbeat_enabled", True)))
    heartbeat_expect_ack_var = tk.BooleanVar(value=bool(config.get("heartbeat_expect_ack", False)))
    heartbeat_interval_var = tk.IntVar(value=int(config.get("heartbeat_interval_seconds", 10)))
    heartbeat_cmd_var = tk.StringVar(value=config.get("heartbeat_command", "PING"))
    heartbeat_resp_var = tk.StringVar(value=config.get("heartbeat_response", "PONG"))
    heartbeat_timeout_var = tk.IntVar(value=int(config.get("heartbeat_timeout_ms", 500)))
    ttk.Checkbutton(hb_cfg, text="Enable heartbeat", variable=heartbeat_enabled_var).grid(row=0, column=0, sticky="w", padx=5, pady=2)
    ttk.Label(hb_cfg, text="Interval [s]:").grid(row=0, column=1, sticky="w", padx=5, pady=2)
    ttk.Entry(hb_cfg, textvariable=heartbeat_interval_var, width=7).grid(row=0, column=2, sticky="w", padx=5, pady=2)
    ttk.Checkbutton(hb_cfg, text="Expect ACK", variable=heartbeat_expect_ack_var).grid(row=1, column=0, sticky="w", padx=5, pady=2)
    ttk.Label(hb_cfg, text="Cmd:").grid(row=1, column=1, sticky="w", padx=5, pady=2)
    ttk.Entry(hb_cfg, textvariable=heartbeat_cmd_var, width=8).grid(row=1, column=2, sticky="w", padx=5, pady=2)
    ttk.Label(hb_cfg, text="Response:").grid(row=1, column=3, sticky="w", padx=5, pady=2)
    ttk.Entry(hb_cfg, textvariable=heartbeat_resp_var, width=10).grid(row=1, column=4, sticky="w", padx=5, pady=2)
    ttk.Label(hb_cfg, text="Timeout [ms]:").grid(row=1, column=5, sticky="w", padx=5, pady=2)
    ttk.Entry(hb_cfg, textvariable=heartbeat_timeout_var, width=8).grid(row=1, column=6, sticky="w", padx=5, pady=2)

    # Timing
    timing_cfg = ttk.LabelFrame(settings_inner, text="Timing")
    timing_cfg.pack(fill="x", padx=10, pady=5)
    ttk.Label(timing_cfg, text="Status refresh [s]:").grid(row=0, column=0, sticky="w", padx=5, pady=2)
    ttk.Entry(timing_cfg, textvariable=status_refresh_var, width=10).grid(row=0, column=1, padx=5, pady=2)
    ttk.Label(timing_cfg, text="Blink period [s]:").grid(row=0, column=2, sticky="w", padx=5, pady=2)
    ttk.Entry(timing_cfg, textvariable=blink_period_var, width=10).grid(row=0, column=3, padx=5, pady=2)
    ttk.Label(timing_cfg, text="GUI tick [ms]:").grid(row=1, column=0, sticky="w", padx=5, pady=2)
    ttk.Entry(timing_cfg, textvariable=gui_tick_var, width=10).grid(row=1, column=1, padx=5, pady=2)

    # Calendar logic
    cal_cfg = ttk.LabelFrame(settings_inner, text="Calendar logic")
    cal_cfg.pack(fill="x", padx=10, pady=5)
    ttk.Label(cal_cfg, text="Upcoming busy [min]:").grid(row=0, column=0, sticky="w", padx=5, pady=2)
    ttk.Entry(cal_cfg, textvariable=upcoming_busy_var, width=10).grid(row=0, column=1, padx=5, pady=2)
    ttk.Label(cal_cfg, text="Past grace [min]:").grid(row=0, column=2, sticky="w", padx=5, pady=2)
    ttk.Entry(cal_cfg, textvariable=past_grace_var, width=10).grid(row=0, column=3, padx=5, pady=2)
    ttk.Label(cal_cfg, text="Ignore subjects (;)").grid(row=1, column=0, sticky="w", padx=5, pady=2)
    ttk.Entry(cal_cfg, textvariable=ignore_subjects_var, width=40).grid(row=1, column=1, columnspan=3, sticky="w", padx=5, pady=2)

    # Working hours
    work_cfg = ttk.LabelFrame(settings_inner, text="Working hours")
    work_cfg.pack(fill="x", padx=10, pady=5)
    ttk.Checkbutton(work_cfg, text="Weekdays only", variable=weekdays_only_var).grid(row=0, column=0, columnspan=2, sticky="w", padx=5, pady=2)
    ttk.Label(work_cfg, text="Start (HH:MM):").grid(row=1, column=0, sticky="w", padx=5, pady=2)
    ttk.Entry(work_cfg, textvariable=work_start_hour_var, width=4).grid(row=1, column=1, padx=2, pady=2, sticky="w")
    ttk.Entry(work_cfg, textvariable=work_start_min_var, width=4).grid(row=1, column=2, padx=2, pady=2, sticky="w")
    ttk.Label(work_cfg, text="End (HH:MM):").grid(row=1, column=3, sticky="w", padx=5, pady=2)
    ttk.Entry(work_cfg, textvariable=work_end_hour_var, width=4).grid(row=1, column=4, padx=2, pady=2, sticky="w")
    ttk.Entry(work_cfg, textvariable=work_end_min_var, width=4).grid(row=1, column=5, padx=2, pady=2, sticky="w")
    ttk.Label(work_cfg, text="Out-of-hours status:").grid(row=2, column=0, sticky="w", padx=5, pady=2)
    ttk.OptionMenu(work_cfg, out_of_hours_status_var, ooh_val, *allowed_ooh).grid(row=2, column=1, padx=5, pady=2, sticky="w")

    # Appearance (Brightness + Fade)
    appearance_cfg = ttk.LabelFrame(settings_inner, text="Appearance")
    appearance_cfg.pack(fill="x", padx=10, pady=5)
    ttk.Label(appearance_cfg, text="Brightness (%):").grid(row=0, column=0, sticky="w", padx=5, pady=2)
    def on_brightness_change(value):
        try:
            v = int(float(value))
        except Exception:
            v = 0
        brightness_display_var.set(max(0, min(100, v)))
    ttk.Scale(appearance_cfg, from_=0, to=100, orient="horizontal",
              variable=brightness_var, command=on_brightness_change, length=180)\
        .grid(row=0, column=1, padx=5, pady=2, sticky="w")
    ttk.Label(appearance_cfg, textvariable=brightness_display_var).grid(row=0, column=2, padx=5, pady=2, sticky="w")
    ttk.Label(appearance_cfg, text="Fade duration [ms]:").grid(row=1, column=0, sticky="w", padx=5, pady=2)
    ttk.Entry(appearance_cfg, textvariable=fade_ms_var, width=8).grid(row=1, column=1, sticky="w", padx=5, pady=2)

    # Color scheme
    colors_cfg = ttk.LabelFrame(settings_inner, text="Color scheme")
    colors_cfg.pack(fill="x", padx=10, pady=5)
    color_rows = [
        ("busy", "Busy (now)"),
        ("upcoming_busy", "Upcoming busy"),
        ("free", "Free"),
        ("oof", "Out of office"),
        ("unknown", "Unknown / error"),
    ]
    color_swatches = {}
    def make_color_row(row, key, label_text):
        ttk.Label(colors_cfg, text=label_text).grid(row=row, column=0, sticky="w", padx=5, pady=3)
        c = tk.Canvas(colors_cfg, width=36, height=18, highlightthickness=1, highlightbackground="#888")
        c.grid(row=row, column=1, padx=5, pady=3)
        rect = c.create_rectangle(2, 2, 34, 16, fill=rgb_to_hex(color_state[key]), outline="")
        hex_var = tk.StringVar(value=rgb_to_hex(color_state[key]))
        ent = ttk.Entry(colors_cfg, textvariable=hex_var, width=10); ent.grid(row=row, column=2, padx=5, pady=3, sticky="w")
        def apply_hex_from_entry(*args):
            rgb = hex_to_rgb(hex_var.get(), fallback=color_state[key])
            color_state[key] = clamp_rgb(rgb)
            c.itemconfig(rect, fill=rgb_to_hex(color_state[key]))
        ent.bind("<FocusOut>", apply_hex_from_entry); ent.bind("<Return>", apply_hex_from_entry)
        def pick():
            initial = rgb_to_hex(color_state[key])
            picked = cc.askcolor(color=initial, title=f"Pick color for {label_text}")
            if picked and picked[1]:
                rgb = hex_to_rgb(picked[1], fallback=color_state[key])
                color_state[key] = clamp_rgb(rgb)
                c.itemconfig(rect, fill=rgb_to_hex(color_state[key]))
                hex_var.set(rgb_to_hex(color_state[key]))
        ttk.Button(colors_cfg, text="Pick…", command=pick).grid(row=row, column=3, padx=5, pady=3)
        color_swatches[key] = (c, rect, hex_var)
    for idx, (key, label_text) in enumerate(color_rows):
        make_color_row(idx, key, label_text)
    def reset_colors_to_defaults():
        for key, rgb in DEFAULT_COLORS.items():
            color_state[key] = tuple(rgb)
            canvas, rect, hex_var = color_swatches[key]
            canvas.itemconfig(rect, fill=rgb_to_hex(rgb))
            hex_var.set(rgb_to_hex(rgb))
    ttk.Button(colors_cfg, text="Reset colors to defaults", command=reset_colors_to_defaults)\
        .grid(row=len(color_rows), column=0, columnspan=4, sticky="w", padx=5, pady=5)

    # Logging UI + open buttons
    log_cfg = ttk.LabelFrame(settings_inner, text="Logging")
    log_cfg.pack(fill="x", padx=10, pady=5)
    ttk.Checkbutton(log_cfg, text="Verbose calendar overview", variable=verbose_var).grid(row=0, column=0, sticky="w", padx=5, pady=2)
    ttk.Checkbutton(log_cfg, text="Show status summary", variable=summary_var).grid(row=1, column=0, sticky="w", padx=5, pady=2)
    ttk.Checkbutton(log_cfg, text="Log color changes", variable=log_color_changes_var).grid(row=2, column=0, sticky="w", padx=5, pady=2)
    log_buttons = ttk.Frame(log_cfg); log_buttons.grid(row=3, column=0, sticky="w", padx=5, pady=(6,4))
    ttk.Button(log_buttons, text="Open log file", command=open_log_file).pack(side="left", padx=(0, 6))
    ttk.Button(log_buttons, text="Open logs folder", command=open_logs_folder).pack(side="left")

    # Save settings
    btn_frame = ttk.Frame(settings_inner); btn_frame.pack(fill="x", padx=10, pady=10)

    def update_int(key, var, mn=None, mx=None):
        try:
            val = int(var.get())
        except Exception:
            return
        if mn is not None and val < mn: val = mn
        if mx is not None and val > mx: val = mx
        config[key] = val

    def update_float(key, var, mn=None, mx=None):
        try:
            val = float(var.get())
        except Exception:
            return
        if mn is not None and val < mn: val = mn
        if mx is not None and val > mx: val = mx
        config[key] = val

    def save_config_from_gui():
        # Serial
        config["serial_port"] = serial_port_var.get().strip()
        config["baudrate"] = int(baud_var.get())
        config["simulate"] = bool(simulate_var.get())
        update_int("serial_retry_seconds", serial_retry_var, 1, None)
        # Heartbeat
        config["heartbeat_enabled"] = bool(heartbeat_enabled_var.get())
        config["heartbeat_expect_ack"] = bool(heartbeat_expect_ack_var.get())
        update_int("heartbeat_interval_seconds", heartbeat_interval_var, 1, None)
        config["heartbeat_command"] = heartbeat_cmd_var.get().strip() or "PING"
        config["heartbeat_response"] = heartbeat_resp_var.get().strip() or "PONG"
        update_int("heartbeat_timeout_ms", heartbeat_timeout_var, 50, None)
        # Timing
        update_float("status_refresh_seconds", status_refresh_var, 1.0, None)
        update_float("blink_period_seconds", blink_period_var, 0.1, None)
        update_int("gui_tick_ms", gui_tick_var, 20, None)
        # Calendar
        update_int("upcoming_busy_minutes", upcoming_busy_var, 0, None)
        update_int("past_grace_minutes", past_grace_var, 0, None)
        config["ignore_subjects"] = ignore_subjects_var.get()
        # Working hours
        update_int("work_start_hour", work_start_hour_var, 0, 23)
        update_int("work_start_minute", work_start_min_var, 0, 59)
        update_int("work_end_hour", work_end_hour_var, 0, 23)
        update_int("work_end_minute", work_end_min_var, 0, 59)
        config["weekdays_only"] = bool(weekdays_only_var.get())
        ooh = out_of_hours_status_var.get()
        config["out_of_hours_status"] = ooh if ooh in ["oof","free","busy","unknown"] else "oof"
        # Logging
        config["verbose_calendar"] = bool(verbose_var.get())
        config["show_status_summary"] = bool(summary_var.get())
        config["log_color_changes"] = bool(log_color_changes_var.get())
        # Appearance
        try:
            b = int(brightness_display_var.get())
        except Exception:
            b = int(config.get("brightness_percent", 100))
        config["brightness_percent"] = max(0, min(100, b))
        update_int("fade_ms", fade_ms_var, 0, None)
        # Colors
        cfg_colors = {}
        for key in DEFAULT_COLORS:
            cfg_colors[key] = list(clamp_rgb(color_state[key]))
        config["colors"] = cfg_colors

        save_config(config)

    ttk.Button(btn_frame, text="Save settings", command=save_config_from_gui).pack(side="left", padx=5)


    # --- About header ---
    hdr = ttk.Frame(about_tab, padding=10)
    hdr.pack(fill="x")
    ttk.Label(hdr, text="Calendar Busy Light", font=("Segoe UI", 13, "bold")).pack(anchor="w")
    ttk.Label(hdr, text=f"Version: {__version__}").pack(anchor="w", pady=(2, 8))

    # --- App license (MIT) ---
    app_license = ttk.LabelFrame(about_tab, text="Application license (MIT)")
    app_license.pack(fill="x", padx=10, pady=6)
    ttk.Label(app_license,
              text="This application’s source code is licensed under the MIT License.").grid(
                  row=0, column=0, sticky="w", padx=8, pady=(6, 2)
              )

    mit_link = ttk.Label(app_license, text="Open MIT License ↗", cursor="hand2", foreground="#0a58ca")
    mit_link.grid(row=1, column=0, sticky="w", padx=8, pady=(0, 8))
    mit_link.bind("<Button-1>", lambda e: open_url("https://opensource.org/license/mit/"))

    # --- GitHub repository ---
    repo_box = ttk.LabelFrame(about_tab, text="Project repository")
    repo_box.pack(fill="x", padx=10, pady=6)

    ttk.Label(
        repo_box,
        text="Source code, releases, and documentation are available on GitHub."
    ).grid(row=0, column=0, sticky="w", padx=8, pady=(6, 2))

    repo_link = ttk.Label(
        repo_box,
        text="Open GitHub repository ↗",
        cursor="hand2",
        foreground="#0a58ca",
    )
    repo_link.grid(row=1, column=0, sticky="w", padx=8, pady=(0, 8))
    repo_link.bind("<Button-1>", lambda e: open_url("https://github.com/AJK-314159265/busy-light"))

    # ---------------- Serial init ----------------
    if not config.get("simulate", False):
        state["ser"] = open_serial_port(config)
        if state["ser"] is not None:
            active = config.get("active_serial_port") or config.get("serial_port", "")
            serial_status_text.set(f"Serial: connected on {active}")
        else:
            serial_status_text.set("Serial: disconnected (retrying...)")
            state["next_serial_retry"] = time.time() + config.get("serial_retry_seconds", 5)
    else:
        serial_status_text.set("Serial: simulation mode (no hardware)")

    # ---------------- Main update loop ----------------
    def update_loop():
        now_ts = time.time()
        now_dt = datetime.now().replace(tzinfo=None)

        # Keep override countdown fresh
        update_override_banner()

        # Serial reconnect
        if state["ser"] is None and not config.get("simulate", False) and now_ts >= state["next_serial_retry"]:
            serial_status_text.set("Serial: reconnecting...")
            state["ser"] = open_serial_port(config)
            if state["ser"] is None:
                state["next_serial_retry"] = now_ts + config.get("serial_retry_seconds", 5)
                serial_status_text.set("Serial: disconnected (retrying...)")
            else:
                serial_status_text.set(f"Serial: connected on {config.get('serial_port')}")
                state["serial_needs_resync"] = True

        # Heartbeat
        if state["ser"] is not None and config.get("heartbeat_enabled", True):
            if not state["hb_pending"] and now_ts >= state["next_heartbeat"]:
                try:
                    cmd = (config.get("heartbeat_command", "PING") + "\n").encode("ascii")
                    state["ser"].write(cmd)
                    state["ser"].flush()
                    if config.get("heartbeat_expect_ack", False):
                        state["hb_pending"] = True
                        state["hb_deadline"] = now_ts + (config.get("heartbeat_timeout_ms", 500) / 1000.0)
                        state["hb_buffer"] = ""
                    state["next_heartbeat"] = now_ts + config.get("heartbeat_interval_seconds", 10)
                except Exception as e:
                    print(f"Heartbeat send failed: {e}")
                    try:
                        state["ser"].close()
                    except Exception:
                        pass
                    state["ser"] = None
                    state["next_serial_retry"] = now_ts + config.get("serial_retry_seconds", 5)
                    serial_status_text.set("Serial: disconnected (retrying...)")
            elif state["hb_pending"]:
                try:
                    avail = state["ser"].in_waiting if hasattr(state["ser"], "in_waiting") else 0
                    if avail:
                        data = state["ser"].read(avail)
                        try:
                            state["hb_buffer"] += data.decode("ascii", errors="ignore")
                        except Exception:
                            pass
                    expected = config.get("heartbeat_response", "PONG")
                    if expected and expected in state["hb_buffer"]:
                        state["hb_pending"] = False
                        state["hb_buffer"] = ""
                    elif now_ts >= state["hb_deadline"]:
                        print("Heartbeat ACK timeout — reconnecting serial.")
                        try:
                            state["ser"].close()
                        except Exception:
                            pass
                        state["ser"] = None
                        state["hb_pending"] = False
                        state["hb_buffer"] = ""
                        state["next_serial_retry"] = now_ts + config.get("serial_retry_seconds", 5)
                        serial_status_text.set("Serial: disconnected (retrying...)")
                except Exception as e:
                    print(f"Heartbeat read error: {e}")
                    try:
                        state["ser"].close()
                    except Exception:
                        pass
                    state["ser"] = None
                    state["hb_pending"] = False
                    state["next_serial_retry"] = now_ts + config.get("serial_retry_seconds", 5)

        # Calendar refresh
        if now_ts >= state["next_status_refresh"]:
            if in_working_hours(now_dt, config):
                cal_status, next_meeting, current_meeting = get_calendar_info(config)
            else:
                cal_status = config.get("out_of_hours_status", "oof")
                next_meeting = None
                current_meeting = None
                if config.get("show_status_summary", True):
                    print(f"Summary: now={now_dt}, outside working hours -> status={cal_status}")
            state["calendar_status"] = cal_status
            state["next_meeting"] = next_meeting
            state["current_meeting"] = current_meeting
            state["next_status_refresh"] = now_ts + config.get("status_refresh_seconds", 15.0)

        # Compute desired status/color
        if mode_var.get() == "auto":
            cal_status = state.get("calendar_status", "unknown")

            # Apply override while preserving 'upcoming_busy'
            effective = apply_override(cal_status, state)
            state["current_status"] = effective

            base_r, base_g, base_b = get_status_color_from_config(effective, config)

            # Blink if upcoming
            if effective == "upcoming_busy":
                period = float(config.get("blink_period_seconds", 1.0))
                phase = (now_ts % period) / period
                r, g, b = (base_r, base_g, base_b) if phase < 0.5 else (0, 0, 0)
            else:
                r, g, b = base_r, base_g, base_b

            status_text.set(f"Status (auto): {state['current_status']}")
        else:
            # Manual mode: use the selected status and its configured color
            selected = (manual_status_var.get() or "free").lower()
            state["current_status"] = selected

            base_r, base_g, base_b = get_status_color_from_config(selected, config)

            # Keep blink behavior for upcoming in manual mode
            if selected == "upcoming_busy":
                period = float(config.get("blink_period_seconds", 1.0))
                phase = (now_ts % period) / period
                r, g, b = (base_r, base_g, base_b) if phase < 0.5 else (0, 0, 0)
            else:
                r, g, b = base_r, base_g, base_b

            status_text.set(f"Status (manual): {selected}")

        
        # Apply the icon
        set_status_taskbar_icon(root, config, state["current_status"])

        # Brightness scale
        try:
            brightness = int(brightness_display_var.get())
        except Exception:
            brightness = int(config.get("brightness_percent", 100))
        brightness = max(0, min(100, brightness))
        factor = brightness / 100.0
        desired = (int(round(r * factor)), int(round(g * factor)), int(round(b * factor)))

        # Fade transitions
        fade_ms = max(0, int(fade_ms_var.get()))
        if desired != state["fade_target"]:
            state["fade_start"] = state["last_color"]
            state["fade_target"] = desired
            state["fade_start_ts"] = now_ts

        if fade_ms == 0:
            current_color = desired
        else:
            t = (now_ts - state["fade_start_ts"]) / (fade_ms / 1000.0)
            if t <= 0:
                current_color = state["fade_start"]
            elif t >= 1:
                current_color = state["fade_target"]
            else:
                sr, sg, sb = state["fade_start"]
                tr, tg, tb = state["fade_target"]
                current_color = (int(round(sr + (tr - sr) * t)),
                                 int(round(sg + (tg - sg) * t)),
                                 int(round(sb + (tb - sb) * t)))

        # Meeting info + countdown + progress
        nm = as_meeting(state.get("next_meeting")); cm = as_meeting(state.get("current_meeting"))
        mapping = {
            "busy": "BUSY",
            "upcoming_busy": "UPCOMING",
            "free": "FREE",
            "oof": "OUT OF OFFICE",
            "unknown": "UNKNOWN",
            "manual": "MANUAL",
        }
        status_big_text.set(mapping.get(state["current_status"], "UNKNOWN"))
        big_label.config(fg=rgb_to_hex(get_status_color_from_config(state["current_status"], config)))

        if cm is not None and state.get("calendar_status") == "busy":
            subj = cm["subject"] or "(no subject)"
            loc = cm["location"] or ""
            start, end = cm["start"], cm["end"]
            current_meeting_text.set(f"Current: {start.strftime('%H:%M')}–{end.strftime('%H:%M')}  {subj}  {loc}")
            delta = (end - now_dt).total_seconds()
            if delta <= 0:
                countdown_text.set("Ends: now")
                progress_bar["value"] = 100
            else:
                countdown_text.set(f"Ends in {int(delta)//60:02d}:{int(delta)%60:02d}")
                total = max(1, (end - start).total_seconds())
                elapsed = max(0, (now_dt - start).total_seconds())
                progress_bar["value"] = max(0, min(100, int(100 * (elapsed / total))))
        else:
            current_meeting_text.set("Current: none")
            if nm is not None and state.get("calendar_status") == "upcoming_busy":
                start = nm["start"]
                subj = nm["subject"] or "(no subject)"
                loc = nm["location"] or ""
                next_meeting_text.set(f"Next: {start.strftime('%Y-%m-%d %H:%M')}  {subj}  {loc}")
                delta = (start - now_dt).total_seconds()
                total = max(1, int(config.get("upcoming_busy_minutes", 5)) * 60)
                if delta <= 0:
                    countdown_text.set("Starts: now")
                    progress_bar["value"] = 100
                else:
                    countdown_text.set(f"Starts in {int(delta)//60:02d}:{int(delta)%60:02d}")
                    done = max(0, min(total, total - delta))
                    progress_bar["value"] = max(0, min(100, int(100 * (done / total))))
            else:
                if nm is not None:
                    start = nm["start"]; subj = nm["subject"] or "(no subject)"; loc = nm["location"] or ""
                    next_meeting_text.set(f"Next: {start.strftime('%Y-%m-%d %H:%M')}  {subj}  {loc}")
                else:
                    next_meeting_text.set("Next: none")
                countdown_text.set("")
                progress_bar["value"] = 0

        # Push LED if color changed
        if state.get("serial_needs_resync") or current_color != state["last_color"]:
            if config.get("log_color_changes", True):
                print(f"[{now_dt.strftime('%H:%M:%S')}] Mode={mode_var.get()}, "
                      f"Status={state['current_status']}, RGB={current_color}, "
                      f"Brightness={brightness}%")
            try:
                send_color(state["ser"], *current_color, config)
            except Exception as e:
                print(f"Serial error while sending, will retry later: {e}")
                try:
                    if state["ser"] is not None:
                        state["ser"].close()
                except Exception:
                    pass
                state["ser"] = None
                state["next_serial_retry"] = now_ts + config.get("serial_retry_seconds", 5)
                serial_status_text.set("Serial: disconnected (retrying...)")

            state["serial_needs_resync"] = False
            state["last_color"] = current_color
            preview_canvas.itemconfig(preview_circle, fill=rgb_to_hex(current_color))

        root.after(max(20, int(config.get("gui_tick_ms", 100))), update_loop)

    # On-close: persist visual settings + colors
    def on_close():
        try:
            b = int(brightness_display_var.get())
        except Exception:
            b = int(config.get("brightness_percent", 100))
        config["brightness_percent"] = max(0, min(100, b))
        config["fade_ms"] = max(0, int(fade_ms_var.get()))
        cfg_colors = {}
        for key in DEFAULT_COLORS:
            cfg_colors[key] = list(clamp_rgb(color_state[key]))
        config["colors"] = cfg_colors
        save_config(config)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(config.get("gui_tick_ms", 100), update_loop)
    root.mainloop()

if __name__ == "__main__":
    run()
