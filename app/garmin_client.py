import os
import base64
import hashlib
import json
import math
import time
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Lock
from typing import List, Tuple
from urllib.parse import urlsplit

from fastapi import HTTPException
from garminconnect import Garmin, GarminConnectTooManyRequestsError

from .models import Activity, WellnessDay

# Tokenstore directory on Render. Use a persistent disk path if one is mounted;
# otherwise env-provided tokens repopulate this directory after each restart.
TOKENSTORE_DIR = os.getenv("GARMIN_TOKENSTORE_DIR", "/tmp/.garth")
OAUTH1_PATH = os.path.join(TOKENSTORE_DIR, "oauth1_token.json")
OAUTH2_PATH = os.path.join(TOKENSTORE_DIR, "oauth2_token.json")

_GARMIN_CLIENT: Garmin | None = None
_GARMIN_CLIENT_LOCK = Lock()
_TOKENSTORE_LOCK = Lock()
_PASSWORD_LOGIN_ATTEMPTED = False
_AUTH_FAILURE: dict = {}

# This is a local quiet period, not a guarantee of Garmin's reset time.
_RATE_LIMIT_QUIET_SECONDS = 30 * 60

_OAUTH1_ENV_NAMES = ("OAUTH1_B64", "GARMIN_OAUTH1_B64", "GARTH_OAUTH1_B64")
_OAUTH2_ENV_NAMES = ("OAUTH2_B64", "GARMIN_OAUTH2_B64", "GARTH_OAUTH2_B64")


def _get_first_env(names: Tuple[str, ...]) -> str | None:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return None


def _write_tokens_from_env_to_disk() -> bool:
    """
    Decode OAuth token env vars and write them into TOKENSTORE_DIR.
    Returns True when both token files were restored.
    """
    b1 = _get_first_env(_OAUTH1_ENV_NAMES)
    b2 = _get_first_env(_OAUTH2_ENV_NAMES)

    if not b1 and not b2:
        return False
    if not b1 or not b2:
        raise HTTPException(500, "Both Garmin OAuth token variables are required.")

    os.makedirs(TOKENSTORE_DIR, exist_ok=True)

    try:
        oauth1_bytes = base64.b64decode(b1, validate=True)
        oauth2_bytes = base64.b64decode(b2, validate=True)
        if not all(isinstance(json.loads(value), dict) for value in (oauth1_bytes, oauth2_bytes)):
            raise ValueError("Token files must contain JSON objects")
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Invalid Garmin OAuth token encoding or JSON: {type(e).__name__}",
        )

    try:
        fingerprint = hashlib.sha256(oauth1_bytes + b"\0" + oauth2_bytes).hexdigest()
        marker = Path(TOKENSTORE_DIR) / "env_tokens.sha256"
        # Seed once per env-token pair. Keep refreshed tokens on subsequent attempts
        # and on restarts where a persistent token directory is available.
        if (marker.exists() and marker.read_text() == fingerprint
                and os.path.exists(OAUTH1_PATH) and os.path.exists(OAUTH2_PATH)):
            return True
        with open(OAUTH1_PATH, "wb") as f:
            f.write(oauth1_bytes)
        with open(OAUTH2_PATH, "wb") as f:
            f.write(oauth2_bytes)
        os.chmod(OAUTH1_PATH, 0o600)
        os.chmod(OAUTH2_PATH, 0o600)
        marker.write_text(fingerprint)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed writing token files: {type(e).__name__}",
        )

    os.environ["GARTH_HOME"] = TOKENSTORE_DIR
    return True


def _save_tokens(client: Garmin) -> None:
    with _TOKENSTORE_LOCK:
        client.garth.dump(TOKENSTORE_DIR)
        os.chmod(OAUTH1_PATH, 0o600)
        os.chmod(OAUTH2_PATH, 0o600)


def _new_client(**kwargs) -> Garmin:
    client = Garmin(**kwargs)
    # A response hook also saves tokens refreshed during later API requests.
    def save_refreshed_tokens(response, *args, **kwargs):
        if client.garth.oauth1_token and client.garth.oauth2_token:
            _save_tokens(client)

    client.garth.sess.hooks["response"].append(save_refreshed_tokens)
    return client


