"""
Получает данные с claude.ai через cloudscraper + sessionKey.
Сохраняет в monitor_usage_cache.json.
"""
import json, os, sys
from pathlib import Path
from datetime import datetime, timezone

APPDATA    = Path(os.environ.get("APPDATA", Path.home()))
CLAUDE_DIR = APPDATA / "Claude"
CACHE_FILE = CLAUDE_DIR / "monitor_usage_cache.json"
SESSION    = CLAUDE_DIR / "monitor_session.json"
ORG_CACHE  = CLAUDE_DIR / "monitor_org.json"


_LOG = CLAUDE_DIR / "monitor_fetch.log"


def _log(msg: str) -> None:
    """Append a single line to the rolling fetch log. The file is the
    smoking-gun for diagnosing 'why did setup pop up at 01:42'.

    Two robustness fixes against past zombie incidents:
    * Rotation uses .tmp + os.replace so a concurrent reader can never
      see a half-written file (atomic on Windows for same-directory).
    * On any open/write failure we fall through to sys.stderr (which
      claude_monitor.py's startup block redirects into monitor_stderr.log).
      The 2026-05-27 incident left zero trail in monitor_fetch.log
      because something — most likely a zombie handle — blocked our
      'a'-mode opens. Stderr is a separate descriptor and survives.
    """
    ts   = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}  {msg}\n"
    try:
        # Cap at 64 KB so the log never bloats indefinitely.
        if _LOG.exists() and _LOG.stat().st_size > 64_000:
            tail = _LOG.read_text(encoding="utf-8", errors="ignore")[-32_000:]
            tmp  = _LOG.with_suffix(_LOG.suffix + ".tmp")
            tmp.write_text(tail, encoding="utf-8")
            os.replace(tmp, _LOG)
        with _LOG.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
        return
    except Exception as e:
        primary_err = type(e).__name__
    # Fallback path — keep visibility even when the primary log is dead.
    try:
        sys.stderr.write(f"[_log-fallback {primary_err}] {line}")
        sys.stderr.flush()
    except Exception:
        pass


def _get_scraper(key: str):
    """Build a fresh cloudscraper, then warm it up with a no-auth GET to
    claude.ai/. Cloudflare hands out a fresh challenge token on that
    first hit; reusing it on the API call immediately after sidesteps
    the 'cold scraper gets challenged on auth endpoint' failure mode
    that can return a 403 even with a valid sessionKey."""
    import cloudscraper
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    s.cookies.set("sessionKey", key, domain="claude.ai")
    try:
        s.get("https://claude.ai/", timeout=10)
    except Exception:
        pass   # warm-up is best-effort; the real call will surface any error
    return s


def _is_expired(r) -> bool:
    """True if the HTTP response indicates sessionKey is no longer valid.

    HTTP 401 is unambiguously an auth failure. 403 alone is NOT — Cloudflare
    serves 403 for bot-challenge pages too, and a fresh cloudscraper session
    occasionally hits one right after process restart. We only call 403
    'expired' when the body confirms it via Anthropic's structured error
    code; otherwise we treat it as transient and let backoff retry pick a
    new TLS fingerprint."""
    try:
        body = r.json()
    except ValueError:
        body = None
    code = ""
    if isinstance(body, dict):
        code = (body.get("error", {}).get("details") or {}).get("error_code", "")
    if code == "account_session_invalid":
        return True
    return r.status_code == 401


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
        _log("no_session: session file missing")
        return {"error": "no_session"}

    key = json.loads(SESSION.read_text("utf-8")).get("sessionKey", "")
    if not key:
        _log("no_session: empty sessionKey field")
        return {"error": "no_session"}

    s      = _get_scraper(key)
    org_id = _get_org_id(s)
    if org_id is None:
        _log("expired: orgs endpoint reported invalid session")
        return {"error": "expired"}

    r = s.get(
        f"https://claude.ai/api/organizations/{org_id}/usage", timeout=15
    )
    if _is_expired(r):
        ORG_CACHE.unlink(missing_ok=True)
        _log(f"expired: usage endpoint reported invalid session "
             f"(status={r.status_code} body={r.text[:120]!r})")
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
    _log(f"ok: fh={result['fh_pct']} wd={result['wd_pct']} "
         f"sn={result['sn_pct']} dz={result['dz_pct']}")
    return result


if __name__ == "__main__":
    import pprint
    pprint.pprint(fetch_and_save())
