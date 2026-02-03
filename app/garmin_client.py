import os
import base64
from datetime import date
from typing import List

from fastapi import HTTPException
from garminconnect import Garmin

from .models import Activity, WellnessDay

# Tokenstore directory on Render (ephemeral but fine for runtime)
TOKENSTORE_DIR = "/tmp/.garth"
OAUTH1_PATH = os.path.join(TOKENSTORE_DIR, "oauth1_token.json")
OAUTH2_PATH = os.path.join(TOKENSTORE_DIR, "oauth2_token.json")


def _write_tokens_from_env_to_disk() -> None:
    """
    Decode GARTH_OAUTH1_B64 and GARTH_OAUTH2_B64 env vars and write them
    into TOKENSTORE_DIR as oauth1_token.json / oauth2_token.json.
    """
    b1 = os.getenv("GARTH_OAUTH1_B64")
    b2 = os.getenv("GARTH_OAUTH2_B64")

    if not b1 or not b2:
        raise HTTPException(
            status_code=500,
            detail="Missing GARTH_OAUTH1_B64 or GARTH_OAUTH2_B64 env vars",
        )

    os.makedirs(TOKENSTORE_DIR, exist_ok=True)

    try:
        oauth1_bytes = base64.b64decode(b1)
        oauth2_bytes = base64.b64decode(b2)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed base64 decode of GARTH tokens: {type(e).__name__}",
        )

    try:
        with open(OAUTH1_PATH, "wb") as f:
            f.write(oauth1_bytes)
        with open(OAUTH2_PATH, "wb") as f:
            f.write(oauth2_bytes)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed writing token files: {type(e).__name__}",
        )


def _get_garmin_client() -> Garmin:
    """
    Create a Garmin client and authenticate using tokenstore directory.
    This avoids email/password login (and avoids repeated MFA prompts).
    """
    _write_tokens_from_env_to_disk()

    try:
        client = Garmin()  # no username/password
        # IMPORTANT: tokenstore must be a DIRECTORY containing oauth1/2 json files
        client.login(TOKENSTORE_DIR)
        return client
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Garmin token login failed: {type(e).__name__}",
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
        "activityId": str(activity_id),
        "zones": zones,
        # keep a tiny hint for debugging without dumping everything
        "details_keys_sample": sorted(list(details.keys()))[:50],
    }
