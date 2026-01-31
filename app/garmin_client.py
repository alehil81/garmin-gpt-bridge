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
