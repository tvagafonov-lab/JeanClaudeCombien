"""
Получает данные с claude.ai через cloudscraper + sessionKey.
Сохраняет в monitor_usage_cache.json.
"""
import json, os
from pathlib import Path
from datetime import datetime, timezone

APPDATA    = Path(os.environ.get("APPDATA", Path.home()))
CLAUDE_DIR = APPDATA / "Claude"
CACHE_FILE = CLAUDE_DIR / "monitor_usage_cache.json"
SESSION    = CLAUDE_DIR / "monitor_session.json"
ORG_CACHE  = CLAUDE_DIR / "monitor_org.json"


def _get_scraper(key: str):
    """Build a *fresh* cloudscraper on every call — no module-level cache.
    Costs ~200 ms of TLS handshake per fetch but avoids cumulative state
    poisoning inside a long-running process (where Cloudflare can blacklist
    a persistent fingerprint and every subsequent call silently fails)."""
    import cloudscraper
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    s.cookies.set("sessionKey", key, domain="claude.ai")
    return s


def _is_expired(r) -> bool:
    """True if the HTTP response indicates sessionKey is no longer valid.
    Covers both raw 401/403 and the JSON error shape Anthropic returns
    through their Cloudflare layer with HTTP 200-looking bodies."""
    if r.status_code in (401, 403):
        return True
    try:
        err = r.json().get("error", {})
        code = (err.get("details") or {}).get("error_code", "")
        return code == "account_session_invalid"
    except (ValueError, AttributeError):
        return False


def _get_org_id(s) -> str | None:
    """Returns the cached org_id, or fetches and caches it. Returns None
    when the sessionKey was rejected (caller surfaces `error: expired`).
    A corrupt cache (partial write, missing key, manual edit) is removed
    rather than allowed to stick forever — re-fetching from the API is
    cheap and self-healing."""
    if ORG_CACHE.exists():
        try:
            return json.loads(ORG_CACHE.read_text("utf-8"))["org_id"]
        except (ValueError, KeyError):
            ORG_CACHE.unlink(missing_ok=True)
    r = s.get("https://claude.ai/api/organizations", timeout=10)
    if _is_expired(r):
        return None
    org_id = r.json()[0]["uuid"]
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
    if org_id is None:
        return {"error": "expired"}

    r = s.get(
        f"https://claude.ai/api/organizations/{org_id}/usage", timeout=15
    )
    if _is_expired(r):
        # org_id cache may be stale too after a re-login flipped orgs.
        ORG_CACHE.unlink(missing_ok=True)
        return {"error": "expired"}
    usage = r.json()

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
