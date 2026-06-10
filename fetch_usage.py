"""
Получает данные с claude.ai через cloudscraper + sessionKey.
Сохраняет в monitor_usage_cache.json.
"""
import json, os, sys, threading
from pathlib import Path
from datetime import datetime, timezone

APPDATA    = Path(os.environ.get("APPDATA", Path.home()))
CLAUDE_DIR = APPDATA / "Claude"
CACHE_FILE = CLAUDE_DIR / "monitor_usage_cache.json"
SESSION    = CLAUDE_DIR / "monitor_session.json"
ORG_CACHE  = CLAUDE_DIR / "monitor_org.json"


_LOG          = CLAUDE_DIR / "monitor_fetch.log"
_LOG_FALLBACK = CLAUDE_DIR / f"monitor_fetch_{os.getpid()}.log"
_LOG_TIMEOUT  = 1.0


def _try_write(target: Path, line: str) -> bool:
    """Synchronous write to a log file with rotation. Returns True on
    success; on any exception returns False (caller falls through to
    the next tier)."""
    try:
        if target.exists() and target.stat().st_size > 64_000:
            tail = target.read_text(encoding="utf-8", errors="ignore")[-32_000:]
            tmp  = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(tail, encoding="utf-8")
            os.replace(tmp, target)
        with target.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
        return True
    except Exception:
        return False


def _log(msg: str) -> None:
    """Append a single line to the rolling fetch log. The file is the
    smoking-gun for diagnosing 'why did setup pop up at 01:42'.

    Three-tier write to survive the incident-4 deadlock pattern:
      1. Shared `monitor_fetch.log` via a worker thread with a 1 s
         timeout. A zombie sibling process can hold a file handle in a
         way that makes the open or write block — by isolating the
         write on a daemon thread we never freeze the caller. The
         daemon thread is left to finish on its own (or never, if the
         lock outlives the process), which is harmless.
      2. Per-PID `monitor_fetch_<pid>.log`. Each interpreter writes its
         own file, so there is no shared handle to contend on. This
         file is the post-mortem source of truth when the shared log
         goes silent. Cleanup of old per-PID files happens at
         startup in claude_monitor.py.
      3. `sys.stderr`, which claude_monitor.py redirects into
         monitor_stderr.log at startup. Last resort, always
         non-blocking.
    """
    ts   = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}  {msg}\n"

    shared_ok = {"v": False}
    def shared_writer():
        shared_ok["v"] = _try_write(_LOG, line)
    t = threading.Thread(target=shared_writer, daemon=True)
    t.start()
    t.join(_LOG_TIMEOUT)
    if shared_ok["v"]:
        return

    if _try_write(_LOG_FALLBACK, line):
        return

    try:
        sys.stderr.write(f"[_log-final-fallback] {line}")
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

    # Null-safety: dict.get(k, default) returns the default ONLY when the
    # key is missing. When Anthropic returns an explicit `null` for a
    # field (observed for `used_credits`/`monthly_limit`/`utilization`
    # in `extra_usage` on 2026-06-05 ~16:47 UTC), `.get(k, 0)` returns
    # None, and any arithmetic like `None / 100` raises TypeError —
    # which froze fetch_and_save in `err: TypeError` for 2.5h before
    # the next reboot. The `or 0` collapses missing+null into 0.
    # used_credits и monthly_limit приходят в центах → делим на 100
    result = {
        "fh_pct":   fh.get("utilization") or 0,
        "fh_reset": fh.get("resets_at"),
        "wd_pct":   sd.get("utilization") or 0,
        "wd_reset": sd.get("resets_at"),
        "sn_pct":   sn.get("utilization") or 0,
        "sn_reset": sn.get("resets_at"),
        "dz_pct":   dz.get("utilization") or 0,
        "dz_reset": dz.get("resets_at"),
        "ex_used":  (eu.get("used_credits")  or 0) / 100,
        "ex_limit": (eu.get("monthly_limit") or 0) / 100,
        "ex_pct":   eu.get("utilization") or 0,
        "ex_curr":  eu.get("currency") or "",
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
    }

    CACHE_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    _log(f"ok: fh={result['fh_pct']} wd={result['wd_pct']} "
         f"sn={result['sn_pct']} dz={result['dz_pct']}")
    return result


if __name__ == "__main__":
    import pprint
    pprint.pprint(fetch_and_save())
