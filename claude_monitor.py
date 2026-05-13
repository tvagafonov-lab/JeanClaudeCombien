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
SPAWN_STATE   = CLAUDE_DIR / "monitor_spawn_state.json"   # persistent setup-spawn cooldown

# Auto-spawn guards: every threshold below has cost real-world false-positive
# rescues. Do not loosen without a repro.
SPAWN_MIN_UPTIME_S      = 300        # ignore the first 5 min after monitor start
SPAWN_MIN_CACHE_AGE_S   = 1800       # cache must be older than 30 min
SPAWN_MIN_EXPIRED_STREAK = 5         # 5 expired ticks, ≥60 s apart ≈ 5 min wall time
SPAWN_PERSIST_COOLDOWN_S = 6 * 3600  # 6 h between auto-spawns, survives reboot

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

ROWS = [
    # (pct_key, reset_key, icon, label_key, window_seconds)
    ("fh_pct", "fh_reset", "⏱", "row_5h",      5 * 3600),
    ("wd_pct", "wd_reset", "📅", "row_week",   7 * 86400),
    ("sn_pct", "sn_reset", "✨", "row_sonnet", 7 * 86400),
    ("dz_pct", "dz_reset", "🎨", "row_design", 7 * 86400),
    ("ex_pct", None,       "💳", "row_credits", 0),
]


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


def _pil_color(hex_str: str) -> tuple:
    """Convert '#rrggbb' to an opaque RGBA tuple for Pillow."""
    h = hex_str.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)


