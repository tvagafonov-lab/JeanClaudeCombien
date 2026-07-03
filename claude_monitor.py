#!/usr/bin/env python3
"""
JeanClaudeCombien — Claude usage overlay for Windows
Always-on-top widget. Double-click header to toggle compact mode.
Right-click for settings (opacity, language, used/remaining toggle).
Updates automatically when buddy-tokens.json changes (file watcher).
"""

import tkinter as tk
import ctypes
from ctypes import wintypes
import json, os, sys, subprocess, time, threading
from pathlib import Path
from datetime import datetime, timedelta, timezone
import i18n
import fetch_usage

# ── Log redirection (must run BEFORE tk.Tk() and any thread start) ────────────
# pythonw.exe sends stdout/stderr to NUL by default. That swallows every
# uncaught exception inside `_bg_fetch`'s worker thread, every tk
# `report_callback_exception`, every traceback from a third-party library
# (cloudscraper, pystray, requests, urllib3). Symptom we keep hitting:
# process alive, overlay visible, `monitor_fetch.log` not growing for
# hours, and no clue why. Redirect both streams to files under
# %APPDATA%\Claude so the next scheduler-zombie incident is diagnosable
# without manually re-running the script under python.exe.
# Mode 'w' truncates per launch — we want THIS run's evidence, not a
# rolling history that buries the relevant trace.
try:
    _LOG_DIR = Path(os.environ.get("APPDATA", "")) / "Claude"
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    sys.stdout = open(_LOG_DIR / "monitor_stdout.log", "w",
                      encoding="utf-8", buffering=1)
    sys.stderr = open(_LOG_DIR / "monitor_stderr.log", "w",
                      encoding="utf-8", buffering=1)
except Exception:
    pass  # best-effort; staying on /dev/null is no worse than today

# Cleanup stale per-PID fetch logs (>1 day old). These accumulate when
# fetch_usage._log falls through to its tier-2 fallback during a shared-
# log deadlock; without cleanup the directory would grow forever.
try:
    cutoff = time.time() - 86_400
    for stale in _LOG_DIR.glob("monitor_fetch_*.log"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            pass
except Exception:
    pass

# Distinct AppUserModelID so Win11 shell treats this pythonw.exe instance
# as its own application (separate from any sibling overlay like CHB).
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
        "JeanClaudeCombien.Overlay.1")
except Exception:
    pass

# Tray-mode deps. Pystray+Pillow are optional — if they're missing the
# overlay still works in full / compact / dock. PIL is also used for
# antialiased dock rings via supersampling + LANCZOS downscale.
try:
    import pystray
    import pystray._win32 as _pystray_win32
    from pystray._win32 import win32 as _pystray_w32
    from PIL import Image, ImageDraw, ImageTk
    TRAY_AVAILABLE = True

    def _pystray_message_patched(self, code, flags, **kwargs):
        guid = getattr(self, "_guid", None)
        if guid is not None:
            flags |= _pystray_w32.NIF_GUID
            kwargs["guidItem"] = guid
        _pystray_w32.Shell_NotifyIcon(code, _pystray_w32.NOTIFYICONDATAW(
            cbSize=ctypes.sizeof(_pystray_w32.NOTIFYICONDATAW),
            hWnd=self._hwnd,
            hID=getattr(self, "_uid", None) or id(self),
            uFlags=flags,
            **kwargs))
    _pystray_win32.Icon._message = _pystray_message_patched

    def _make_guid(d1, d2, d3, d4):
        G = _pystray_w32.NOTIFYICONDATAW.GUID
        return G(Data1=d1, Data2=d2, Data3=d3,
                 Data4=(ctypes.c_ubyte * 8)(*d4))
except Exception:
    TRAY_AVAILABLE = False

# Win32 indices for GetSystemMetrics / SystemParametersInfo.
SM_XVIRTUALSCREEN  = 76
SM_YVIRTUALSCREEN  = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SPI_GETWORKAREA    = 0x0030

# ── Paths ─────────────────────────────────────────────────────────────────────
APPDATA       = Path(os.environ.get("APPDATA", Path.home()))
CLAUDE_DIR    = APPDATA / "Claude"
TOKENS_FILE   = CLAUDE_DIR / "buddy-tokens.json"
CACHE_FILE    = CLAUDE_DIR / "monitor_usage_cache.json"
SETTINGS_FILE = CLAUDE_DIR / "monitor_settings.json"
# Scheduler watchdog: independent daemon-thread that respawns the process
# if _bg_fetch hasn't fired a heartbeat for this long. The four-incident
# series (2026-05-13…2026-05-27) made it clear that the real fault isn't
# auth false-positives but scheduler-zombie — root.after callbacks stop
# firing after wake/standby/cold-boot and no in-process logic can detect
# it from a thread that has already been silenced. Watchdog runs in its
# own daemon-thread (independent of tk-mainloop), checks the heartbeat
# every 60 s, and after >WATCHDOG_FATAL_S of silence Popens a fresh self
# and os._exit(2). Sized as 3× the 180 s fetch cadence with one tick of
# slack — anything past three lost ticks is unambiguously dead.
WATCHDOG_FATAL_S = 600
# Objective liveness backstop independent of the in-process heartbeat: if the
# usage cache file hasn't been written (= no fetch has actually succeeded) for
# this long, the process is a silent-fetch zombie (incident 2026-06-22) and
# the watchdog respawns it even though its heartbeat looks healthy.
CACHE_STALE_S = 360


try:
    ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_uint64
except Exception:
    pass


def _system_uptime_s() -> float:
    """Seconds since the last Windows boot, via GetTickCount64.
    Returns 0.0 if the API is unavailable so callers treat the system
    as 'long up' (i.e. the cold-boot suppression window is closed).
    restype is pinned to c_uint64 at import to avoid 32-bit truncation
    on machines that have been up for >49 days."""
    try:
        return ctypes.windll.kernel32.GetTickCount64() / 1000.0
    except Exception:
        return 0.0


DEFAULT_SETTINGS = {
    "opacity":        0.92,
    "compact":        False,
    "dock":           False,
    "tray":           False,   # fourth mode: hidden window, system-tray icon
    "dock_x":         -1,      # saved X in dock mode (-1 = default near Start)
    "lang":           "en",
    "show_remaining": False,   # False = used %, True = remaining %
    "pos_x":          -1,
    "pos_y":          -1,
}

# ── Colors — Claude orange on warm dark ───────────────────────────────────────
C = {
    "bg":     "#110a06",
    "bg2":    "#1f140a",
    "hdr":    "#2a1a0e",
    "accent": "#d97757",   # Claude orange
    "text":   "#f5e5d3",   # warm cream
    "muted":  "#7a5c45",
    "green":  "#7ecf6e",
    "yellow": "#e8a020",
    "red":    "#e06050",
    "bar":    "#3a2515",
}

