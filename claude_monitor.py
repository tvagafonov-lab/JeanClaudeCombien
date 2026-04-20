#!/usr/bin/env python3
"""
Claude Token Monitor
Полный и компактный режим. Двойной клик на заголовке — переключение.
"""

import tkinter as tk
import json, os, subprocess, sys, threading
from pathlib import Path
from datetime import datetime, timezone

# ── Пути ─────────────────────────────────────────────────────────────────────
APPDATA       = Path(os.environ.get("APPDATA", Path.home()))
CLAUDE_DIR    = APPDATA / "Claude"
TOKENS_FILE   = CLAUDE_DIR / "buddy-tokens.json"
CACHE_FILE    = CLAUDE_DIR / "monitor_usage_cache.json"
SETTINGS_FILE = CLAUDE_DIR / "monitor_settings.json"
FETCH_SCRIPT  = Path(__file__).parent / "fetch_usage.py"

DEFAULT_SETTINGS = {
    "opacity":  0.92,
    "interval": 300,
    "compact":  False,
    "pos_x": -1,
    "pos_y": -1,
}

# ── Цвета ─────────────────────────────────────────────────────────────────────
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

W_FULL    = 255
W_COMPACT = 165

# Строки: (ключ_pct, ключ_reset, иконка, название)
ROWS = [
    ("fh_pct", "fh_reset", "⏱", "5ч окно"),
    ("wd_pct", "wd_reset", "📅", "Неделя"),
    ("sn_pct", "sn_reset", "✨", "Sonnet"),
    ("dz_pct", "dz_reset", "🎨", "Дизайн"),
    ("ex_pct", None,       "💳", "Кредиты"),
]


# ── Утилиты ───────────────────────────────────────────────────────────────────
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


def read_tokens() -> int:
    try:
        d = json.loads(TOKENS_FILE.read_text("utf-8"))
        return d.get("tokens-today", {}).get("tokens", 0)
    except Exception:
        return 0


def read_cache() -> dict:
    try:
        if CACHE_FILE.exists():
            return json.loads(CACHE_FILE.read_text("utf-8"))
    except Exception:
        pass
    return {}


