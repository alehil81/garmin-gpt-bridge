import os
from datetime import date, datetime
from typing import List

from garminconnect import Garmin
from fastapi import HTTPException
from .models import Activity, WellnessDay


TOKEN_DIR = "/tmp/garminconnect"
TOKEN_PATH = os.path.join(TOKEN_DIR, "tokens.json")


def _get_garmin_client() -> Garmin:
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        raise RuntimeError("Missing GARMIN_EMAIL or GARMIN_PASSWORD env vars")

    os.makedirs(TOKEN_DIR, exist_ok=True)

client = Garmin(email=email, password=password)

try:
    if os.path.exists(TOKEN_PATH):
        try:
            client.login(TOKEN_PATH)
            return client
        except Exception:
            pass

    client.login()
    try:
        client.garth.dump(TOKEN_PATH)
    except Exception:
        pass

    return client

except Exception as e:
    # Surface a clean error to callers (and therefore to GPT)
    raise HTTPException(status_code=502, detail=f"Garmin login failed: {type(e).__name__}")
    try:
        client.garth.dump(TOKEN_PATH)
    except Exception:
        # Not fatal; we'll just re-login next time if token save fails
        pass

    return client


def fetch_activities(start: date, end: date) -> List[Activity]:
    """
    Fetch activities between start and end (inclusive).
    """
    client = _get_garmin_client()

    # garminconnect returns activities paged; easiest: fetch by date range
    # We'll loop days and merge results, de-duplicating by activityId.
    seen = set()
    results: List[Activity] = []

    day = start
    while day <= end:
        # get_activities_by_date returns list of dicts for a single day
        acts = client.get_activities_by_date(day.isoformat())

        for a in acts:
            activity_id = a.get("activityId")
            if activity_id in seen:
                continue
            seen.add(activity_id)

            # Map to our Activity model (best-effort; fields vary)
            results.append(
                Activity(
                    activityId=str(activity_id) if activity_id is not None else None,
                    startTimeLocal=a.get("startTimeLocal") or a.get("startTimeGMT"),
                    activityName=a.get("activityName") or a.get("activityType", {}).get("typeKey"),
                    activityType=(a.get("activityType", {}) or {}).get("typeKey"),
                    duration=a.get("duration"),
                    distance=a.get("distance"),
                    avgHr=a.get("averageHR"),
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

        # "Wellness" endpoints vary; we'll use daily summary + sleep + stress where available.
        daily = client.get_stats_and_body(d)

        resting_hr = daily.get("restingHeartRate")
        # HRV: Garmin often exposes HRV as nightly average; some accounts/devices expose it differently.
        # We'll attempt to read common keys.
        hrv = daily.get("hrvValue") or daily.get("hrvWeeklyAvg") or daily.get("hrv")

        # Sleep score may be separate; try best-effort
        sleep_score = None
        try:
            sleep = client.get_sleep_data(d)
            sleep_score = sleep.get("sleepScores", {}).get("overall", {}).get("value")
        except Exception:
            pass

        # Body Battery often in daily stats
        body_battery = daily.get("bodyBattery", {}).get("bodyBatteryMax") or daily.get("bodyBatteryMax")

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