W_FULL    = 265
W_COMPACT = 165

RING_SIZE           = 36   # ring canvas size (px) in dock mode
RING_PAD            = 3    # padding around each ring canvas
DOCK_H              = RING_SIZE + RING_PAD * 2 + 2   # matches Win11 taskbar height
DOCK_DEFAULT_X      = 80   # default dock X near the Win11 Start button
TASKBAR_FALLBACK_H  = 48   # assumed taskbar height if SPI_GETWORKAREA fails
REFETCH_INTERVAL_MS = 180_000   # 3-min fallback re-fetch (symmetric with CHB)

# Tray icon — drawn at 64×64 and downscaled by Windows to 16/20/24 px.
# Pillow supersamples 4× and LANCZOS-downsamples for antialiased edges.
TRAY_ICON_SIZE      = 64
TRAY_RING_STROKE    = 14   # outer ring thickness at target size
TRAY_EDGE_MARGIN    = 1    # inset from icon edge
# Stable identity (do NOT change). Windows 11 binds (GUID→exe) at first
# NIM_ADD and refuses fresh GUID registrations from the same exe later.
TRAY_UID  = 0xC1A0_DE77
TRAY_GUID = (0xC1A0DE77, 0xCAD5, 0xEAF1,
             (0x77, 0x77, 0xC1, 0xA0, 0xDE, 0x77, 0xC1, 0xA0))


# ── Multi-monitor helpers ─────────────────────────────────────────────────────
def _virtual_screen_rect() -> "tuple[int, int, int, int] | None":
    """Bounding box of all currently connected monitors, in screen coords."""
    try:
        u = ctypes.windll.user32
        x = u.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = u.GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = u.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = u.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        return (x, y, x + w, y + h)
    except Exception:
        return None


def _rect_on_screen(x: int, y: int, w: int, h: int, min_overlap: int = 40) -> bool:
    """True if the window rect overlaps the visible virtual desktop enough to be
    reachable — used to detect positions stranded on a now-disconnected monitor."""
    vs = _virtual_screen_rect()
    if vs is None:
        return True
    vl, vt, vr, vb = vs
    return (min(x + w, vr) - max(x, vl) >= min_overlap
            and min(y + h, vb) - max(y, vt) >= min_overlap)

# Icons for the fixed limit kinds; scoped model limits get _MODEL_ICON.
_KIND_ICONS   = {"session": "⏱", "weekly_all": "📅"}
_MODEL_ICON   = "✦"
_CREDITS_ICON = "💳"


def _display_rows(cache: dict, lang: str) -> list:
    """Unified, ordered list of rows to render: one per limit in the cache
    (session, weekly_all, then any scoped model limits) plus a credits row.

    Single source of truth for cache→row mapping — _refresh_ui, the tray
    tooltip and the hover card all render from this instead of each
    re-parsing the cache. Each row is a dict:
        {icon, label, pct, reset, credits}
    where `reset` is an ISO string or None (credits has no reset) and
    `credits` is the spend dict (used/limit/balance/curr/enabled) or None."""
    out = []
    for lim in (cache.get("limits") or []):
        lk = lim.get("label_key")
        if lk:                              # named kind (session / weekly_all)
            label, icon = i18n.get(lang, lk), _KIND_ICONS.get(lim.get("kind"), "◆")
        else:                               # scoped model limit — label is the model name
            label, icon = (lim.get("label") or "?"), _MODEL_ICON
        out.append({"icon": icon, "label": label, "pct": lim.get("pct") or 0,
                    "reset": lim.get("reset"), "credits": None,
                    "kind": lim.get("kind")})
    cr = cache.get("credits")
    if cr:
        out.append({"icon": _CREDITS_ICON, "label": i18n.get(lang, "row_credits"),
                    "pct": cr.get("pct") or 0, "reset": None, "credits": cr,
                    "kind": "credits"})
    return out


def _credits_text(cr: dict) -> str:
    """Reset-column text for the credits row: used/limit when spend is
    enabled, otherwise the available balance."""
    curr = "€" if cr.get("curr") == "EUR" else (cr.get("curr") or "")
    if cr.get("enabled") and cr.get("limit"):
        return f"{cr.get('used', 0):.2f} / {cr['limit']:.2f} {curr}".strip()
    return f"{cr.get('balance', 0):.2f} {curr}".strip()


# ── Settings ──────────────────────────────────────────────────────────────────
class Settings:
    def __init__(self):
        self._d = DEFAULT_SETTINGS.copy()
        try:
            if SETTINGS_FILE.exists():
                self._d.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
        except Exception:
            pass

    def save(self):
        try:
            SETTINGS_FILE.write_text(json.dumps(self._d, indent=2), "utf-8")
        except Exception:
            pass

    def __getitem__(self, k):    return self._d.get(k)
    def __setitem__(self, k, v): self._d[k] = v; self.save()


# ── Helpers ───────────────────────────────────────────────────────────────────
_cache_data: dict = {}
_cache_mtime: float = 0.0


def read_cache() -> dict:
    """Return the parsed cache file, re-reading only when its mtime changes."""
    global _cache_data, _cache_mtime
    try:
        mt = os.path.getmtime(CACHE_FILE)
    except OSError:
        return _cache_data
    if mt != _cache_mtime:
        try:
            _cache_data  = json.loads(CACHE_FILE.read_text("utf-8"))
            _cache_mtime = mt
        except (OSError, ValueError):
            pass
    return _cache_data