def _load_client_from_tokenstore() -> Garmin:
    client = _new_client()
    client.login(TOKENSTORE_DIR)
    _save_tokens(client)
    return client


def _failure_path() -> Path:
    return Path(TOKENSTORE_DIR) / "auth_failure.json"


def garmin_auth_status() -> dict:
    """Read safe local diagnostics without contacting Garmin."""
    failure = _AUTH_FAILURE
    if not failure:
        try:
            failure = json.loads(_failure_path().read_text())
        except (OSError, ValueError):
            failure = {}
    return {
        "client_cached": _GARMIN_CLIENT is not None,
        "last_failure_stage": failure.get("stage"),
        "last_http_status": failure.get("http_status"),
        "retry_after_seconds": max(0, math.ceil(failure.get("retry_at", 0) - time.time())),
    }


def _auth_error(error: Exception) -> HTTPException:
    global _AUTH_FAILURE
    status = None
    stage = "session_initialization"
    retry_after = 0
    rate_limited = isinstance(error, GarminConnectTooManyRequestsError)
    seen = set()
    pending = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        response = getattr(current, "response", None)
        if response is not None:
            status = response.status_code
            path = urlsplit(response.url or "").path
            if path == "/oauth-service/oauth/exchange/user/2.0":
                stage = "oauth2_refresh"
            elif path.startswith("/userprofile-service/"):
                stage = "profile"
            if status == 429:
                rate_limited = True
                value = response.headers.get("Retry-After", "")
                try:
                    retry_after = max(0, int(value))
                except ValueError:
                    try:
                        retry_after = max(0, math.ceil(
                            (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
                        ))
                    except (TypeError, ValueError, OverflowError):
                        pass
        # Garth wraps requests errors in .error rather than always using .__cause__.
        pending.extend(e for e in (
            getattr(current, "__cause__", None),
            getattr(current, "__context__", None),
            getattr(current, "error", None),
        ) if isinstance(e, Exception))

    delay = max(_RATE_LIMIT_QUIET_SECONDS, retry_after) if rate_limited else 60
    _AUTH_FAILURE = {
        "stage": stage,
        "http_status": 429 if rate_limited else status,
        "retry_at": time.time() + delay,
    }
    os.makedirs(TOKENSTORE_DIR, exist_ok=True)
    _failure_path().write_text(json.dumps(_AUTH_FAILURE))
    if rate_limited:
        detail = (
            f"Garmin returned HTTP 429 during {stage}. OAuth requests are paused for {delay} seconds. "
            "This does not prove the tokens are invalid. Password login was not attempted."
        )
    else:
        detail = (
            f"Garmin OAuth session failed during {stage}: {type(error).__name__}. "
            "Password login was not attempted."
        )
    return HTTPException(503 if rate_limited else 502, detail, headers={"Retry-After": str(delay)})


def _login_client_with_credentials() -> Garmin:
    email = (os.getenv("GARMIN_EMAIL") or "").strip()
    password = (os.getenv("GARMIN_PASSWORD") or "").strip()

    if not email or not password:
        raise HTTPException(
            status_code=500,
            detail=(
                "Missing valid OAuth tokens and missing GARMIN_EMAIL/GARMIN_PASSWORD. "
                "Set OAUTH1_B64 and OAUTH2_B64 in Render to avoid password login."
            ),
        )

    os.makedirs(TOKENSTORE_DIR, exist_ok=True)
    os.environ["GARTH_HOME"] = TOKENSTORE_DIR

    client = _new_client(email=email, password=password)
    client.login()

    try:
        _save_tokens(client)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Garmin login succeeded but token persistence failed: {type(e).__name__}",
        )

    return client


