"""
Получает данные с claude.ai через cloudscraper + sessionKey.
Сохраняет в monitor_usage_cache.json.
"""
import json, os, threading
from pathlib import Path
from datetime import datetime, timezone

APPDATA    = Path(os.environ.get("APPDATA", Path.home()))
CLAUDE_DIR = APPDATA / "Claude"
CACHE_FILE = CLAUDE_DIR / "monitor_usage_cache.json"
SESSION    = CLAUDE_DIR / "monitor_session.json"
ORG_CACHE  = CLAUDE_DIR / "monitor_org.json"


# Reusable cloudscraper session — TLS handshake + JS-challenge prep costs
# ~200 ms on first use. Guarded by a lock because startup fires the initial
# fetch on one daemon thread while the watcher can fire again seconds later.
_scraper: "object | None" = None
_scraper_key: "str | None" = None
_scraper_lock = threading.Lock()


def _get_scraper(key: str):
    global _scraper, _scraper_key
    with _scraper_lock:
        if _scraper is None or _scraper_key != key:
            import cloudscraper
            _scraper = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False}
            )
            _scraper.cookies.set("sessionKey", key, domain="claude.ai")
            _scraper_key = key
        return _scraper


def _get_org_id(s) -> str:
    """Определяет org_id через API и кэширует на диск."""
    if ORG_CACHE.exists():
        return json.loads(ORG_CACHE.read_text("utf-8"))["org_id"]
    orgs = s.get("https://claude.ai/api/organizations", timeout=10).json()
    org_id = orgs[0]["uuid"]
    ORG_CACHE.write_text(json.dumps({"org_id": org_id}), encoding="utf-8")
    return org_id


def fetch_and_save() -> dict:
    if not SESSION.exists():
        return {"error": "no_session"}

    key = json.loads(SESSION.read_text("utf-8")).get("sessionKey", "")
    if not key:
        return {"error": "no_session"}

    s      = _get_scraper(key)
    org_id = _get_org_id(s)

    usage = s.get(
        f"https://claude.ai/api/organizations/{org_id}/usage", timeout=15
    ).json()

    fh = usage.get("five_hour")          or {}
    sd = usage.get("seven_day")          or {}
    sn = usage.get("seven_day_sonnet")   or {}
    dz = usage.get("seven_day_omelette") or {}
    eu = usage.get("extra_usage")        or {}

    # used_credits и monthly_limit приходят в центах → делим на 100
    result = {
        "fh_pct":   fh.get("utilization", 0),
        "fh_reset": fh.get("resets_at"),
        "wd_pct":   sd.get("utilization", 0),
        "wd_reset": sd.get("resets_at"),
        "sn_pct":   sn.get("utilization", 0),
        "sn_reset": sn.get("resets_at"),
        "dz_pct":   dz.get("utilization", 0),
        "dz_reset": dz.get("resets_at"),
        "ex_used":  eu.get("used_credits", 0) / 100,
        "ex_limit": eu.get("monthly_limit", 0) / 100,
        "ex_pct":   eu.get("utilization", 0),
        "ex_curr":  eu.get("currency", ""),
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
    }

    CACHE_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    import pprint
    pprint.pprint(fetch_and_save())
