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
import json, os, time, threading
from pathlib import Path
from datetime import datetime, timedelta, timezone
import i18n
import fetch_usage

# Give Windows a unique AppUserModelID so the shell treats this pythonw.exe
# instance as its own application — lets us co-exist in the system tray with
# sibling pythonw overlays (CHB, etc.) and gives taskbar / Start menu a
# stable identity. Must run before any tk.Tk() / tray icon creation.
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
        "JeanClaudeCombien.Overlay.1")
except Exception:
    pass

# Tray-mode deps are optional — the overlay works fully without them if the
# user never enables tray. PIL is also used for dock rings: tkinter's Canvas
# has no AA, so we supersample through Pillow and display via ImageTk.
try:
    import pystray
    import pystray._win32 as _pystray_win32
    from pystray._win32 import win32 as _pystray_w32
    from PIL import Image, ImageDraw, ImageTk
    TRAY_AVAILABLE = True

    # Pystray uses `hID = id(self)` in NOTIFYICONDATAW, which collides across
    # sibling processes sharing the same pythonw.exe — Windows 11 indexes
    # NotifyIconSettings by (ExecutablePath, uID) and silently merges the
    # duplicate. Robust fix: NIF_GUID identifies the icon by GUID instead,
    # so different apps get independent registrations / taskbar entries.
    def _pystray_message_patched(self, code, flags, **kwargs):
        import ctypes
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

    def _make_guid(d1: int, d2: int, d3: int, d4: tuple) -> object:
        import ctypes
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

DEFAULT_SETTINGS = {
    "opacity":        0.92,
    "compact":        False,
    "dock":           False,
    "tray":           False,   # fourth mode: hidden overlay, system-tray icon
    "tray_hinted":    False,   # shown the "look in overflow ^" toast once
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

RING_SIZE           = 40   # ring canvas size (px) in dock mode
RING_PAD            = 2    # padding around each ring canvas
DOCK_H              = RING_SIZE + RING_PAD * 2 + 2   # close to Win11 taskbar (44 px)
DOCK_RING_STROKE    = 7    # ring thickness in dock mode
DOCK_DEFAULT_X      = 80   # default dock X near the Win11 Start button
TASKBAR_FALLBACK_H  = 48   # assumed taskbar height if SPI_GETWORKAREA fails
REFETCH_INTERVAL_MS = 300_000   # session-refresh fallback (browser path updates cookies)
# Tray icons are drawn at 64×64 then Windows scales them to the active tray
# size (16 / 20 / 24 px depending on DPI). Pillow supersamples 4× internally
# and LANCZOS-downsamples to 64 for antialiased ring edges.
TRAY_ICON_SIZE      = 64
# One bold ring (5h) + large brand center disc. On the 16 px tray canvas
# two concentric rings blurred into noise; a single ring + punchy center
# reads instantly. Week usage moves to the tooltip + hover card.
TRAY_RING_STROKE    = 14   # outer ring thickness at target size
TRAY_EDGE_MARGIN    = 1    # inset from icon edge
# Stable uID + GUID let Windows 11 NotifyIconSettings register this app
# independently of other pythonw.exe tray icons. The GUID is the field Win11
# actually keys on; the uID is kept as a legacy fallback.
TRAY_UID  = 0xC1A0_DE77
# Stable GUID. Do NOT change this once users have it registered — Windows 11
# binds (GUID→exe) at first NIM_ADD and refuses fresh GUID registrations
# from the same exe for a while after.
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
    """Saturated RGBA for tray/dock rings — readable at 16 px in the tray
    and more punchy than the muted progress-bar palette used inside the
    overlay rows."""
    if pct >= 90: return (255,  68,  68, 255)   # bright red
    if pct >= 60: return (255, 176,  32, 255)   # bright amber
    return              ( 34, 220,  85, 255)    # bright lime


def _pil_color(hex_str: str) -> tuple:
    """Convert a '#rrggbb' string to an opaque RGBA tuple for Pillow."""
    h = hex_str.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)


