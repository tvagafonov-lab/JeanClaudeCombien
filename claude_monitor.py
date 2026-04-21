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
REFETCH_INTERVAL_MS = 300_000   # session-refresh fallback (browser path updates cookies)


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
        self._build_window()
        self._build_content()
        self._fit_height()
        self._refresh_ui()
        self._schedule_bg_fetch()

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
            try:
                fetch_usage.fetch_and_save()
            except Exception as e:
                err = type(e).__name__
            if err:
                self.root.after(0, lambda: self._upd_var.set(f"⚠ {err}"))
            else:
                self.root.after(0, self._refresh_ui)
        threading.Thread(target=run, daemon=True).start()
        self._upd_var.set("↻ …")

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