def fmt_reset(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt   = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        diff = dt.astimezone(timezone.utc) - datetime.now(tz=timezone.utc)
        if diff.total_seconds() < 0:
            return "↺ сброс"
        mins = int(diff.total_seconds() // 60)
        h, m = divmod(mins, 60)
        if diff.total_seconds() < 86400:
            return f"{h}ч {m:02}м" if h else f"{m}м"
        local = dt.astimezone()
        days  = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        return f"{days[local.weekday()]} {local.strftime('%H:%M')}"
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


# ── Основное окно ─────────────────────────────────────────────────────────────
class ClaudeMonitor:
    def __init__(self):
        self.cfg   = Settings()
        self.root  = tk.Tk()
        self._body = None          # будет пересоздаваться при смене режима
        self._rows_widgets = []
        self._build_window()
        self._build_content()
        self._fit_height()
        self._refresh_ui()
        self._schedule_bg_fetch()

    # ── Окно (создаётся один раз) ─────────────────────────────────────────────
    def _build_window(self):
        r = self.root
        r.title("Claude Monitor")
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

        # ── Шапка (один раз) ──────────────────────────────────────────────────
        hdr = tk.Frame(r, bg=C["hdr"], height=24)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        self._plan_var = tk.StringVar(value="◆ Claude Monitor")
        hdr_lbl = tk.Label(hdr, textvariable=self._plan_var,
                           bg=C["hdr"], fg=C["accent"],
                           font=("Segoe UI", 8, "bold"), cursor="hand2")
        hdr_lbl.pack(side="left", padx=7)
        hdr_lbl.bind("<Double-Button-1>", lambda _: self._toggle_compact())

        x_lbl = tk.Label(hdr, text="✕", bg=C["hdr"], fg=C["muted"],
                         font=("Segoe UI", 10), cursor="hand2")
        x_lbl.pack(side="right", padx=5)
        x_lbl.bind("<Button-1>", lambda _: r.destroy())
        x_lbl.bind("<Enter>",    lambda _: x_lbl.config(fg=C["red"]))
        x_lbl.bind("<Leave>",    lambda _: x_lbl.config(fg=C["muted"]))

        btn = tk.Label(hdr, text="↺", bg=C["hdr"], fg=C["muted"],
                       font=("Segoe UI", 11), cursor="hand2")
        btn.pack(side="right", padx=1)
        btn.bind("<Button-1>", lambda _: self._bg_fetch())
        btn.bind("<Enter>",    lambda _: btn.config(fg=C["accent"]))
        btn.bind("<Leave>",    lambda _: btn.config(fg=C["muted"]))

        self._upd_var = tk.StringVar(value="")
        tk.Label(hdr, textvariable=self._upd_var,
                 bg=C["hdr"], fg=C["muted"],
                 font=("Segoe UI", 7)).pack(side="right", padx=3)

    # ── Контент (пересоздаётся при смене режима) ──────────────────────────────
    def _build_content(self):
        if self._body:
            self._body.destroy()

        compact = self.cfg["compact"]
        W = W_COMPACT if compact else W_FULL

        self._body = tk.Frame(self.root, bg=C["bg"],
                              padx=6 if compact else 10)
        self._body.pack(fill="x", pady=(4, 5))

        self._rows_widgets = []
        for key_pct, key_rst, icon, name in ROWS:
            if compact:
                w = self._make_compact_row(self._body, icon)
            else:
                w = self._make_full_row(self._body, icon, name)
            self._rows_widgets.append(w)

        if not compact:
            tk.Frame(self._body, bg=C["bar"], height=1).pack(fill="x", pady=(4, 2))
            self._tok_var = tk.StringVar(value="—")
            tk.Label(self._body, textvariable=self._tok_var,
                     bg=C["bg"], fg=C["muted"],
                     font=("Segoe UI", 7)).pack(anchor="w")
        else:
            self._tok_var = None

        # Resize window width
        x, y = self.root.winfo_x(), self.root.winfo_y()
        self.root.geometry(f"{W}x1+{x}+{y}")

    def _make_full_row(self, parent, icon: str, name: str) -> dict:
        """Полный режим: иконка+название | бар | % | сброс"""
        f = tk.Frame(parent, bg=C["bg"])
        f.pack(fill="x", pady=1)

        tk.Label(f, text=f"{icon} {name}", bg=C["bg"], fg=C["muted"],
                 font=("Segoe UI", 7), width=10, anchor="w").pack(side="left")

        canvas = tk.Canvas(f, height=5, bg=C["bar"],
                           highlightthickness=0, bd=0, width=72)
        canvas.pack(side="left", padx=(2, 3))

        pct_var = tk.StringVar(value="—")
        tk.Label(f, textvariable=pct_var, bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 7), width=4, anchor="e").pack(side="left")

        rst_var = tk.StringVar(value="")
        tk.Label(f, textvariable=rst_var, bg=C["bg"], fg=C["muted"],
                 font=("Segoe UI", 7)).pack(side="left", padx=(3, 0))

        return {"mode": "full", "canvas": canvas,
                "pct_var": pct_var, "rst_var": rst_var}

    def _make_compact_row(self, parent, icon: str) -> dict:
        """Компактный режим: иконка  %  сброс"""
        f = tk.Frame(parent, bg=C["bg"])
        f.pack(fill="x", pady=1)

        tk.Label(f, text=icon, bg=C["bg"], fg=C["muted"],
                 font=("Segoe UI", 8), width=2).pack(side="left")

        pct_var = tk.StringVar(value="—")
        tk.Label(f, textvariable=pct_var, bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 8, "bold"), width=5, anchor="e").pack(side="left")

        rst_var = tk.StringVar(value="")
        tk.Label(f, textvariable=rst_var, bg=C["bg"], fg=C["muted"],
                 font=("Segoe UI", 7)).pack(side="left", padx=(5, 0))

        return {"mode": "compact", "pct_var": pct_var, "rst_var": rst_var}

    def _draw_bar(self, canvas: tk.Canvas, pct: float, color: str):
        canvas.update_idletasks()
        w = canvas.winfo_width() or 72
        canvas.delete("all")
        canvas.create_rectangle(0, 0, w, 5, fill=C["bar"], outline="")
        fw = int(w * min(pct, 100) / 100)
        if fw > 0:
            canvas.create_rectangle(0, 0, fw, 5, fill=color, outline="")

    def _fit_height(self):
        """Подгоняет высоту окна под реальный контент."""
        self.root.update_idletasks()
        h = self.root.winfo_reqheight()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        W = W_COMPACT if self.cfg["compact"] else W_FULL
        self.root.geometry(f"{W}x{h}+{x}+{y}")

    # ── Обновление данных ─────────────────────────────────────────────────────
    def _refresh_ui(self):
        cache  = read_cache()
        tokens = read_tokens()

        self._plan_var.set("◆ Claude")

        for i, (key_pct, key_rst, icon, name) in enumerate(ROWS):
            pct   = float(cache.get(key_pct, 0))
            color = bar_color(pct)
            w     = self._rows_widgets[i]

            if key_rst is None:  # кредиты (значения уже в евро с центами)
                used  = cache.get("ex_used",  0)
                limit = cache.get("ex_limit", 0)
                curr  = "€" if cache.get("ex_curr") == "EUR" else cache.get("ex_curr", "")
                rst_txt = f"{used:.2f} / {limit:.2f} {curr}"
            else:
                rst_txt = fmt_reset(cache.get(key_rst))

            w["pct_var"].set(f"{pct:.0f}%")
            w["pct_var"]  # цвет через configure
            w["rst_var"].set(rst_txt)

            # Цвет процента
            try:
                w["pct_var"].__label__.config(fg=pct_color(pct))
            except Exception:
                pass

            if w["mode"] == "full":
                self.root.after(30 * i, lambda c=w["canvas"], p=pct, col=color:
                                self._draw_bar(c, p, col))

        if self._tok_var:
            self._tok_var.set(f"🔢  {tokens:,} токенов сегодня")

        if cache.get("fetched_at"):
            try:
                dt  = datetime.fromisoformat(cache["fetched_at"])
                self._upd_var.set(f"⟳ {dt.astimezone().strftime('%H:%M')}")
            except Exception:
                pass

        self.root.after(10_000, self._refresh_ui)

    # ── Компактный режим ──────────────────────────────────────────────────────
    def _toggle_compact(self):
        self.cfg["compact"] = not self.cfg["compact"]
        self._build_content()
        self.root.after(50, self._fit_height)
        self._refresh_ui()

    # ── Фоновое обновление ────────────────────────────────────────────────────
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

    def _schedule_bg_fetch(self):
        self._bg_fetch()
        ms = max(self.cfg["interval"] * 1000, 60_000)
        self.root.after(ms, self._schedule_bg_fetch)

    # ── Drag ──────────────────────────────────────────────────────────────────
    def _drag_start(self, e): self._ox, self._oy = e.x, e.y
    def _drag_move(self, e):
        x = self.root.winfo_x() + e.x - self._ox
        y = self.root.winfo_y() + e.y - self._oy
        self.root.geometry(f"+{x}+{y}")
    def _drag_end(self, e):
        self.cfg["pos_x"] = self.root.winfo_x()
        self.cfg["pos_y"] = self.root.winfo_y()

    # ── Контекстное меню ──────────────────────────────────────────────────────
    def _ctx_menu(self, e):
        m = tk.Menu(self.root, tearoff=0, bg=C["bg2"], fg=C["text"],
                    activebackground=C["accent"], font=("Segoe UI", 9), bd=0)

        mode_lbl = "→ Полный режим" if self.cfg["compact"] else "→ Компактный режим"
        m.add_command(label=mode_lbl, command=self._toggle_compact)
        m.add_command(label="↺  Обновить сейчас", command=self._bg_fetch)
        m.add_separator()

        sub = tk.Menu(m, tearoff=0, bg=C["bg2"], fg=C["text"],
                      activebackground=C["accent"], font=("Segoe UI", 9))
        for v, lbl in [(60, "1 мин"), (300, "5 мин"), (600, "10 мин"), (1800, "30 мин")]:
            mark = "✓  " if self.cfg["interval"] == v else "    "
            sub.add_command(label=f"{mark}{lbl}",
                            command=lambda v=v: self._set_interval(v))
        m.add_cascade(label="⏰  Интервал", menu=sub)

        sub2 = tk.Menu(m, tearoff=0, bg=C["bg2"], fg=C["text"],
                       activebackground=C["accent"], font=("Segoe UI", 9))
        for a in (1.0, 0.92, 0.80, 0.60):
            mark = "✓  " if abs(self.cfg["opacity"] - a) < 0.01 else "    "
            sub2.add_command(label=f"{mark}{int(a*100)}%",
                             command=lambda a=a: self._set_opacity(a))
        m.add_cascade(label="👁  Прозрачность", menu=sub2)

        m.add_separator()
        m.add_command(label="✕  Закрыть", command=self.root.destroy)
        m.post(e.x_root, e.y_root)

    def _set_interval(self, v):
        self.cfg["interval"] = v

    def _set_opacity(self, v):
        self.cfg["opacity"] = v
        self.root.attributes("-alpha", v)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    ClaudeMonitor().run()
