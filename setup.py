"""
Первоначальная настройка Claude Monitor.
Запускать один раз: python setup.py
"""
import json, os, webbrowser
import tkinter as tk
from tkinter import messagebox
from pathlib import Path

APPDATA    = Path(os.environ.get("APPDATA", Path.home()))
CLAUDE_DIR = APPDATA / "Claude"
SESSION    = CLAUDE_DIR / "monitor_session.json"
ORG_CACHE  = CLAUDE_DIR / "monitor_org.json"

INSTRUCTIONS = """Как получить sessionKey:

1. Открой Chrome и войди на claude.ai
2. Нажми F12 (DevTools)
3. Вкладка Application
4. Слева: Storage → Cookies → https://claude.ai
5. Найди строку sessionKey
6. Кликни на неё → в поле Value внизу
   выдели всё (Ctrl+A) и скопируй (Ctrl+C)
7. Вставь в поле ниже и нажми Сохранить

Ключ выглядит так:
sk-ant-sid02-XXXXXXXXXXXXXXXXXXXX...
(около 130 символов)
"""


def run_gui():
    root = tk.Tk()
    root.title("JeanClaudeCombien — Setup")
    root.geometry("480x540")
    root.minsize(480, 540)
    root.configure(bg="#0d0d1a")

    fg_accent = "#7b61ff"
    fg_text   = "#e2e2e2"
    fg_muted  = "#888"
    bg        = "#0d0d1a"
    bg2       = "#1e1e36"

    tk.Label(root, text="◆ JeanClaudeCombien — Setup",
             bg=bg, fg=fg_accent,
             font=("Segoe UI", 14, "bold")).pack(pady=(20, 4))

    tk.Label(root, text=INSTRUCTIONS,
             bg=bg, fg=fg_muted,
             font=("Segoe UI", 9), justify="left").pack(padx=20)

    # Кнопка открыть chrome
    def open_chrome():
        webbrowser.open("https://claude.ai")
    tk.Button(root, text="Открыть claude.ai в браузере",
              command=open_chrome,
              bg=bg2, fg=fg_text,
              relief="flat", padx=10, pady=4,
              font=("Segoe UI", 9),
              cursor="hand2").pack(pady=(4, 0))

    # Поле ввода
    tk.Label(root, text="Вставь sessionKey:",
             bg=bg, fg=fg_text,
             font=("Segoe UI", 9)).pack(anchor="w", padx=20, pady=(12, 2))

    entry = tk.Entry(root, width=60, show="•",
                     bg=bg2, fg=fg_text,
                     insertbackground=fg_text,
                     relief="flat", font=("Consolas", 9))
    entry.pack(padx=20, ipady=5)

    # Показать/скрыть
    show_var = tk.BooleanVar(value=False)
    def toggle_show():
        entry.config(show="" if show_var.get() else "•")
    tk.Checkbutton(root, text="Показать ключ",
                   variable=show_var, command=toggle_show,
                   bg=bg, fg=fg_muted,
                   selectcolor=bg2,
                   activebackground=bg,
                   font=("Segoe UI", 8)).pack(anchor="w", padx=20)

    status_var = tk.StringVar(value="")
    status_lbl = tk.Label(root, textvariable=status_var,
                          bg=bg, fg="#4ade80",
                          font=("Segoe UI", 8))
    status_lbl.pack()

    def save():
        key = entry.get().strip()
        if not key.startswith("sk-ant"):
            status_var.set("⚠ Ключ должен начинаться с sk-ant...")
            status_lbl.config(fg="#f87171")
            return
        if len(key) < 50:
            status_var.set("⚠ Слишком короткий ключ")
            status_lbl.config(fg="#f87171")
            return

        SESSION.write_text(json.dumps({"sessionKey": key}), encoding="utf-8")

        # Удаляем кэш org_id — будет получен заново
        if ORG_CACHE.exists():
            ORG_CACHE.unlink()

        status_var.set("Проверяю подключение...")
        status_lbl.config(fg=fg_muted)
        root.update()

        # Тестовый запрос — импортим напрямую, без subprocess
        try:
            import importlib, fetch_usage as _fu
            importlib.reload(_fu)   # подхватить только что записанный SESSION
            res = _fu.fetch_and_save()
        except Exception as e:
            status_var.set(f"⚠ {type(e).__name__}: проверь подключение")
            status_lbl.config(fg="#f87171")
            return
        if isinstance(res, dict) and res.get("error"):
            status_var.set("⚠ Ключ отклонён. Проверь, что скопировал целиком.")
            status_lbl.config(fg="#f87171")
        else:
            status_var.set("✓ Готово! Можно закрыть это окно.")
            status_lbl.config(fg="#4ade80")

    tk.Button(root, text="  Сохранить и проверить  ",
              command=save,
              bg=fg_accent, fg="white",
              relief="flat", padx=16, pady=6,
              font=("Segoe UI", 10, "bold"),
              cursor="hand2").pack(pady=12)

    root.mainloop()


if __name__ == "__main__":
    run_gui()
