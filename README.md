# Claude Monitor

Compact always-on-top overlay for Windows that shows your real-time **Claude usage stats** — 5-hour window, weekly limit, Sonnet usage, Claude Design, and extra credits — directly on your desktop.

No browser tab juggling. No digging through settings.

![Claude Monitor screenshot — full and compact mode](docs/screenshot.png)

---

## What it shows

| Metric | Description |
|---|---|
| ⏱ **5ч окно** | 5-hour rolling window usage |
| 📅 **Неделя** | 7-day total usage |
| ✨ **Sonnet** | Claude 3.5 Sonnet usage |
| 🎨 **Дизайн** | Claude Design (Artifacts) usage |
| 💳 **Кредиты** | Extra credits used / monthly limit |

Color-coded progress bars: green → yellow → red as limits approach.

**Two modes:** full (with progress bars) and compact (icon + % + time to reset). Double-click the header to switch.

---

## Requirements

- **Windows 10 / 11**
- **Python 3.10+** — [python.org/downloads](https://python.org/downloads/) *(check "Add Python to PATH" during install)*
- **Claude Pro or Max account** on [claude.ai](https://claude.ai)
- Claude Desktop app *(optional — the overlay works standalone)*

---

## Installation

### 1. Download

Click **Code → Download ZIP**, extract anywhere (e.g. `C:\claude_monitor\`).

Or clone:
```
git clone https://github.com/YOUR_USERNAME/claude-monitor.git
cd claude-monitor
```

### 2. Run the installer

Double-click **`install.bat`**

It will:
- Check Python version
- Install dependencies (`cloudscraper`, `requests`)
- Create a Windows **autostart shortcut** (runs on login)
- Open the setup window

### 3. Enter your sessionKey

The setup window explains exactly where to find it:

1. Open **Chrome** and log in to [claude.ai](https://claude.ai)
2. Press **F12** → **Application** tab
3. **Storage → Cookies → https://claude.ai**
4. Find the row **`sessionKey`**
5. Click it → select all in the Value field → copy
6. Paste into the setup window → **Save**

The key looks like: `sk-ant-sid02-XXXX...` (~131 characters).

> **One-time step.** The key is stored locally in `%APPDATA%\Claude\monitor_session.json` and never leaves your machine.

---

## Usage

After setup, the overlay starts automatically with Windows.

To start it manually: double-click **`start_monitor.bat`**

**Controls:**
| Action | Result |
|---|---|
| Drag | Move the window anywhere |
| Double-click header | Toggle compact / full mode |
| Right-click | Context menu (refresh interval, opacity, refresh now) |
| ↺ button | Force refresh immediately |
| ✕ button | Close |

Settings (position, opacity, interval, mode) are saved automatically to `%APPDATA%\Claude\monitor_settings.json`.

---

## Updating the sessionKey

Sessions expire after a few weeks. When usage data stops updating:

1. Re-run **`setup.py`** (or `python setup.py`)
2. Get a fresh key from Chrome DevTools (same steps as above)
3. Paste and save

---

## How it works

- **Token count** (`tokens-today`) is read from Claude Desktop's local file:  
  `%APPDATA%\Claude\buddy-tokens.json`

- **Usage percentages** are fetched from the claude.ai private API:  
  `https://claude.ai/api/organizations/{orgId}/usage`  
  using your `sessionKey` cookie via [cloudscraper](https://github.com/VeNoMouS/cloudscraper) (handles Cloudflare JS challenge transparently).

- Your `orgId` is auto-detected on first run and cached locally.

- Data is refreshed every **5 minutes** by default (configurable: 1 / 5 / 10 / 30 min).

---

## Files

```
claude_monitor/
├── claude_monitor.py   # Main overlay (tkinter)
├── fetch_usage.py      # Fetches usage from claude.ai API
├── setup.py            # First-time sessionKey setup GUI
├── requirements.txt    # Python dependencies
├── install.bat         # One-click installer
└── start_monitor.bat   # Launch overlay manually
```

User data (not in repo, stored in `%APPDATA%\Claude\`):
- `monitor_session.json` — your sessionKey
- `monitor_org.json` — cached org ID
- `monitor_usage_cache.json` — last fetched usage data
- `monitor_settings.json` — position, opacity, mode

---

## Troubleshooting

**"Ошибка подключения" in setup**  
→ Make sure you're logged into claude.ai in Chrome and the sessionKey is copied correctly (all ~131 chars).

**Usage not updating after reboot**  
→ Your sessionKey expired. Re-run `setup.py` with a fresh key.

**HTTP 401 / 403 errors**  
→ Session expired. Re-run `setup.py`.

**Window not visible**  
→ It may be off-screen. Delete `%APPDATA%\Claude\monitor_settings.json` and restart — it will reappear in the bottom-right corner.

**"Python not found" in install.bat**  
→ Reinstall Python from [python.org](https://python.org/downloads/) and check **"Add Python to PATH"**.

---

## Privacy

All data stays on your machine. The only outbound request is to `claude.ai` using your own session cookie — the same request your browser makes when you visit the usage page. No telemetry, no third-party servers.

---

## License

MIT