def _load_spawn_state() -> float:
    """Return the unix timestamp of the last auto-spawn, or 0.0 if absent.
    Persisted so the 6 h cooldown survives Windows logout/reboot — without
    this, every login was its own brand-new 'first failure → spawn setup'."""
    try:
        return float(json.loads(SPAWN_STATE.read_text("utf-8")).get("last_spawn_ts", 0))
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def _save_spawn_state(ts: float) -> None:
    try:
        SPAWN_STATE.write_text(json.dumps({"last_spawn_ts": ts}), encoding="utf-8")
    except OSError:
        pass


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
        self._start_time      = time.time()   # for SPAWN_MIN_UPTIME_S guard
        self.root             = tk.Tk()
        self._body            = None
        self._rows_widgets    = []
        self._known_mtime     = 0.0
        self._last_fetch_time = 0.0
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
        self._schedule_bg_fetch()
        if self._tray_wanted:
            self.root.after(5_000, self._enter_tray_if_pending)

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

        self._rows_widgets = []
        for key_pct, key_rst, icon, name_key, _win_s in ROWS:
            name = i18n.get(lang, name_key)
            if compact:
                w = self._make_compact_row(self._body, icon)
            else:
                w = self._make_full_row(self._body, icon, name)
            self._rows_widgets.append(w)

        x, y = self.root.winfo_x(), self.root.winfo_y()
        self.root.geometry(f"{W}x1+{x}+{y}")

    def _make_full_row(self, parent, icon: str, name: str) -> dict:
        f = tk.Frame(parent, bg=C["bg"])
        f.pack(fill="x", pady=1)

        tk.Label(f, text=f"{icon} {name}", bg=C["bg"], fg=C["muted"],
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

        return {"mode": "full", "canvas": canvas,
                "pct_var": pct_var, "pct_lbl": pct_lbl, "rst_var": rst_var}

    def _make_compact_row(self, parent, icon: str) -> dict:
        f = tk.Frame(parent, bg=C["bg"])
        f.pack(fill="x", pady=1)

        tk.Label(f, text=icon, bg=C["bg"], fg=C["muted"],
                 font=("Segoe UI", 8), width=2).pack(side="left")

        pct_var = tk.StringVar(value="—")
        pct_lbl = tk.Label(f, textvariable=pct_var, bg=C["bg"], fg=C["text"],
                           font=("Segoe UI", 8, "bold"), width=5, anchor="e")
        pct_lbl.pack(side="left")

        rst_var = tk.StringVar(value="")
        tk.Label(f, textvariable=rst_var, bg=C["bg"], fg=C["muted"],
                 font=("Segoe UI", 7)).pack(side="left", padx=(5, 0))

        return {"mode": "compact", "pct_var": pct_var, "pct_lbl": pct_lbl,
                "rst_var": rst_var}

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

        for i, (key_pct, key_rst, icon, name_key, win_s) in enumerate(ROWS):
            pct = float(cache.get(key_pct, 0))
            if key_rst is not None and reset_passed(cache.get(key_rst), now):
                pct = 0.0  # stale cache after window rollover
            if   key_pct == "fh_pct": self._last_pct_5h = pct
            elif key_pct == "wd_pct": self._last_pct_wk = pct
            color = bar_color(pct)
            w     = self._rows_widgets[i]

            if key_rst is None:  # Credits row
                used    = cache.get("ex_used",  0)
                limit   = cache.get("ex_limit", 0)
                curr    = "€" if cache.get("ex_curr") == "EUR" else cache.get("ex_curr", "")
                rst_txt = f"{used:.2f} / {limit:.2f} {curr}"
            else:
                rst_txt = fmt_reset(cache.get(key_rst), lang, win_s, now)

            if w["mode"] == "dock":
                self.root.after(30 * i, lambda c=w["canvas"], p=pct, col=color:
                                self._draw_ring(c, p, col))
            else:
                display_pct = max(0.0, 100.0 - pct) if self.cfg["show_remaining"] else pct
                w["pct_var"].set(f"{display_pct:.0f}%")
                w["pct_lbl"].config(fg=pct_color(pct))
                w["rst_var"].set(rst_txt)
                if w["mode"] == "full":
                    self.root.after(30 * i, lambda c=w["canvas"], p=pct, col=color:
                                    self._draw_bar(c, p, col))

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
        return len(ROWS) * (RING_SIZE + RING_PAD * 2) + 4

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
        self._rows_widgets = []
        for _key_pct, _key_rst, _icon, _name_key, _win_s in ROWS:
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
    def _bg_fetch(self):
        def run():
            err = None
            result = None
            try:
                result = fetch_usage.fetch_and_save()
            except Exception as e:
                err = type(e).__name__
            error_kind = result.get("error") if isinstance(result, dict) else None
            # "expired" auto-opens setup only after the second tick still
            # says expired AND those ticks are at least 60 s apart. A
            # single 403 — or two 403s in quick succession because of an
            # exp-backoff retry against the same Cloudflare fingerprint —
            # is treated as transient. "no_session" (file genuinely
            # missing) is unambiguous and opens the dialog immediately.
            now = time.time()
            if error_kind == "expired":
                last = getattr(self, "_last_expired_ts", 0)
                if now - last >= 60:
                    self._expired_streak = getattr(self, "_expired_streak", 0) + 1
                    self._last_expired_ts = now
            else:
                self._expired_streak = 0
                self._last_expired_ts = 0
            confirm_expired = (error_kind == "no_session"
                               or (error_kind == "expired"
                                   and self._expired_streak >= SPAWN_MIN_EXPIRED_STREAK))
            if confirm_expired:
                # Always surface the warning. Whether to escalate to a setup
                # dialog is decided by _maybe_auto_spawn_setup — it enforces
                # the uptime / cache-age / persistent-cooldown guards that
                # past patches kept failing to enforce. The header indicator
                # is the cheap always-on signal.
                self._fetch_backoff_ms = 5_000
                self.root.after(0, lambda: self._upd_var.set("⚠ session"))
                self.root.after(0, lambda ek=error_kind:
                                self._maybe_auto_spawn_setup(ek))
            elif error_kind == "expired":
                # Surface in header; do NOT schedule an aggressive retry —
                # let the 3-min periodic tick decide if it's still expired.
                self.root.after(0, lambda: self._upd_var.set("⚠ retry"))
            elif err:
                self.root.after(0, lambda: self._upd_var.set(f"⚠ {err}"))
                # Right after sign-in the network stack may not be up yet —
                # retry with exponential backoff so we don't sit on a cold
                # cache until the 3-min fallback.
                retry_ms = getattr(self, "_fetch_backoff_ms", 5_000)
                if retry_ms <= 60_000:
                    self.root.after(retry_ms, self._bg_fetch)
                    self._fetch_backoff_ms = retry_ms * 3
            else:
                self._fetch_backoff_ms = 5_000
                self.root.after(0, self._refresh_ui)
                # Enter tray only once the cache is populated so the icon's
                # first render already has real percentages.
                self.root.after(0, self._enter_tray_if_pending)
        threading.Thread(target=run, daemon=True).start()
        self._upd_var.set("↻ …")

    def _maybe_auto_spawn_setup(self, error_kind: str):
        """Run the full guard chain before delegating to _prompt_session_refresh.

        History: this is the fifth iteration. Previous patches loosened or
        tightened single thresholds and the bug kept coming back. This
        version requires ALL of:
          - monitor uptime ≥ SPAWN_MIN_UPTIME_S
              (a cold-start network race must not be enough on its own;
               cf. the post-signin "no_session" symptom seen at +5 s).
          - on-disk cache age ≥ SPAWN_MIN_CACHE_AGE_S
              (if the cache is recent, the key is provably working — any
               'expired' is a transient API/Cloudflare hiccup, not auth).
          - SPAWN_MIN_EXPIRED_STREAK consecutive 'expired' ticks, ≥60 s
            apart (already enforced by _expired_streak in caller for the
            "expired" branch; trivially satisfied by "no_session").
          - persistent cooldown via SPAWN_STATE on disk
              (in-memory _setup_spawn_ts didn't survive reboot, so every
               Windows login was its own cold-start race all over again).
        """
        now = time.time()
        uptime = now - self._start_time
        if uptime < SPAWN_MIN_UPTIME_S:
            fetch_usage._log(
                f"auto-spawn: blocked uptime={uptime:.0f}s "
                f"<{SPAWN_MIN_UPTIME_S}s (cold-start race window)")
            return
        try:
            cache_age = now - CACHE_FILE.stat().st_mtime
        except OSError:
            cache_age = float("inf")
        if cache_age < SPAWN_MIN_CACHE_AGE_S:
            fetch_usage._log(
                f"auto-spawn: blocked cache_age={cache_age:.0f}s "
                f"<{SPAWN_MIN_CACHE_AGE_S}s (cache still fresh — key works)")
            return
        last_spawn = _load_spawn_state()
        cooldown_left = SPAWN_PERSIST_COOLDOWN_S - (now - last_spawn)
        if cooldown_left > 0:
            fetch_usage._log(
                f"auto-spawn: blocked cooldown_left={cooldown_left:.0f}s "
                f"({SPAWN_PERSIST_COOLDOWN_S}s persistent window)")
            return
        fetch_usage._log(
            f"auto-spawn: all guards passed (error_kind={error_kind} "
            f"uptime={uptime:.0f}s cache_age={cache_age:.0f}s) → prompt")
        self._prompt_session_refresh()

    def _prompt_session_refresh(self):
        """Spawn the setup dialog. Hard-guarded so a runaway upstream
        signal can't reopen the window:
          1) one process at a time (poll the previous Popen),
          2) 1-hour cooldown between successful spawns,
          3) cache-freshness gate — if the on-disk cache has valid
             percentages with a fetched_at < 10 minutes old, the key
             is definitely working; suppress the dialog.
          4) final synchronous re-fetch before spawning — open the
             dialog ONLY if the fetch explicitly returns
             {'error': 'expired' | 'no_session'}."""
        fetch_usage._log("prompt: _prompt_session_refresh called")
        proc = getattr(self, "_setup_proc", None)
        if proc is not None and proc.poll() is None:
            fetch_usage._log("prompt: suppressed (previous dialog still open)")
            return
        last = getattr(self, "_setup_spawn_ts", 0)
        if time.time() - last < 3600:
            fetch_usage._log(f"prompt: suppressed (cooldown, {int(time.time()-last)}s since last spawn)")
            return
        if self._cache_is_fresh():
            fetch_usage._log("prompt: suppressed (cache has fresh valid data)")
            return

        def verify_and_maybe_spawn():
            try:
                res = fetch_usage.fetch_and_save()
            except Exception as e:
                fetch_usage._log(f"prompt: verify raised {type(e).__name__}; not spawning")
                return
            if isinstance(res, dict) and res.get("error") in ("expired", "no_session"):
                fetch_usage._log(f"prompt: verify confirmed {res['error']}; spawning setup")
                self.root.after(0, self._spawn_setup_now)
            else:
                fetch_usage._log("prompt: verify returned OK; not spawning")
        threading.Thread(target=verify_and_maybe_spawn, daemon=True).start()

    def _cache_is_fresh(self) -> bool:
        """True if the usage cache currently has valid percentages and
        was written within the last 10 minutes. Used as a circuit breaker
        — if the cache is fresh-and-valid, the API is clearly working,
        so any 'expired' signal upstream must be a false positive."""
        try:
            cache = json.loads(CACHE_FILE.read_text("utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(cache, dict) or "error" in cache or "fh_pct" not in cache:
            return False
        try:
            fetched = datetime.fromisoformat(cache["fetched_at"]).timestamp()
        except (KeyError, ValueError, TypeError):
            return False
        return time.time() - fetched < 600

    def _spawn_setup_now(self):
        setup_path = Path(__file__).with_name("setup.py")
        if not setup_path.exists():
            return
        try:
            self._setup_proc = subprocess.Popen(
                [sys.executable, str(setup_path)],
                cwd=str(setup_path.parent),
            )
            self._setup_spawn_ts = time.time()
            _save_spawn_state(self._setup_spawn_ts)   # 6 h persistent cooldown
            fetch_usage._log("spawn: setup.py launched")
        except OSError as e:
            self._setup_proc = None
            fetch_usage._log(f"spawn: Popen failed {type(e).__name__}")

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
        for key_pct, key_rst, icon, name_key, win_s in ROWS:
            name = i18n.get(lang, name_key)
            if key_rst is None:
                used  = cache.get("ex_used",  0)
                limit = cache.get("ex_limit", 0)
                curr  = "€" if cache.get("ex_curr") == "EUR" else cache.get("ex_curr", "")
                lines.append(f"{icon} {name}: {used:.2f} / {limit:.2f} {curr}".rstrip())
            else:
                pct = float(cache.get(key_pct, 0))
                if reset_passed(cache.get(key_rst), now):
                    pct = 0.0
                rst = fmt_reset(cache.get(key_rst), lang, win_s, now)
                lines.append(f"{icon} {name}: {pct:.0f}%   {rst}")
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
        for key_pct, key_rst, icon, name_key, win_s in ROWS:
            row = tk.Frame(body, bg=C["bg"])
            row.pack(fill="x", pady=1)
            tk.Label(row, text=f"{icon} {i18n.get(lang, name_key)}",
                     bg=C["bg"], fg=C["muted"], font=("Segoe UI", 8),
                     width=12, anchor="w").pack(side="left")
            if key_rst is None:
                used  = cache.get("ex_used",  0)
                limit = cache.get("ex_limit", 0)
                curr  = "€" if cache.get("ex_curr") == "EUR" else cache.get("ex_curr", "")
                tk.Label(row, text=f"{used:.2f} / {limit:.2f} {curr}",
                         bg=C["bg"], fg=C["text"], font=("Segoe UI", 8)).pack(side="left")
            else:
                pct = float(cache.get(key_pct, 0))
                if reset_passed(cache.get(key_rst), now):
                    pct = 0.0
                tk.Label(row, text=f"{pct:.0f}%", bg=C["bg"], fg=pct_color(pct),
                         font=("Segoe UI", 8, "bold"), width=5, anchor="e").pack(side="left")
                tk.Label(row, text=fmt_reset(cache.get(key_rst), lang, win_s, now),
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

    def _watch_tokens(self):
        """Every 5 s: re-fetch if buddy-tokens.json changed (30 s API cooldown)."""
        try:
            mt  = TOKENS_FILE.stat().st_mtime if TOKENS_FILE.exists() else 0.0
            now = time.time()
            if mt > self._known_mtime and now - self._last_fetch_time >= 30:
                self._known_mtime     = mt
                self._last_fetch_time = now
                self._bg_fetch()
        except Exception:
            pass
        self.root.after(5_000, self._watch_tokens)

    def _schedule_bg_fetch(self):
        """Initial fetch on startup + periodic fallback + file watcher."""
        try:
            self._known_mtime = TOKENS_FILE.stat().st_mtime if TOKENS_FILE.exists() else 0.0
        except Exception:
            self._known_mtime = 0.0
        self._last_fetch_time = time.time()
        # Small delay on the *first* fetch after startup so the Windows
        # network stack has time to come up — the most common cause of
        # first-fetch failure after sign-in / unlock.
        delay = 0 if getattr(self, "_first_fetch_done", False) else 3_000
        self._first_fetch_done = True
        self.root.after(delay, self._bg_fetch)
        self.root.after(5_000, self._watch_tokens)
        # Fallback: re-fetch every 3 min regardless (browser use, session refresh)
        self.root.after(REFETCH_INTERVAL_MS, self._schedule_bg_fetch)

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