def _get_garmin_client() -> Garmin:
    """
    Return a cached authenticated Garmin client.
    Restore OAuth tokens first; only fall back to credentials once per process.
    """
    global _GARMIN_CLIENT, _PASSWORD_LOGIN_ATTEMPTED

    if _GARMIN_CLIENT is not None:
        return _GARMIN_CLIENT

    with _GARMIN_CLIENT_LOCK:
        if _GARMIN_CLIENT is not None:
            return _GARMIN_CLIENT

        retry_after = garmin_auth_status()["retry_after_seconds"]
        if retry_after:
            raise HTTPException(
                503,
                f"Garmin authentication is paused. Retry after {retry_after} seconds; no Garmin request was sent.",
                headers={"Retry-After": str(retry_after)},
            )

        have_env_tokens = _write_tokens_from_env_to_disk()

        have_disk_tokens = os.path.exists(OAUTH1_PATH) and os.path.exists(OAUTH2_PATH)

        if have_env_tokens or have_disk_tokens:
            try:
                _GARMIN_CLIENT = _load_client_from_tokenstore()
                _AUTH_FAILURE.clear()
                _failure_path().unlink(missing_ok=True)
                return _GARMIN_CLIENT
            except Exception as e:
                raise _auth_error(e) from None

        if _PASSWORD_LOGIN_ATTEMPTED:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Garmin credential login was already attempted for this process. "
                    "Add fresh OAUTH1_B64/OAUTH2_B64 tokens in Render before retrying."
                ),
            )

        _PASSWORD_LOGIN_ATTEMPTED = True

        try:
            _GARMIN_CLIENT = _login_client_with_credentials()
            return _GARMIN_CLIENT
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"Garmin password login failed: {type(e).__name__}",
            )


def _activity_in_range(ts: str, start: date, end: date) -> bool:
    """
    Best-effort: Determine whether an activity timestamp is within [start, end].
    Garmin commonly returns 'YYYY-MM-DD HH:MM:SS' or similar strings.
    """
    if not ts:
        return False

    # Most common: "2026-01-01 13:05:13"
    try:
        act_date = ts.split(" ")[0]  # "YYYY-MM-DD"
        return start.isoformat() <= act_date <= end.isoformat()
    except Exception:
        # If parsing fails, keep it rather than accidentally dropping valid data
        return True


def fetch_activities(start: date, end: date) -> List[Activity]:
    """
    Fetch activities between start and end (inclusive).
    NOTE: Garmin sometimes returns activities outside a single requested day,
    so we locally filter by date range.
    """
    client = _get_garmin_client()

    seen = set()
    results: List[Activity] = []

    day = start
    while day <= end:
        try:
            acts = client.get_activities_by_date(day.isoformat())
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"Garmin activities fetch failed: {type(e).__name__}",
            )

        for a in acts or []:
            # Prefer local time; fallback to GMT
            ts = a.get("startTimeLocal") or a.get("startTimeGMT") or ""
            if not _activity_in_range(ts, start, end):
                continue

            activity_id = a.get("activityId")
            if activity_id in seen:
                continue
            seen.add(activity_id)

            results.append(
                Activity(
                    activityId=str(activity_id) if activity_id is not None else None,
                    startTime=ts,
                    type=(a.get("activityType", {}) or {}).get("typeKey"),
                    durationSec=a.get("duration"),
                    distanceM=a.get("distance"),
                    avgHr=a.get("averageHR"),
                    maxHr=a.get("maxHR"),
                    avgPower=a.get("avgPower") or a.get("averagePower"),
                    tss=a.get("trainingStressScore"),
                )
            )

        day = date.fromordinal(day.toordinal() + 1)

    return results


def fetch_wellness(start: date, end: date) -> List[WellnessDay]:
    """
    Fetch wellness summary metrics by day.
    """
    client = _get_garmin_client()
    results: List[WellnessDay] = []

    day = start
    while day <= end:
        d = day.isoformat()

        try:
            daily = client.get_stats_and_body(d)
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"Garmin wellness fetch failed: {type(e).__name__}",
            )

        resting_hr = daily.get("restingHeartRate")
        hrv = daily.get("hrvValue") or daily.get("hrvWeeklyAvg") or daily.get("hrv")

        sleep_score = None
        try:
            sleep = client.get_sleep_data(d)
            sleep_score = sleep.get("sleepScores", {}).get("overall", {}).get("value")
        except Exception:
            pass

        body_battery = (
            (daily.get("bodyBattery", {}) or {}).get("bodyBatteryMax")
            or daily.get("bodyBatteryMax")
        )

        results.append(
            WellnessDay(
                date=d,
                restingHr=resting_hr,
                hrv=hrv,
                sleepScore=sleep_score,
                bodyBattery=body_battery,
            )
        )

        day = date.fromordinal(day.toordinal() + 1)

    return results