def _reset_dt(iso: str | None) -> "datetime | None":
    """Parse an ISO timestamp into an aware UTC datetime, or None if invalid."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def reset_passed(iso: str | None, now: "datetime | None" = None) -> bool:
    """True if the given ISO timestamp is in the past."""
    dt = _reset_dt(iso)
    if dt is None:
        return False
    return dt < (now or datetime.now(tz=timezone.utc))


def fmt_reset(iso: str | None, lang: str, window_seconds: int = 0,
              now: "datetime | None" = None) -> str:
    """Format an ISO timestamp into a countdown, rolling past resets forward
    by `window_seconds` so we always show the *next* expected reset."""
    tr = i18n.STRINGS.get(lang, i18n.STRINGS["en"])
    dt = _reset_dt(iso)
    if dt is None:
        return "—"
    if now is None:
        now = datetime.now(tz=timezone.utc)
    diff = dt - now
    if diff.total_seconds() < 0:
        if window_seconds <= 0:
            return tr["reset_done"]
        cycles = int(-diff.total_seconds() // window_seconds) + 1
        dt     = dt + timedelta(seconds=window_seconds * cycles)
        diff   = dt - now
    mins = int(diff.total_seconds() // 60)
    h, m = divmod(mins, 60)
    if diff.total_seconds() < 86400:
        return f"{h}h {m:02}m" if h else f"{m}m"
    local = dt.astimezone()
    return f"{tr['days'][local.weekday()]} {local.strftime('%H:%M')}"


def bar_color(pct: float) -> str:
    if pct >= 90: return C["red"]
    if pct >= 60: return C["yellow"]
    return C["green"]


def pct_color(pct: float) -> str:
    if pct >= 90: return C["red"]
    if pct >= 60: return C["yellow"]
    return C["text"]


def ring_color(pct: float) -> tuple:
    """Saturated RGBA for tray/dock rings — reads at 16 px in the tray."""
    if pct >= 90: return (255,  68,  68, 255)
    if pct >= 60: return (255, 176,  32, 255)
    return              ( 34, 220,  85, 255)


def _truncate_utf16(s: str, max_units: int = 127) -> str:
    """Truncate `s` to at most `max_units` UTF-16 code units.

    Windows `NOTIFYICONDATAW.szTip` is `WCHAR[128]` (127 chars + NUL). Python
    `str[:127]` counts Unicode code points, so a string with several emoji
    (`💳📅🎨` — each is a UTF-16 surrogate pair, 2 units) silently slips past
    the limit and pystray raises `ValueError: string too long (130, maximum
    length 128)` from inside its setup_handler thread. That exception leaves
    the tray icon half-initialized AND can cascade into the periodic fetch
    scheduler going dormant — observed symptom: monitor process alive,
    overlay visible, but `monitor_fetch.log` not appended for hours."""
    out, used = [], 0
    for ch in s:
        units = 2 if ord(ch) > 0xFFFF else 1  # BMP supplementary → surrogate pair
        if used + units > max_units:
            break
        out.append(ch)
        used += units
    return "".join(out)


def _render_single_ring(size, pct, color_rgba, track_rgba, stroke,
                        supersample=4, center_rgba=None, edge_margin=3,
                        outline_rgba=None, outline_width=1):
    """Render a progress ring of `size`×`size` via 4× supersampling.
    Optional center disc + thin contour on both ring edges."""
    S = size * supersample
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    sw  = stroke * supersample
    margin = edge_margin * supersample
    ow  = outline_width * supersample

    bbox = (margin, margin, S - margin - 1, S - margin - 1)
    d.ellipse(bbox, outline=track_rgba, width=sw)
    if pct > 0:
        span = max(1.0, min(359.9, 3.6 * pct))
        d.arc(bbox, start=-90, end=-90 + span, fill=color_rgba, width=sw)

    if outline_rgba is not None and ow > 0:
        d.ellipse(bbox, outline=outline_rgba, width=ow)
        ie = margin + sw - ow
        d.ellipse((ie, ie, S - ie - 1, S - ie - 1),
                  outline=outline_rgba, width=ow)

    if center_rgba is not None:
        c = margin + sw
        d.ellipse((c, c, S - c - 1, S - c - 1), fill=center_rgba)
    return img.resize((size, size), Image.LANCZOS)


# ── Main window ───────────────────────────────────────────────────────────────
class JeanClaudeCombien:
    def __init__(self):
        self.cfg              = Settings()
        self._start_time      = time.time()
        # Heartbeat updated on every _bg_fetch entry. Read by the watchdog
        # daemon-thread; a stale value means the scheduler died.
        self._last_tick_ts    = self._start_time
        self.root             = tk.Tk()
        # Mirror Tk-mainloop callback exceptions into monitor_fetch.log.
        # Without this they only reach sys.stderr — which under pythonw.exe
        # is /dev/null. Even after the startup redirect, having them in the
        # same triage file as `ok:`/`err:` ticks lets us spot the moment
        # scheduler-zombie started by glancing at one log.
        self.root.report_callback_exception = self._tk_exc
        self._body            = None
        self._rows_widgets    = []
        self._known_mtime     = 0.0
        self._refresh_id      = None
        # Tray state — deferred entry so the icon doesn't bake in 0 % on
        # the initial cold-cache refresh.
        self._tray_icon       = None
        self._hover_card      = None
        self._hover_hide_id   = None
        self._last_pct_5h     = 0.0
        self._last_pct_wk     = 0.0
        self._tray_wanted     = bool(self.cfg["tray"]) and TRAY_AVAILABLE
        self._build_window()
        self._build_content()
        self._fit_height()
        self._refresh_ui()
        # Startup breadcrumb: several incidents (7v1/7v3) left ZERO lines
        # in monitor_fetch.log even though Setup popped — we could never
        # tell whether _bg_fetch ran at all. Logging the PID + uptime at
        # __init__ gives every future incident an anchored "process N
        # started" marker to reason from.
        try:
            fetch_usage._log(
                f"init: monitor PID {os.getpid()} started "
                f"(sys_uptime {int(_system_uptime_s())}s)")
        except Exception:
            pass
        self._schedule_bg_fetch()
        # Independent daemon-thread that respawns the process if the
        # tk-mainloop-driven scheduler stops ticking. Lives outside tk
        # so it survives wake/standby races that silence root.after.
        threading.Thread(target=self._scheduler_watchdog,
                         name="scheduler-watchdog", daemon=True).start()
        if self._tray_wanted:
            self.root.after(5_000, self._enter_tray_if_pending)

    def _tk_exc(self, exc, val, tb):
        """Surface uncaught exceptions from Tk-mainloop callbacks.

        Tk routes errors raised by `root.after()` callbacks (including the
        UI-thread half of `_bg_fetch`) through `report_callback_exception`.
        Default implementation prints to sys.stderr — which is /dev/null
        under pythonw.exe. We mirror the formatted traceback into
        monitor_fetch.log so triage stays in one file, and also write it
        to stderr (now redirected by the startup block at the top of the
        module) for full multiline detail."""
        import traceback
        msg = "".join(traceback.format_exception(exc, val, tb))[:1200]
        try:
            fetch_usage._log("tk-exc: " + msg.replace("\n", " | "))
        except Exception:
            pass
        try:
            sys.stderr.write(msg)
            sys.stderr.flush()
        except Exception:
            pass

    def _t(self, key: str, **kwargs) -> str:
        return i18n.get(self.cfg["lang"], key, **kwargs)

    # ── Window (built once) ───────────────────────────────────────────────────
    def _build_window(self):
        r = self.root
        r.title("JeanClaudeCombien")
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.attributes("-alpha", self.cfg["opacity"])
        r.configure(bg=C["bg"])

        W = W_COMPACT if self.cfg["compact"] else W_FULL
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        x = self.cfg["pos_x"] if self.cfg["pos_x"] >= 0 else sw - W - 20
        y = self.cfg["pos_y"] if self.cfg["pos_y"] >= 0 else sh - 200 - 60
        if not _rect_on_screen(x, y, W, 200):
            x, y = sw - W - 20, sh - 200 - 60  # stranded on a gone monitor
        r.geometry(f"{W}x200+{x}+{y}")

        r.bind("<Button-1>",        self._drag_start)
        r.bind("<B1-Motion>",       self._drag_move)
        r.bind("<ButtonRelease-1>", self._drag_end)
        r.bind("<Button-3>",        self._ctx_menu)
        r.bind("<Double-Button-1>", self._on_double_click)

        # Header — hidden in dock mode; packed/unpacked by _build_content
        self._hdr = tk.Frame(r, bg=C["hdr"], height=24)
        self._hdr.pack_propagate(False)

        self._title_var = tk.StringVar(value="◆ JeanClaudeCombien")
        hdr_lbl = tk.Label(self._hdr, textvariable=self._title_var,
                           bg=C["hdr"], fg=C["accent"],
                           font=("Segoe UI", 8, "bold"), cursor="hand2")
        hdr_lbl.pack(side="left", padx=7)
        hdr_lbl.bind("<Double-Button-1>", lambda _: self._toggle_compact())

        x_lbl = tk.Label(self._hdr, text="✕", bg=C["hdr"], fg=C["muted"],
                         font=("Segoe UI", 10), cursor="hand2")
        x_lbl.pack(side="right", padx=5)
        x_lbl.bind("<Button-1>", lambda _: r.destroy())
        x_lbl.bind("<Enter>",    lambda _: x_lbl.config(fg=C["red"]))
        x_lbl.bind("<Leave>",    lambda _: x_lbl.config(fg=C["muted"]))

        self._upd_var = tk.StringVar(value="")
        tk.Label(self._hdr, textvariable=self._upd_var,
                 bg=C["hdr"], fg=C["muted"],
                 font=("Segoe UI", 7)).pack(side="right", padx=5)

    # ── Content (rebuilt on mode / language change) ───────────────────────────
    def _build_content(self):
        self._hdr.pack_forget()
        if self._body:
            self._body.destroy()

        if self.cfg["dock"]:
            self._build_dock()
            return

        self._hdr.pack(fill="x")
        compact = self.cfg["compact"]
        lang    = self.cfg["lang"]
        W       = W_COMPACT if compact else W_FULL

        self._title_var.set("◆ Claude" if compact else "◆ JeanClaudeCombien")

        self._body = tk.Frame(self.root, bg=C["bg"],
                              padx=6 if compact else 10)
        self._body.pack(fill="x", pady=(4, 5))

        # One blank row widget per current display row. Labels/icons live in
        # StringVars filled by _refresh_ui, so only a change in the NUMBER of
        # rows (a model limit appearing/disappearing) forces a rebuild — that
        # check lives in _refresh_ui. `or [None]` keeps at least one row so a
        # momentarily empty cache doesn't collapse the window.
        n = len(_display_rows(read_cache(), lang)) or 1
        self._rows_widgets = []
        for _ in range(n):
            w = self._make_compact_row(self._body) if compact else self._make_full_row(self._body)
            self._rows_widgets.append(w)

        x, y = self.root.winfo_x(), self.root.winfo_y()
        self.root.geometry(f"{W}x1+{x}+{y}")

    def _make_full_row(self, parent) -> dict:
        f = tk.Frame(parent, bg=C["bg"])
        f.pack(fill="x", pady=1)

        name_var = tk.StringVar(value="")
        tk.Label(f, textvariable=name_var, bg=C["bg"], fg=C["muted"],
                 font=("Segoe UI", 7), width=12, anchor="w").pack(side="left")

        canvas = tk.Canvas(f, height=5, bg=C["bar"],
                           highlightthickness=0, bd=0, width=62)
        canvas.pack(side="left", padx=(2, 3))

        pct_var = tk.StringVar(value="—")
        pct_lbl = tk.Label(f, textvariable=pct_var, bg=C["bg"], fg=C["text"],
                           font=("Segoe UI", 7), width=4, anchor="e")
        pct_lbl.pack(side="left")

        rst_var = tk.StringVar(value="")
        tk.Label(f, textvariable=rst_var, bg=C["bg"], fg=C["muted"],
                 font=("Segoe UI", 7)).pack(side="left", padx=(3, 0))

        return {"mode": "full", "canvas": canvas, "name_var": name_var,
                "pct_var": pct_var, "pct_lbl": pct_lbl, "rst_var": rst_var}

    def _make_compact_row(self, parent) -> dict:
        f = tk.Frame(parent, bg=C["bg"])
        f.pack(fill="x", pady=1)

        icon_var = tk.StringVar(value="")
        tk.Label(f, textvariable=icon_var, bg=C["bg"], fg=C["muted"],
                 font=("Segoe UI", 8), width=2).pack(side="left")

        pct_var = tk.StringVar(value="—")
        pct_lbl = tk.Label(f, textvariable=pct_var, bg=C["bg"], fg=C["text"],
                           font=("Segoe UI", 8, "bold"), width=5, anchor="e")
        pct_lbl.pack(side="left")

        rst_var = tk.StringVar(value="")
        tk.Label(f, textvariable=rst_var, bg=C["bg"], fg=C["muted"],
                 font=("Segoe UI", 7)).pack(side="left", padx=(5, 0))

        return {"mode": "compact", "icon_var": icon_var, "pct_var": pct_var,
                "pct_lbl": pct_lbl, "rst_var": rst_var}

    def _draw_bar(self, canvas: tk.Canvas, pct: float, color: str):
        canvas.update_idletasks()
        w = canvas.winfo_width() or 62
        canvas.delete("all")
        canvas.create_rectangle(0, 0, w, 5, fill=C["bar"], outline="")
        fw = int(w * min(pct, 100) / 100)
        if fw > 0:
            canvas.create_rectangle(0, 0, fw, 5, fill=color, outline="")

    def _fit_height(self):
        if self.cfg["dock"]:
            return  # geometry fixed by _build_dock
        self.root.update_idletasks()
        h = self.root.winfo_reqheight()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        W = W_COMPACT if self.cfg["compact"] else W_FULL
        self.root.geometry(f"{W}x{h}+{x}+{y}")

    # ── Data refresh ──────────────────────────────────────────────────────────
    def _refresh_ui(self):
        cache = read_cache()
        lang  = self.cfg["lang"]
        now   = datetime.now(tz=timezone.utc)   # single snapshot for the whole tick
        rows  = _display_rows(cache, lang)

        # Rebuild the widgets only when we actually have rows AND their count
        # changed (a scoped model limit appeared/disappeared). Guarding on
        # `rows` is essential: an empty cache (no file yet, or a transient
        # error) yields 0 rows while _build_content always makes at least 1
        # widget — without the guard that mismatch would rebuild → _refresh_ui
        # → rebuild forever (RecursionError). With rows present, the rebuild
        # makes exactly len(rows) widgets, so the next tick matches and stops.
        if rows and len(rows) != len(self._rows_widgets):
            self._rebuild_ui()
            return

        for i, row in enumerate(rows):
            pct = float(row["pct"] or 0)
            if row["reset"] and reset_passed(row["reset"], now):
                pct = 0.0  # stale cache after window rollover
            # Track 5h / week for the tray icon ring.
            if   row["kind"] == "session":    self._last_pct_5h = pct
            elif row["kind"] == "weekly_all": self._last_pct_wk = pct
            color = bar_color(pct)
            w     = self._rows_widgets[i]

            if row["credits"] is not None:
                rst_txt = _credits_text(row["credits"])
            else:
                rst_txt = fmt_reset(row["reset"], lang, 0, now)

            if w["mode"] == "dock":
                self.root.after(30 * i, lambda c=w["canvas"], p=pct, col=color:
                                self._draw_ring(c, p, col))
            else:
                display_pct = max(0.0, 100.0 - pct) if self.cfg["show_remaining"] else pct
                w["pct_var"].set(f"{display_pct:.0f}%")
                w["pct_lbl"].config(fg=pct_color(pct))
                w["rst_var"].set(rst_txt)
                if w["mode"] == "full":
                    w["name_var"].set(f"{row['icon']} {row['label']}")
                    self.root.after(30 * i, lambda c=w["canvas"], p=pct, col=color:
                                    self._draw_bar(c, p, col))
                else:  # compact
                    w["icon_var"].set(row["icon"])

        if cache.get("fetched_at"):
            try:
                dt = datetime.fromisoformat(cache["fetched_at"])
                self._upd_var.set(f"⟳ {dt.astimezone().strftime('%H:%M')}")
            except (ValueError, TypeError):
                pass

        self._reclaim_if_offscreen()

        if self.cfg["tray"] and self._tray_icon is not None:
            self._update_tray(cache, now)

        if self._refresh_id is not None:
            self.root.after_cancel(self._refresh_id)
        self._refresh_id = self.root.after(10_000, self._refresh_ui)

    def _reclaim_if_offscreen(self):
        """If a monitor was disconnected and our window is stranded, snap it back."""
        try:
            x, y = self.root.winfo_x(), self.root.winfo_y()
            w, h = self.root.winfo_width(), self.root.winfo_height()
        except Exception:
            return
        # Before Tk finishes laying out, winfo_width/height can return 1 from
        # the intermediate `{W}x1+{x}+{y}` set in _build_content. Reposition
        # decisions made then would put the window at nonsense coordinates.
        if w < 50 or h < 40:
            return
        if _rect_on_screen(x, y, w, h):
            return
        if self.cfg["dock"]:
            dw = self._dock_width()
            nx, ny = self._dock_snap_pos(dw, DOCK_H)
            self.root.geometry(f"{dw}x{DOCK_H}+{nx}+{ny}")
        else:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            W = W_COMPACT if self.cfg["compact"] else W_FULL
            # Center on the primary monitor — a deterministic visible spot
            # that survives DPI changes and monitor reshuffling.
            self.root.geometry(f"+{(sw - W) // 2}+{(sh - h) // 2}")

    # ── Mode & language ───────────────────────────────────────────────────────
    def _rebuild_ui(self):
        self._build_content()
        self.root.after(50, self._fit_height)
        self._refresh_ui()

    def _toggle_compact(self):
        self.cfg["compact"] = not self.cfg["compact"]
        self._rebuild_ui()

    def _set_lang(self, lang: str):
        self.cfg["lang"] = lang
        self._rebuild_ui()

    def _on_double_click(self, e):
        if self.cfg["dock"]:
            self._toggle_dock()
        # non-dock: header label handles its own double-click → toggle compact

    def _toggle_dock(self):
        self.cfg["dock"] = not self.cfg["dock"]
        self._rebuild_ui()

    # ── Dock mode helpers ─────────────────────────────────────────────────────
    def _dock_width(self) -> int:
        return len(self._rows_widgets) * (RING_SIZE + RING_PAD * 2) + 4

    def _dock_snap_pos(self, w: int, h: int) -> tuple:
        """Y: just above primary-monitor taskbar. X: saved dock_x, or a small
        default near the Start button. Falls back to the primary monitor if
        the saved X is stranded on a disconnected monitor."""
        try:
            wa = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETWORKAREA, 0, ctypes.byref(wa), 0)
            y = wa.bottom - h
        except Exception:
            y = self.root.winfo_screenheight() - h - TASKBAR_FALLBACK_H
        x = self.cfg["dock_x"] if self.cfg["dock_x"] >= 0 else DOCK_DEFAULT_X
        if not _rect_on_screen(x, y, w, h):
            x = DOCK_DEFAULT_X
        return x, y

    def _build_dock(self):
        """Build the dock strip: one ring canvas per row, no header."""
        self._body = tk.Frame(self.root, bg=C["bg"])
        self._body.pack(fill="both", expand=True)
        n = len(_display_rows(read_cache(), self.cfg["lang"])) or 1
        self._rows_widgets = []
        for _ in range(n):
            c = tk.Canvas(self._body, width=RING_SIZE, height=RING_SIZE,
                          bg=C["bg"], highlightthickness=0, bd=0)
            c.pack(side="left", padx=RING_PAD, pady=RING_PAD)
            self._rows_widgets.append({"mode": "dock", "canvas": c})
        dw = self._dock_width()
        dx, dy = self._dock_snap_pos(dw, DOCK_H)
        self.root.geometry(f"{dw}x{DOCK_H}+{dx}+{dy}")

    def _draw_ring(self, canvas: tk.Canvas, pct: float, color: str):
        """Draw a donut-ring progress indicator on canvas."""
        canvas.delete("all")
        s, p = RING_SIZE, 3
        display_pct = max(0.0, 100.0 - pct) if self.cfg["show_remaining"] else pct
        # Background ring (full circle)
        canvas.create_arc(p, p, s - p, s - p,
                          start=90, extent=-359.9,
                          style="arc", width=4, outline=C["bar"])
        # Progress arc
        if display_pct > 0:
            canvas.create_arc(p, p, s - p, s - p,
                              start=90,
                              extent=-min(max(1.0, 3.6 * display_pct), 359.9),
                              style="arc", width=4, outline=color)
        # Center percentage text
        canvas.create_text(s // 2, s // 2,
                           text=f"{display_pct:.0f}",
                           fill=color if pct >= 1 else C["muted"],
                           font=("Segoe UI", 7, "bold"))

    def _toggle_show_remaining(self):
        self.cfg["show_remaining"] = not self.cfg["show_remaining"]
        self._refresh_ui()

    # ── Background fetch ──────────────────────────────────────────────────────
    def _fetch_once(self) -> str:
        """Run one synchronous fetch and marshal the result onto the UI.
        Returns 'ok' | 'err' | 'expired'. Called only from the fetch
        scheduler thread (never the tk main thread), so it may block on
        the network freely."""
        err = None
        result = None
        try:
            result = fetch_usage.fetch_and_save()
        except Exception as e:
            err = type(e).__name__
            # A silenced exception here used to make monitor_fetch.log stop
            # growing while the process stayed alive. Surface every failure.
            try:
                fetch_usage._log(f"err: {err}: {str(e)[:160]}")
            except Exception:
                pass
        error_kind = result.get("error") if isinstance(result, dict) else None
        if error_kind in ("expired", "no_session"):
            # Auto-spawn of setup.py is permanently removed (incident series
            # 2026-05-13…2026-06-10): a valid key kept getting a transient
            # 'expired' right after reboot/wake while the network warmed up.
            # Show a persistent indicator; the next loop tick clears it once
            # the backend answers cleanly. A genuinely dead key is re-entered
            # by the user via the context menu (🔑 Setup sessionKey…).
            self.root.after(0, lambda: self._upd_var.set("⚠ session"))
            fetch_usage._log(
                f"expired ({error_kind}); indicator shown, periodic retry continues")
            return "expired"
        if err:
            self.root.after(0, lambda e=err: self._upd_var.set(f"⚠ {e}"))
            return "err"
        self.root.after(0, self._refresh_ui)
        # Enter tray only once the cache is populated so the icon's first
        # render already has real percentages.
        self.root.after(0, self._enter_tray_if_pending)
        return "ok"

    def _fetch_scheduler_loop(self):
        """Drive fetches from an independent daemon thread paced by
        time.sleep — NOT tk's root.after.

        ROOT-CAUSE FIX for the scheduler-zombie incidents (2026-05…06):
        tk's after() timers stop firing after the machine wakes from
        hibernation, so the old root.after-chained poll loop went silent
        while the process stayed alive (4 threads, zero fetches, frozen
        numbers — what looked like 'asks for login / won't connect'). A
        plain time.sleep loop survives hibernate: the thread just resumes
        late and keeps polling. UI writes still hop onto the tk thread via
        root.after(0, …), which keeps working because the mainloop itself
        is alive — only *deferred* timers were the broken part."""
        self._upd_var.set("↻ …")
        # Seed the heartbeat so the watchdog grants the first fetch its full
        # WATCHDOG_FATAL_S window to land before judging the process dead.
        self._last_tick_ts = time.time()
        # Cold-start guard (incident 2026-06-22): a monitor launched by the
        # Startup shortcut the instant the user logs in after wake races the
        # network stack. Firing fetches into a dead network drove the process
        # into a wedged state it never recovered from — a silent zombie that
        # logged nothing and fetched nothing for hours, while a process
        # started later in a warm system worked. Wait for a live socket
        # before the first fetch instead of a blind 3 s sleep.
        self._await_network_ready()
        fail_streak = 0
        while True:
            try:
                status = self._fetch_once()
            except Exception:
                status = "err"
            if status == "ok":
                # HEARTBEAT TRACKS SUCCESSFUL FETCHES ONLY (incident
                # 2026-06-22). A process that woke from hibernate kept this
                # loop spinning while every fetch failed silently — a broken
                # network stack *inside* the long-lived process, even though
                # a fresh process fetched in 3 s. When the heartbeat tracked
                # mere attempts, the watchdog saw a 'healthy' process and
                # never respawned it, so the overlay froze for days. Bumping
                # it only on success lets a failure run past WATCHDOG_FATAL_S
                # respawn the process — and the fresh one fetches cleanly.
                self._last_tick_ts = time.time()
                if fail_streak:
                    fetch_usage._log(f"recovered after {fail_streak} failed fetch(es)")
                fail_streak = 0
            else:
                fail_streak += 1
                fetch_usage._log(
                    f"fetch {status}; fail_streak={fail_streak} — heartbeat held, "
                    f"watchdog respawns after {WATCHDOG_FATAL_S}s of failures")
            # ok → full 3-min cadence; transient failure → retry in 30 s so a
            # cold-network miss right after wake recovers fast instead of
            # sitting on a stale cache for three minutes.
            wait_s = (REFETCH_INTERVAL_MS // 1000) if status == "ok" else 30
            slept = 0
            while slept < wait_s:
                time.sleep(min(5, wait_s - slept))
                slept += 5
                # buddy-tokens.json moved → fetch ahead of cadence, but no
                # more than ~once per 30 s to respect the API cooldown.
                if slept >= 30 and self._token_file_changed():
                    break

    def _await_network_ready(self, max_wait: int = 300):
        """Block until claude.ai:443 accepts a TCP connection, up to max_wait
        seconds. Cold-start guard (see the caller): a live socket means the
        network stack, DNS and any VPN/AV are up, so the first cloudscraper
        fetch won't hammer a dead connection and wedge the process into the
        silent-zombie state. If the network never comes up within max_wait we
        start anyway — the periodic loop and the cache-staleness watchdog
        recover from there."""
        import socket
        deadline = time.time() + max_wait
        attempt = 0
        while time.time() < deadline:
            try:
                socket.create_connection(("claude.ai", 443), timeout=5).close()
                if attempt:
                    fetch_usage._log(f"network ready after {attempt} probe(s)")
                return
            except OSError:
                attempt += 1
                self.root.after(0, lambda: self._upd_var.set("↻ net…"))
                time.sleep(5)
        fetch_usage._log(f"network not ready after {max_wait}s; fetching anyway")

    def _spawn_setup_now(self):
        """Launch setup.py to (re-)enter the sessionKey. Only reached from the
        context menu (🔑 Setup sessionKey…) — never automatically. Guards
        against spawning a second dialog while one is still open."""
        proc = getattr(self, "_setup_proc", None)
        if proc is not None and proc.poll() is None:
            return  # a setup window is already open
        setup_path = Path(__file__).with_name("setup.py")
        if not setup_path.exists():
            return
        try:
            self._setup_proc = subprocess.Popen(
                [sys.executable, str(setup_path)],
                cwd=str(setup_path.parent),
            )
            fetch_usage._log("spawn: setup.py launched")
        except OSError as e:
            self._setup_proc = None
            fetch_usage._log(f"spawn: Popen failed {type(e).__name__}")

    def _scheduler_watchdog(self):
        """Daemon-thread heartbeat monitor. If _bg_fetch hasn't ticked
        for >WATCHDOG_FATAL_S the scheduler is dead: tk-mainloop hung,
        root.after callbacks silenced by wake/standby, or the periodic
        timer chain broke mid-run. We can't reliably resurrect it from
        inside the same interpreter — Popen a fresh process and exit.

        Runs in its own thread so it doesn't depend on the very loop
        it's supposed to monitor. Sleeps in 60 s slices. Catches all
        exceptions so a fluke (file permissions, transient os._exit
        failure) never silences the watchdog itself."""
        while True:
            try:
                time.sleep(60)
                stale = time.time() - getattr(self, "_last_tick_ts", self._start_time)
                proc_age = time.time() - self._start_time
                # Two independent respawn triggers:
                #  1) heartbeat stale — the loop stopped ticking at all;
                #  2) cache stale — the loop ticks but no fetch has SUCCEEDED
                #     (written the cache) for CACHE_STALE_S. This catches the
                #     silent-fetch zombie (incident 2026-06-22) whose heartbeat
                #     looked healthy while it fetched nothing. Gated on
                #     proc_age so a freshly started process (which may legit-
                #     imately wait on _await_network_ready before its first
                #     write) isn't killed before it gets a chance.
                cache_stale = None
                try:
                    cache_stale = time.time() - CACHE_FILE.stat().st_mtime
                except OSError:
                    pass
                reason = None
                if stale > WATCHDOG_FATAL_S:
                    reason = f"scheduler stale {stale:.0f}s >{WATCHDOG_FATAL_S}s"
                elif (proc_age > CACHE_STALE_S and cache_stale is not None
                        and cache_stale > CACHE_STALE_S):
                    reason = (f"cache stale {cache_stale:.0f}s >{CACHE_STALE_S}s "
                              f"(silent-fetch zombie)")
                if reason:
                    try:
                        fetch_usage._log(
                            f"watchdog: {reason}; respawning self and exiting")
                    except Exception:
                        pass
                    try:
                        subprocess.Popen(
                            [sys.executable, __file__],
                            cwd=str(Path(__file__).parent),
                        )
                    except Exception:
                        pass
                    os._exit(2)
            except Exception:
                # Never crash the watchdog on a transient OS hiccup —
                # the next loop iteration retries cleanly.
                pass

    # ── Tray mode ─────────────────────────────────────────────────────────────
    def _enter_tray_if_pending(self):
        """Honor a pending startup request to enter tray (called once after
        the first fetch succeeds, or by a 5 s fallback timer)."""
        if self._tray_wanted and self._tray_icon is None:
            self._tray_wanted = False
            self._enter_tray()

    def _toggle_tray(self):
        if not TRAY_AVAILABLE:
            return
        if self.cfg["tray"]:
            self._exit_tray()
        else:
            self._enter_tray()

    def _enter_tray(self):
        """Hide overlay, spawn a system-tray icon. Abort cleanly on any
        rendering / pystray failure so the window isn't left withdrawn
        with no tray control to bring it back."""
        try:
            icon_img = self._build_tray_image(self._last_pct_5h, self._last_pct_wk)
            tooltip  = self._build_tray_tooltip()
        except Exception:
            return
        self.cfg["tray"] = True
        self.root.withdraw()

        def on_show(icon, item):    self.root.after(0, self._show_hover_card)
        def on_restore(icon, item): self.root.after(0, self._exit_tray)
        def on_quit(icon, item):
            self.cfg["tray"] = False
            try: icon.stop()
            except Exception: pass
            self.root.after(0, self._quit_from_tray)

        menu = pystray.Menu(
            pystray.MenuItem(self._t("menu_tray"), on_show, default=True, visible=False),
            pystray.MenuItem(self._t("menu_exit_tray"), on_restore),
            pystray.MenuItem(self._t("menu_close"), on_quit),
        )
        self._tray_icon = pystray.Icon("JeanClaudeCombien", icon_img,
                                       title=tooltip, menu=menu)
        self._tray_icon._uid  = TRAY_UID
        self._tray_icon._guid = _make_guid(*TRAY_GUID)
        self._last_tray_pcts  = (self._last_pct_5h, self._last_pct_wk)
        self._tray_icon.run_detached()

    def _exit_tray(self):
        self.cfg["tray"] = False
        if self._tray_icon is not None:
            try: self._tray_icon.stop()
            except Exception: pass
            self._tray_icon = None
        self._hide_hover_card()
        self.root.deiconify()

    def _quit_from_tray(self):
        self._hide_hover_card()
        try: self.root.destroy()
        except Exception: pass

    def _update_tray(self, cache: dict, now: "datetime"):
        if self._tray_icon is None:
            return
        pcts = (self._last_pct_5h, self._last_pct_wk)
        if pcts == getattr(self, "_last_tray_pcts", None):
            self._tray_icon.title = self._build_tray_tooltip(cache, now)
            return
        try:
            self._tray_icon.icon  = self._build_tray_image(*pcts)
            self._tray_icon.title = self._build_tray_tooltip(cache, now)
            self._last_tray_pcts  = pcts
        except Exception:
            pass

    def _build_tray_image(self, pct_5h: float, pct_wk: float):
        """One bold ring (5h) + bright brand-tinted near-white center disc."""
        return _render_single_ring(
            TRAY_ICON_SIZE, pct_5h,
            ring_color(pct_5h),
            (110, 70, 40, 90),                 # semi-transparent warm track
            TRAY_RING_STROKE,
            center_rgba=(255, 230, 200, 255),  # near-white with Claude tint
            edge_margin=TRAY_EDGE_MARGIN,
            outline_rgba=(60, 30, 10, 220),    # dark warm contour
            outline_width=1,
        )

    def _build_tray_tooltip(self, cache: "dict | None" = None,
                            now: "datetime | None" = None) -> str:
        if cache is None: cache = read_cache()
        if now   is None: now   = datetime.now(tz=timezone.utc)
        lang  = self.cfg["lang"]
        lines = ["JeanClaudeCombien"]
        for row in _display_rows(cache, lang):
            if row["credits"] is not None:
                lines.append(f"{row['icon']} {row['label']}: "
                             f"{_credits_text(row['credits'])}".rstrip())
            else:
                pct = float(row["pct"] or 0)
                if row["reset"] and reset_passed(row["reset"], now):
                    pct = 0.0
                rst = fmt_reset(row["reset"], lang, 0, now)
                lines.append(f"{row['icon']} {row['label']}: {pct:.0f}%   {rst}")
        return _truncate_utf16("\n".join(lines))

    def _show_hover_card(self):
        self._hide_hover_card()
        card = tk.Toplevel(self.root)
        card.overrideredirect(True)
        card.attributes("-topmost", True)
        card.attributes("-alpha", self.cfg["opacity"])
        card.configure(bg=C["bg"])

        hdr = tk.Frame(card, bg=C["hdr"])
        hdr.pack(fill="x")
        tk.Label(hdr, text="◆ JeanClaudeCombien", bg=C["hdr"], fg=C["accent"],
                 font=("Segoe UI", 8, "bold")).pack(side="left", padx=7, pady=3)
        close = tk.Label(hdr, text="✕", bg=C["hdr"], fg=C["muted"],
                         font=("Segoe UI", 9), cursor="hand2")
        close.pack(side="right", padx=5)
        close.bind("<Button-1>", lambda _: self._hide_hover_card())

        body = tk.Frame(card, bg=C["bg"], padx=10, pady=6)
        body.pack(fill="x")
        cache = read_cache()
        lang  = self.cfg["lang"]
        now   = datetime.now(tz=timezone.utc)
        for row in _display_rows(cache, lang):
            r = tk.Frame(body, bg=C["bg"])
            r.pack(fill="x", pady=1)
            tk.Label(r, text=f"{row['icon']} {row['label']}",
                     bg=C["bg"], fg=C["muted"], font=("Segoe UI", 8),
                     width=12, anchor="w").pack(side="left")
            if row["credits"] is not None:
                tk.Label(r, text=_credits_text(row["credits"]),
                         bg=C["bg"], fg=C["text"], font=("Segoe UI", 8)).pack(side="left")
            else:
                pct = float(row["pct"] or 0)
                if row["reset"] and reset_passed(row["reset"], now):
                    pct = 0.0
                tk.Label(r, text=f"{pct:.0f}%", bg=C["bg"], fg=pct_color(pct),
                         font=("Segoe UI", 8, "bold"), width=5, anchor="e").pack(side="left")
                tk.Label(r, text=fmt_reset(row["reset"], lang, 0, now),
                         bg=C["bg"], fg=C["muted"], font=("Segoe UI", 8)
                         ).pack(side="left", padx=(6, 0))

        card.update_idletasks()
        w, h = card.winfo_reqwidth(), card.winfo_reqheight()
        cx, cy = self.root.winfo_pointerx(), self.root.winfo_pointery()
        x, y = cx - w // 2, cy - h - 12
        vs = _virtual_screen_rect() or (0, 0, 1920, 1080)
        vl, vt, vr, vb = vs
        x = max(vl + 4, min(x, vr - w - 4))
        y = max(vt + 4, min(y, vb - h - 4))
        card.geometry(f"{w}x{h}+{x}+{y}")
        card.bind("<FocusOut>",   lambda _: self._hide_hover_card())
        card.bind("<Button-1>",   lambda _: self._hide_hover_card())
        card.focus_force()
        self._hover_card    = card
        self._hover_hide_id = self.root.after(8_000, self._hide_hover_card)

    def _hide_hover_card(self):
        if self._hover_hide_id is not None:
            try: self.root.after_cancel(self._hover_hide_id)
            except Exception: pass
            self._hover_hide_id = None
        if self._hover_card is not None:
            try: self._hover_card.destroy()
            except Exception: pass
            self._hover_card = None

    def _token_file_changed(self) -> bool:
        """True (once) when buddy-tokens.json has a newer mtime than last
        seen — Claude Desktop just wrote fresh token counts, so fetch ahead
        of the normal cadence. Self-updates the baseline."""
        try:
            mt = TOKENS_FILE.stat().st_mtime if TOKENS_FILE.exists() else 0.0
            if mt > self._known_mtime:
                self._known_mtime = mt
                return True
        except Exception:
            pass
        return False

    def _schedule_bg_fetch(self):
        """Start the self-pacing fetch thread. Called once from __init__.
        Replaces the old root.after fetch/watch/periodic chain, which went
        silent after hibernate (see _fetch_scheduler_loop for the why)."""
        try:
            self._known_mtime = TOKENS_FILE.stat().st_mtime if TOKENS_FILE.exists() else 0.0
        except Exception:
            self._known_mtime = 0.0
        threading.Thread(target=self._fetch_scheduler_loop,
                         name="fetch-scheduler", daemon=True).start()

    # ── Drag ──────────────────────────────────────────────────────────────────
    def _drag_start(self, e): self._ox, self._oy = e.x, e.y
    def _drag_move(self, e):
        x = self.root.winfo_x() + e.x - self._ox
        y = self.root.winfo_y() + e.y - self._oy
        self.root.geometry(f"+{x}+{y}")
    def _drag_end(self, e):
        if self.cfg["dock"]:
            self.cfg["dock_x"] = self.root.winfo_x()
        else:
            self.cfg["pos_x"] = self.root.winfo_x()
            self.cfg["pos_y"] = self.root.winfo_y()

    # ── Context menu ──────────────────────────────────────────────────────────
    def _ctx_menu(self, e):
        lang = self.cfg["lang"]
        m = tk.Menu(self.root, tearoff=0, bg=C["bg2"], fg=C["text"],
                    activebackground=C["accent"], font=("Segoe UI", 9), bd=0)

        if self.cfg["dock"]:
            m.add_command(label=self._t("menu_exit_dock"), command=self._toggle_dock)
        else:
            mode_key = "menu_full" if self.cfg["compact"] else "menu_compact"
            m.add_command(label=self._t(mode_key), command=self._toggle_compact)
            m.add_command(label=self._t("menu_dock"), command=self._toggle_dock)
            if TRAY_AVAILABLE:
                m.add_command(label=self._t("menu_tray"), command=self._toggle_tray)

        pct_key = "menu_show_used" if self.cfg["show_remaining"] else "menu_show_remaining"
        m.add_command(label=self._t(pct_key), command=self._toggle_show_remaining)
        m.add_separator()

        opacity_items = [(a, f"{int(a * 100)}%") for a in (1.0, 0.92, 0.80, 0.60)]
        m.add_cascade(label=self._t("menu_opacity"),
                      menu=self._submenu(m, opacity_items, self.cfg["opacity"],
                                         self._set_opacity,
                                         eq=lambda a, b: abs(a - b) < 0.01))

        lang_items = list(i18n.LANGUAGES.items())
        m.add_cascade(label=self._t("menu_language"),
                      menu=self._submenu(m, lang_items, lang, self._set_lang))

        m.add_separator()
        m.add_command(label="🔑 Setup sessionKey…",
                      command=self._spawn_setup_now)
        m.add_command(label=self._t("menu_close"), command=self.root.destroy)
        m.post(e.x_root, e.y_root)

    def _submenu(self, parent, items, current, on_select, eq=None):
        """Build a submenu with a ✓-prefix on the item matching `current`."""
        sub = tk.Menu(parent, tearoff=0, bg=C["bg2"], fg=C["text"],
                      activebackground=C["accent"], font=("Segoe UI", 9))
        match = eq or (lambda a, b: a == b)
        for value, label in items:
            mark = "✓  " if match(current, value) else "    "
            sub.add_command(label=f"{mark}{label}",
                            command=lambda v=value: on_select(v))
        return sub

    def _set_opacity(self, v):
        self.cfg["opacity"] = v
        self.root.attributes("-alpha", v)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    JeanClaudeCombien().run()