def _promote_tray_icon(uid: int, name_hint: str) -> None:
    """Win11 hides new tray icons in the overflow flyout by default. The
    visibility is tracked in HKCU\\Control Panel\\NotifyIconSettings\\<id>
    with an `IsPromoted` DWORD. Setting it to 1 shows the icon next to the
    clock. Silent no-op if the key / app doesn't exist yet or on older
    Windows versions."""
    try:
        import winreg, sys
        exe_cmp = sys.executable.lower()
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                              r"Control Panel\NotifyIconSettings",
                              0, winreg.KEY_READ)
        target = None
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i); i += 1
            except OSError:
                break
            sk = winreg.OpenKey(root, sub)
            vals = {}
            j = 0
            while True:
                try:
                    k, v, _ = winreg.EnumValue(sk, j); j += 1
                    vals[k] = v
                except OSError:
                    break
            winreg.CloseKey(sk)
            exe = vals.get("ExecutablePath", "").lower()
            if exe != exe_cmp:
                continue
            if vals.get("UID") == uid or name_hint in vals.get("InitialTooltip", ""):
                target = sub
                break
        winreg.CloseKey(root)
        if not target:
            return
        sk = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Control Panel\NotifyIconSettings\\" + target,
                            0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(sk, "IsPromoted", 0, winreg.REG_DWORD, 1)
        winreg.CloseKey(sk)
    except Exception:
        pass


def _render_single_ring(size: int, pct: float, color_rgba: tuple,
                        track_rgba: tuple, stroke: int,
                        supersample: int = 4,
                        center_rgba: "tuple | None" = None,
                        edge_margin: int = 3,
                        outline_rgba: "tuple | None" = None,
                        outline_width: int = 1):
    """Render an antialiased progress ring of `size`×`size` via supersampling.

    Pillow's `arc` has no built-in AA — drawing at 4× size and LANCZOS-
    downsampling gives clean curves at the target resolution.

    If `center_rgba` is given, also fills a brand-colored disc inside the
    ring. If `outline_rgba` is given too, a thin contour is drawn along
    both edges of the ring and around the center disc — lifts the icon
    off dark/light taskbars at 16 px."""
    S   = size * supersample
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


def _render_double_ring(size: int, pct_outer: float, pct_inner: float,
                        outer_rgba: tuple, inner_rgba: tuple,
                        accent_rgba: tuple, track_rgba: tuple,
                        outer_stroke: int, inner_stroke: int,
                        ring_gap: int, edge_margin: int,
                        supersample: int = 4):
    """Render two concentric progress rings plus a brand-colored center disc,
    with LANCZOS supersampling for smooth edges."""
    S  = size * supersample
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    em  = edge_margin  * supersample
    os_ = outer_stroke * supersample
    gap = ring_gap     * supersample
    is_ = inner_stroke * supersample

    outer_bbox = (em, em, S - em - 1, S - em - 1)
    d.ellipse(outer_bbox, outline=track_rgba, width=os_)
    if pct_outer > 0:
        span = max(1.0, min(359.9, 3.6 * pct_outer))
        d.arc(outer_bbox, start=-90, end=-90 + span,
              fill=outer_rgba, width=os_)

    i_off = em + os_ + gap
    inner_bbox = (i_off, i_off, S - i_off - 1, S - i_off - 1)
    d.ellipse(inner_bbox, outline=track_rgba, width=is_)
    if pct_inner > 0:
        span = max(1.0, min(359.9, 3.6 * pct_inner))
        d.arc(inner_bbox, start=-90, end=-90 + span,
              fill=inner_rgba, width=is_)

    # Center disc — flush against the inner ring's inside edge (no extra gap)
    # so it's as big as possible and the brand color reads at 16 px.
    c_off = i_off + is_
    d.ellipse((c_off, c_off, S - c_off - 1, S - c_off - 1), fill=accent_rgba)
    return img.resize((size, size), Image.LANCZOS)


def resolve_row(cache: dict, key_pct: str, key_rst: "str | None",
                win_s: int, lang: str, now: "datetime") -> tuple:
    """Compute (pct, rst_txt, credits_tuple_or_None) for a ROWS entry.
    Shared by _refresh_ui, _build_tray_tooltip and _show_hover_card so the
    stale-reset-zeroing and credits formatting live in one place."""
    if key_rst is None:  # Credits row
        used  = cache.get("ex_used",  0)
        limit = cache.get("ex_limit", 0)
        curr  = "€" if cache.get("ex_curr") == "EUR" else cache.get("ex_curr", "")
        rst_txt = f"{used:.2f} / {limit:.2f} {curr}".rstrip()
        return (float(cache.get(key_pct, 0)), rst_txt, (used, limit, curr))
    pct = float(cache.get(key_pct, 0))
    if reset_passed(cache.get(key_rst), now):
        pct = 0.0
    return (pct, fmt_reset(cache.get(key_rst), lang, win_s, now), None)