from datetime import timedelta

def _safe_get(d: dict, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur

def extract_sleep_metrics_for_day(client: Garmin, day: date) -> dict:
    """
    Returns compact sleep+HRV+BB+readiness metrics for a day.
    Safe against missing keys / device differences.
    """
    d = day.isoformat()

    sleep = client.get_sleep_data(d) or {}
    body = client.get_stats_and_body(d) or {}

    # Training readiness (may not exist for all devices/accounts)
    readiness = None
    readiness_error = None
    try:
        readiness = client.get_training_readiness(d)
    except Exception as e:
        readiness_error = f"{type(e).__name__}: {e}"

    dto = sleep.get("dailySleepDTO") or {}
    sleep_scores = dto.get("sleepScores") or {}
    overall_score = _safe_get(sleep_scores, "overall", "value")

    # Stages (seconds) — most reliable via dailySleepDTO
    stages = {
        "deep": dto.get("deepSleepSeconds"),
        "light": dto.get("lightSleepSeconds"),
        "rem": dto.get("remSleepSeconds"),
        "awake": dto.get("awakeSleepSeconds"),
    }

    # Total sleeping seconds — body has sleepingSeconds; dto has sleepTimeSeconds
    sleeping_seconds = body.get("sleepingSeconds")
    if sleeping_seconds is None:
        sleeping_seconds = dto.get("sleepTimeSeconds")

    # Overnight HRV — sometimes inside sleep payload in different places
    avg_overnight_hrv = (
        sleep.get("avgOvernightHrv")
        or sleep.get("avgOvernightHRV")
        or _safe_get(sleep, "wellnessSpO2SleepSummaryDTO", "avgOvernightHrv")
        or dto.get("avgOvernightHrv")
    )

    hrv_status = sleep.get("hrvStatus") or dto.get("hrvStatus")

    # Body Battery key values (best effort)
    bb = {
        "during_sleep": body.get("bodyBatteryDuringSleep"),
        "at_wake": body.get("bodyBatteryAtWakeTime"),
        "highest": body.get("bodyBatteryHighestValue"),
        "lowest": body.get("bodyBatteryLowestValue"),
    }

    resting_hr = body.get("restingHeartRate") or dto.get("restingHeartRate")

    # Readiness score: readiness sometimes comes as list; keep list but also pick a “best” score
    best_readiness_score = None
    best_readiness_level = None
    if isinstance(readiness, list) and readiness:
        # Prefer AFTER_WAKEUP_RESET if present; else last entry
        pick = None
        for r in readiness:
            if (r or {}).get("inputContext") == "AFTER_WAKEUP_RESET":
                pick = r
                break
        if pick is None:
            pick = readiness[-1]
        best_readiness_score = (pick or {}).get("score")
        best_readiness_level = (pick or {}).get("level")

    return {
        "date": d,
        "sleep_score": overall_score,
        "sleeping_seconds": sleeping_seconds,
        "stages_seconds": stages,
        "avg_overnight_hrv": avg_overnight_hrv,
        "hrv_status": hrv_status,
        "body_battery": bb,
        "resting_hr": resting_hr,
        "training_readiness": readiness,
        "training_readiness_score": best_readiness_score,
        "training_readiness_level": best_readiness_level,
        "training_readiness_error": readiness_error,
    }

from datetime import date, timedelta
from typing import List, Dict, Any


def fetch_sleep_range(start: date, end: date) -> List[Dict[str, Any]]:
    """
    Fetch sleep + recovery metrics for each day in [start, end].
    Returns one dict per night.
    """
    client = _get_garmin_client()
    results: List[Dict[str, Any]] = []

    day = start
    while day <= end:
        day_str = day.isoformat()

        try:
            sleep = client.get_sleep_data(day_str)
            body = client.get_stats_and_body(day_str)
            readiness = client.get_training_readiness(day_str)
        except Exception:
            # Skip days Garmin does not have data for
            day += timedelta(days=1)
            continue

        dto = (sleep or {}).get("dailySleepDTO", {})
        scores = dto.get("sleepScores", {}) if isinstance(dto.get("sleepScores"), dict) else {}

        results.append({
            "date": day_str,

            # ---- Sleep ----
            "sleep_score": scores.get("overall", {}).get("value"),
            "sleep_seconds": dto.get("sleepTimeSeconds"),
            "deep_seconds": dto.get("deepSleepSeconds"),
            "light_seconds": dto.get("lightSleepSeconds"),
            "rem_seconds": dto.get("remSleepSeconds"),
            "awake_seconds": dto.get("awakeSleepSeconds"),

            # ---- HRV ----
            "avg_overnight_hrv": sleep.get("avgOvernightHrv"),
            "hrv_status": sleep.get("hrvStatus"),

            # ---- Resting HR ----
            "resting_hr": body.get("restingHeartRate"),

            # ---- Body Battery ----
            "body_battery_at_wake": body.get("bodyBatteryAtWakeTime"),
            "body_battery_during_sleep": body.get("bodyBatteryDuringSleep"),
            "body_battery_highest": body.get("bodyBatteryHighestValue"),
            "body_battery_lowest": body.get("bodyBatteryLowestValue"),

            # ---- Training Readiness ----
            "training_readiness": readiness,
        })

        day += timedelta(days=1)

    return results

# -----------------------
# Activity zones helpers
# -----------------------
from typing import Dict, Any, Optional
import re


def _extract_time_in_zones(details: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """
    Best-effort extraction of time-in-zone seconds from Garmin activity details payload.

    Returns keys like:
      hr_z1_sec..hr_z5_sec
      pwr_z1_sec..pwr_z7_sec   (some profiles have 7 power zones)
    Values are seconds (float) or None if not found.
    """
    out: Dict[str, Optional[float]] = {}

    # Common patterns seen in Garmin payloads vary by device/account.
    # We'll scan the entire dict for keys that look like time-in-zone.
    for k, v in (details or {}).items():
        if v is None:
            continue
        if not isinstance(v, (int, float)):
            continue

        key = str(k)

        # Examples we try to support:
        # timeInHrZone1, timeInHrZone_1, timeInHRZone1, hrZone1Seconds, etc.
        m = re.match(r"(?i)^time(in)?hrzone[_]?(\d+)$", key) or re.match(
            r"(?i)^hrzone[_]?(\d+)(seconds|sec)?$", key
        )
        if m:
            # m groups may differ depending on which regex matched
            zone_num = m.group(2) if len(m.groups()) >= 2 and m.group(2) else m.group(1)
            try:
                zn = int(zone_num)
                out[f"hr_z{zn}_sec"] = float(v)
            except Exception:
                pass
            continue

        # Power zone patterns:
        # timeInPowerZone1, timeInPowerZone_1, powerZone1Seconds, etc.
        m = re.match(r"(?i)^time(in)?powerzone[_]?(\d+)$", key) or re.match(
            r"(?i)^powerzone[_]?(\d+)(seconds|sec)?$", key
        )
        if m:
            zone_num = m.group(2) if len(m.groups()) >= 2 and m.group(2) else m.group(1)
            try:
                zn = int(zone_num)
                out[f"pwr_z{zn}_sec"] = float(v)
            except Exception:
                pass
            continue

    # Normalize: ensure at least 1–5 HR zones exist as keys (even if None)
    for i in range(1, 6):
        out.setdefault(f"hr_z{i}_sec", None)

    # Normalize: power zones commonly 1–7
    for i in range(1, 8):
        out.setdefault(f"pwr_z{i}_sec", None)

    return out


def fetch_activity_zones(activity_id: str) -> Dict[str, Any]:
    """
    Fetch activity details and return time-in-zone seconds (HR + Power) if available.
    """
    client = _get_garmin_client()

    # 1) Try the most common method name
    details: Dict[str, Any] = {}
    if hasattr(client, "get_activity_details"):
        details = client.get_activity_details(activity_id)
    elif hasattr(client, "get_activity_detail"):
        details = client.get_activity_detail(activity_id)
    else:
        # Last resort: try to fetch a generic "activity" payload if the lib supports it
        if hasattr(client, "get_activity"):
            details = client.get_activity(activity_id)
        else:
            raise RuntimeError("Garmin client has no activity-details method")

    zones = _extract_time_in_zones(details)
    return {
        "activityId": activity_id,
        "zones": zones,
        "has_hr_zones": any(v is not None for k, v in zones.items() if k.startswith("hr_")),
        "has_power_zones": any(
            v is not None for k, v in zones.items() if k.startswith("pwr_")
        ),
    }

def _extract_time_in_zones(details: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """
    Best-effort extraction of time-in-zone seconds from Garmin activity details payload.
    Handles both:
      - flat keys like timeInHrZone1
      - nested dict/list structures (common in Garmin payloads)

    Output:
      hr_z1_sec..hr_z5_sec
      pwr_z1_sec..pwr_z7_sec
    """
    out: Dict[str, Optional[float]] = {}

    def set_zone(prefix: str, zone: int, seconds: float):
        key = f"{prefix}_z{zone}_sec"
        if seconds is None:
            return
        # if it looks like milliseconds, convert to seconds
        s = float(seconds)
        if s > 100000:  # crude but safe for zone times
            s = s / 1000.0
        # sum if multiple segments exist
        out[key] = (out.get(key) or 0.0) + s

    def walk(node: Any, path: str = ""):
        if isinstance(node, dict):
            for k, v in node.items():
                key = str(k)
                p = f"{path}.{key}" if path else key

                # 1) Flat key patterns (top-level or nested)
                if isinstance(v, (int, float)):
                    m = re.match(r"(?i)^time(in)?hrzone[_]?(\d+)$", key) or re.match(
                        r"(?i)^hrzone[_]?(\d+)(seconds|sec)?$", key
                    )
                    if m:
                        zone_num = m.group(2) if len(m.groups()) >= 2 and m.group(2) else m.group(1)
                        try:
                            set_zone("hr", int(zone_num), float(v))
                        except Exception:
                            pass
                        continue

                    m = re.match(r"(?i)^time(in)?powerzone[_]?(\d+)$", key) or re.match(
                        r"(?i)^powerzone[_]?(\d+)(seconds|sec)?$", key
                    )
                    if m:
                        zone_num = m.group(2) if len(m.groups()) >= 2 and m.group(2) else m.group(1)
                        try:
                            set_zone("pwr", int(zone_num), float(v))
                        except Exception:
                            pass
                        continue

                # 2) Recurse
                walk(v, p)

        elif isinstance(node, list):
            for i, item in enumerate(node):
                p = f"{path}[{i}]"
                # Common “time in zones” list item shapes:
                # { "zoneNumber": 1, "seconds": 1234 }
                # { "zone": 1, "timeInSeconds": 1234 }
                # { "start":..., "end":..., "zone": ... }
                if isinstance(item, dict):
                    # Try to infer HR vs Power from path
                    path_l = path.lower()
                    is_hr = any(s in path_l for s in ["hr", "heart", "heartrate"])
                    is_pwr = any(s in path_l for s in ["power", "pwr", "watts"])

                    zone = item.get("zoneNumber") or item.get("zone") or item.get("zoneIndex")
                    secs = (
                        item.get("seconds")
                        or item.get("sec")
                        or item.get("timeInSeconds")
                        or item.get("durationInSeconds")
                        or item.get("time")
                        or item.get("duration")
                    )

                    if zone is not None and secs is not None:
                        try:
                            z = int(zone)
                            if is_hr:
                                set_zone("hr", z, float(secs))
                            elif is_pwr:
                                set_zone("pwr", z, float(secs))
                            # If we can't infer, still recurse — sometimes nested deeper
                        except Exception:
                            pass

                walk(item, p)

        # primitives: ignore

    walk(details or {})

    # Normalize expected keys
    for i in range(1, 6):
        out.setdefault(f"hr_z{i}_sec", None)
    for i in range(1, 8):
        out.setdefault(f"pwr_z{i}_sec", None)

    return out
