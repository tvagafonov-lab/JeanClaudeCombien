#!/usr/bin/env python3
"""
JeanClaudeCombien — Claude usage overlay for Windows
Always-on-top widget. Double-click header to toggle compact mode.
Right-click for settings (opacity, language, used/remaining toggle).
Updates automatically when buddy-tokens.json changes (file watcher).
"""

import tkinter as tk
import ctypes
import json, os, time, subprocess, sys, threading
from pathlib import Path
from datetime import datetime, timezone
import i18n

# ── Paths ─────────────────────────────────────────────────────────────────────
APPDATA       = Path(os.environ.get("APPDATA", Path.home()))
CLAUDE_DIR    = APPDATA / "Claude"
TOKENS_FILE   = CLAUDE_DIR / "buddy-tokens.json"
CACHE_FILE    = CLAUDE_DIR / "monitor_usage_cache.json"
SETTINGS_FILE = CLAUDE_DIR / "monitor_settings.json"
FETCH_SCRIPT  = Path(__file__).parent / "fetch_usage.py"

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

# ── Colors ────────────────────────────────────────────────────────────────────
C = {
    "bg":     "#0d0d1a",
    "bg2":    "#16162a",
    "hdr":    "#1e1e36",
    "accent": "#7b61ff",
    "text":   "#e2e2e2",
    "muted":  "#5a5a7a",
    "green":  "#4ade80",
    "yellow": "#fbbf24",
    "red":    "#f87171",
    "bar":    "#252540",
}

W_FULL    = 265
W_COMPACT = 165

RING_SIZE = 36   # ring canvas size (px) in dock mode
RING_PAD  = 3    # padding around each ring canvas
DOCK_H    = RING_SIZE + RING_PAD * 2 + 2   # = 44 px (matches Win11 taskbar)

# Row definitions: (pct_key, reset_key, icon, i18n_key)
ROWS = [
    ("fh_pct", "fh_reset", "⏱", "row_5h"),
    ("wd_pct", "wd_reset", "📅", "row_week"),
    ("sn_pct", "sn_reset", "✨", "row_sonnet"),
    ("dz_pct", "dz_reset", "🎨", "row_design"),
    ("ex_pct", None,       "💳", "row_credits"),
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
def read_cache() -> dict:
    try:
        if CACHE_FILE.exists():
            return json.loads(CACHE_FILE.read_text("utf-8"))
    except Exception:
        pass
    return {}


def fmt_reset(iso: str | None, lang: str) -> str:
    tr = i18n.STRINGS.get(lang, i18n.STRINGS["en"])
    if not iso:
        return "—"
    try:
        dt   = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        diff = dt.astimezone(timezone.utc) - datetime.now(tz=timezone.utc)
        if diff.total_seconds() < 0:
            return tr["reset_done"]
        mins = int(diff.total_seconds() // 60)
        h, m = divmod(mins, 60)
        if diff.total_seconds() < 86400:
            return f"{h}h {m:02}m" if h else f"{m}m"
        local = dt.astimezone()
        return f"{tr['days'][local.weekday()]} {local.strftime('%H:%M')}"
    except Exception:
        return "—"


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

        self._title_var.set("◆ JCC" if compact else "◆ JeanClaudeCombien")

        self._body = tk.Frame(self.root, bg=C["bg"],
                              padx=6 if compact else 10)
        self._body.pack(fill="x", pady=(4, 5))

        self._rows_widgets = []
        for key_pct, key_rst, icon, name_key in ROWS:
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

        for i, (key_pct, key_rst, icon, name_key) in enumerate(ROWS):
            pct   = float(cache.get(key_pct, 0))
            color = bar_color(pct)
            w     = self._rows_widgets[i]

            if key_rst is None:  # Credits row
                used    = cache.get("ex_used",  0)
                limit   = cache.get("ex_limit", 0)
                curr    = "€" if cache.get("ex_curr") == "EUR" else cache.get("ex_curr", "")
                rst_txt = f"{used:.2f} / {limit:.2f} {curr}"
            else:
                rst_txt = fmt_reset(cache.get(key_rst), lang)

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
            except Exception:
                pass

        if self._refresh_id is not None:
            self.root.after_cancel(self._refresh_id)
        self._refresh_id = self.root.after(10_000, self._refresh_ui)

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
        """Y: just above taskbar (via SPI_GETWORKAREA). X: saved or default."""
        try:
            from ctypes import wintypes
            wa = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(wa), 0)
            y = wa.bottom - h
        except Exception:
            y = self.root.winfo_screenheight() - h - 48  # 48 px fallback
        x = self.cfg["dock_x"] if self.cfg["dock_x"] >= 0 else 80
        return x, y

    def _build_dock(self):
        """Build the dock strip: one ring canvas per row, no header."""
        self._body = tk.Frame(self.root, bg=C["bg"])
        self._body.pack(fill="both", expand=True)
        self._rows_widgets = []
        for _key_pct, _key_rst, _icon, _name_key in ROWS:
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
                              extent=-max(1.0, 3.6 * min(display_pct, 100)),
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
            try:
                subprocess.run([sys.executable, str(FETCH_SCRIPT)],
                               timeout=40, capture_output=True)
                self.root.after(500, self._refresh_ui)
            except Exception:
                pass
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
        self.root.after(300_000, self._schedule_bg_fetch)

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

        # Opacity submenu
        sub2 = tk.Menu(m, tearoff=0, bg=C["bg2"], fg=C["text"],
                       activebackground=C["accent"], font=("Segoe UI", 9))
        for a in (1.0, 0.92, 0.80, 0.60):
            mark = "✓  " if abs(self.cfg["opacity"] - a) < 0.01 else "    "
            sub2.add_command(label=f"{mark}{int(a * 100)}%",
                             command=lambda a=a: self._set_opacity(a))
        m.add_cascade(label=self._t("menu_opacity"), menu=sub2)

        # Language submenu
        sub3 = tk.Menu(m, tearoff=0, bg=C["bg2"], fg=C["text"],
                       activebackground=C["accent"], font=("Segoe UI", 9))
        for code, label in i18n.LANGUAGES.items():
            mark = "✓  " if lang == code else "    "
            sub3.add_command(label=f"{mark}{label}",
                             command=lambda c=code: self._set_lang(c))
        m.add_cascade(label=self._t("menu_language"), menu=sub3)

        m.add_separator()
        m.add_command(label=self._t("menu_close"), command=self.root.destroy)
        m.post(e.x_root, e.y_root)

    def _set_opacity(self, v):
        self.cfg["opacity"] = v
        self.root.attributes("-alpha", v)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    JeanClaudeCombien().run()