# ── Main window ───────────────────────────────────────────────────────────────
class JeanClaudeCombien:
    def __init__(self):
        self.cfg              = Settings()
        self.root             = tk.Tk()
        self._body            = None
        self._rows_widgets    = []
        self._known_mtime     = 0.0
        self._last_fetch_time = 0.0
        self._refresh_id      = None
        self._tray_icon       = None
        self._hover_card      = None
        self._hover_hide_id   = None
        self._last_pct_5h     = 0.0
        self._last_pct_wk     = 0.0
        # If tray mode is requested at startup, we defer entering it until the
        # first `_bg_fetch` actually succeeds — otherwise the tray icon bakes
        # in 0 % for a second or two (cache is cold before JCC even gets a
        # chance to hit the API).
        self._tray_wanted     = bool(self.cfg["tray"]) and TRAY_AVAILABLE
        self._build_window()
        self._build_content()
        self._fit_height()
        self._refresh_ui()
        self._schedule_bg_fetch()
        # Fallback: if the first fetch is broken / offline, still enter tray
        # after a reasonable delay so the icon shows up (with whatever the
        # cache has).
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
            pct, rst_txt, _ = resolve_row(cache, key_pct, key_rst, win_s, lang, now)
            if   key_pct == "fh_pct": self._last_pct_5h = pct
            elif key_pct == "wd_pct": self._last_pct_wk = pct
            color = bar_color(pct)
            w     = self._rows_widgets[i]

            if w["mode"] == "dock":
                self.root.after(30 * i,
                                lambda c=w["canvas"], p=pct, col=color, wr=w:
                                self._draw_ring(c, p, col, wr))
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

    def _draw_ring(self, canvas: tk.Canvas, pct: float, color: str,
                   widget_ref: dict):
        """Draw an antialiased ring + centered % text on a dock canvas."""
        canvas.delete("all")
        display_pct = max(0.0, 100.0 - pct) if self.cfg["show_remaining"] else pct
        if TRAY_AVAILABLE:
            img = _render_single_ring(RING_SIZE, display_pct,
                                      ring_color(pct),
                                      _pil_color(C["bar"]),
                                      DOCK_RING_STROKE)
            photo = ImageTk.PhotoImage(img)
            widget_ref["_photo"] = photo
            canvas.create_image(RING_SIZE // 2, RING_SIZE // 2, image=photo)
        else:
            p = 3
            canvas.create_arc(p, p, RING_SIZE - p, RING_SIZE - p,
                              start=90, extent=-359.9,
                              style="arc", width=4, outline=C["bar"])
            if display_pct > 0:
                canvas.create_arc(p, p, RING_SIZE - p, RING_SIZE - p,
                                  start=90,
                                  extent=-min(max(1.0, 3.6 * display_pct), 359.9),
                                  style="arc", width=4, outline=color)
        canvas.create_text(RING_SIZE // 2, RING_SIZE // 2,
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
            try:
                fetch_usage.fetch_and_save()
            except Exception as e:
                err = type(e).__name__
            if err:
                self.root.after(0, lambda: self._upd_var.set(f"⚠ {err}"))
                # Right after Windows sign-in / unlock the network stack may
                # not be fully up yet — retry with short backoff so we don't
                # end up stuck on a cold cache until the 5 min fallback.
                retry_ms = getattr(self, "_fetch_backoff_ms", 5_000)
                if retry_ms <= 60_000:
                    self.root.after(retry_ms, self._bg_fetch)
                    self._fetch_backoff_ms = retry_ms * 3
            else:
                self._fetch_backoff_ms = 5_000   # reset backoff on success
                self.root.after(0, self._refresh_ui)
                # Enter tray only after the first successful fetch — the
                # tray icon then shows real percentages, not a cold-cache 0.
                self.root.after(0, self._enter_tray_if_pending)
        threading.Thread(target=run, daemon=True).start()
        self._upd_var.set("↻ …")

    def _enter_tray_if_pending(self):
        """Honor a pending startup request to enter tray mode (called once
        the first fetch has populated the cache, or as a timeout fallback)."""
        if self._tray_wanted and self._tray_icon is None:
            self._tray_wanted = False   # one-shot
            self._enter_tray()

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
        self._bg_fetch()
        self.root.after(5_000, self._watch_tokens)
        # Fallback: re-fetch every 5 min regardless (browser use, session refresh)
        self.root.after(REFETCH_INTERVAL_MS, self._schedule_bg_fetch)

    # ── Tray mode ─────────────────────────────────────────────────────────────
    def _toggle_tray(self):
        if not TRAY_AVAILABLE:
            return
        if self.cfg["tray"]:
            self._exit_tray()
        else:
            self._enter_tray()

    def _enter_tray(self):
        """Hide the overlay, spawn a system-tray icon with two progress rings.
        On any rendering/pystray failure we abort cleanly back to overlay mode
        — otherwise the window would be withdrawn with no tray to control it."""
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
        # Must set _uid/_guid BEFORE run_detached — pystray's setup thread
        # starts immediately and its first Shell_NotifyIcon(NIM_ADD) reads
        # these via the monkeypatch.
        self._tray_icon._uid  = TRAY_UID
        self._tray_icon._guid = _make_guid(*TRAY_GUID)
        self._last_tray_pcts  = (self._last_pct_5h, self._last_pct_wk)
        self._tray_icon.run_detached()
        self.root.after(1200, lambda: _promote_tray_icon(TRAY_UID,
                                                         "JeanClaudeCombien"))
        # Windows 11 hides new tray icons in the overflow flyout by default —
        # show this once so the user knows where to look (and can pin it).
        if not self.cfg["tray_hinted"]:
            self.cfg["tray_hinted"] = True
            self.root.after(600, self._hint_tray_location)

    def _hint_tray_location(self):
        if self._tray_icon is None:
            return
        try:
            self._tray_icon.notify(
                "Click ^ on the taskbar to expand the overflow. If the icon "
                "isn't there either, open Settings → Personalisation → "
                "Taskbar → Other system tray icons and enable pythonw.exe.",
                "JeanClaudeCombien is in the system tray",
            )
        except Exception:
            pass

    def _quit_from_tray(self):
        self._hide_hover_card()
        try: self.root.destroy()
        except Exception: pass

    def _exit_tray(self):
        """Kill tray icon, close hover-card, bring the overlay back."""
        self.cfg["tray"] = False
        if self._tray_icon is not None:
            try: self._tray_icon.stop()
            except Exception: pass
            self._tray_icon = None
        self._hide_hover_card()
        self.root.deiconify()

    def _update_tray(self, cache: dict, now: "datetime"):
        """Refresh the tray icon bitmap and tooltip. Called from _refresh_ui.
        Skip the full redraw when neither percentage moved — icon/tooltip are
        identical, Shell_NotifyIcon(NIM_MODIFY) is a no-op we can save."""
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
        """Render the tray icon: one bold ring = 5h window, brand-colored
        center disc. Supersampled and LANCZOS-downsampled for smooth edges.
        Week/credits live in the tooltip + hover card, not on the icon."""
        return _render_single_ring(
            TRAY_ICON_SIZE, pct_5h,
            ring_color(pct_5h),
            (110, 70, 40, 90),         # semi-transparent warm track
            TRAY_RING_STROKE,
            center_rgba=(255, 230, 200, 255),   # near-white with Claude tint
            edge_margin=TRAY_EDGE_MARGIN,
            outline_rgba=(60, 30, 10, 220),     # dark warm-brown contour
            outline_width=1,
        )

    def _build_tray_tooltip(self, cache: "dict | None" = None,
                            now: "datetime | None" = None) -> str:
        """Short multi-line text shown as the native Windows tray tooltip.
        Reuse cache + `now` from the refresh tick when available."""
        if cache is None: cache = read_cache()
        if now   is None: now   = datetime.now(tz=timezone.utc)
        lang  = self.cfg["lang"]
        lines = ["JeanClaudeCombien"]
        for key_pct, key_rst, icon, name_key, win_s in ROWS:
            pct, rst_txt, credits = resolve_row(cache, key_pct, key_rst,
                                                win_s, lang, now)
            name = i18n.get(lang, name_key)
            if credits is not None:
                lines.append(f"{icon} {name}: {rst_txt}")
            else:
                lines.append(f"{icon} {name}: {pct:.0f}%   {rst_txt}")
        return "\n".join(lines)[:127]

    def _show_hover_card(self):
        """Compact Toplevel popup shown on tray left-click. Closes on FocusOut
        or after an 8 s fallback timeout."""
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
            pct, rst_txt, credits = resolve_row(cache, key_pct, key_rst,
                                                win_s, lang, now)
            row = tk.Frame(body, bg=C["bg"])
            row.pack(fill="x", pady=1)
            tk.Label(row, text=f"{icon} {i18n.get(lang, name_key)}",
                     bg=C["bg"], fg=C["muted"], font=("Segoe UI", 8),
                     width=12, anchor="w").pack(side="left")
            if credits is not None:
                tk.Label(row, text=rst_txt, bg=C["bg"], fg=C["text"],
                         font=("Segoe UI", 8)).pack(side="left")
            else:
                tk.Label(row, text=f"{pct:.0f}%", bg=C["bg"], fg=pct_color(pct),
                         font=("Segoe UI", 8, "bold"), width=5, anchor="e"
                         ).pack(side="left")
                tk.Label(row, text=rst_txt, bg=C["bg"], fg=C["muted"],
                         font=("Segoe UI", 8)).pack(side="left", padx=(6, 0))

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
