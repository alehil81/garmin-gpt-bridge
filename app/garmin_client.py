import os
import base64
from datetime import date
from typing import List

from fastapi import HTTPException
from garth import Client as GarthClient
from garminconnect import Garmin

from .models import Activity, WellnessDay

GARTH_HOME = "/tmp/.garth"
OAUTH1_PATH = os.path.join(GARTH_HOME, "oauth1_token.json")
OAUTH2_PATH = os.path.join(GARTH_HOME, "oauth2_token.json")


def _ensure_garth_tokens_from_env() -> None:
    """
    Decode GARTH_OAUTH1_B64 and GARTH_OAUTH2_B64 into /tmp/.garth.
    """
    b1 = os.getenv("GARTH_OAUTH1_B64")
    b2 = os.getenv("GARTH_OAUTH2_B64")
    if not b1 or not b2:
        raise HTTPException(status_code=500, detail="Missing GARTH_OAUTH1_B64 or GARTH_OAUTH2_B64 env vars")

    os.makedirs(GARTH_HOME, exist_ok=True)

    try:
        with open(OAUTH1_PATH, "wb") as f:
            f.write(base64.b64decode(b1))
        with open(OAUTH2_PATH, "wb") as f:
            f.write(base64.b64decode(b2))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed decoding GARTH tokens: {type(e).__name__}")

    # Make sure garth reads from our directory
    os.environ["GARTH_HOME"] = GARTH_HOME


def _get_garmin_client() -> Garmin:
    """
    Build a Garmin client that uses already-authenticated garth tokens.
    No email/password login. No MFA prompts.
    """
    _ensure_garth_tokens_from_env()

    try:
        g = GarthClient()
        g.load(GARTH_HOME)  # reads oauth1_token.json + oauth2_token.json
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Garth token load failed: {type(e).__name__}")

    try:
        client = Garmin(garth=g)
        # Force a lightweight call to confirm tokens are valid (avoids silent failures)
        _ = g.profile
        return client
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Garmin token client init failed: {type(e).__name__}")


def fetch_activities(start: date, end: date) -> List[Activity]:
    client = _get_garmin_client()

    seen = set()
    results: List[Activity] = []

    day = start
    while day <= end:
        try:
            acts = client.get_activities_by_date(day.isoformat())
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Garmin activities fetch failed: {type(e).__name__}")

        for a in acts:
            activity_id = a.get("activityId")
            if activity_id in seen:
                continue
            seen.add(activity_id)

            results.append(
                Activity(
                    activityId=str(activity_id) if activity_id is not None else None,
                    startTime=a.get("startTimeLocal") or a.get("startTimeGMT"),
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
    client = _get_garmin_client()
    results: List[WellnessDay] = []

    day = start
    while day <= end:
        d = day.isoformat()

        try:
            daily = client.get_stats_and_body(d)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Garmin wellness fetch failed: {type(e).__name__}")

        resting_hr = daily.get("restingHeartRate")
        hrv = daily.get("hrvValue") or daily.get("hrvWeeklyAvg") or daily.get("hrv")

        sleep_score = None
        try:
            sleep = client.get_sleep_data(d)
            sleep_score = sleep.get("sleepScores", {}).get("overall", {}).get("value")
        except Exception:
            pass

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
