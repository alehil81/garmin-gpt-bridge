import os
from datetime import date
from typing import List

from fastapi import HTTPException
from garminconnect import Garmin

from .models import Activity, WellnessDay

TOKEN_DIR = "/tmp/garminconnect"
TOKEN_PATH = os.path.join(TOKEN_DIR, "tokens.json")


def _get_garmin_client() -> Garmin:
    email = (os.getenv("GARMIN_EMAIL") or "").strip()
    password = (os.getenv("GARMIN_PASSWORD") or "").strip()

    if not email or not password:
        raise HTTPException(status_code=500, detail="Missing GARMIN_EMAIL or GARMIN_PASSWORD env vars")

    os.makedirs(TOKEN_DIR, exist_ok=True)

    client = Garmin(email=email, password=password)

    try:
        # Try token-based login first (preferred)
        if os.path.exists(TOKEN_PATH):
            try:
                client.login(TOKEN_PATH)
                return client
            except Exception:
                # Fall back to password login
                pass

        # Password login (may trigger MFA depending on account)
        client.login()

        # Save tokens for next time (best-effort)
        try:
            # Some versions use client.garth.dump; if not available, ignore
            if hasattr(client, "garth") and hasattr(client.garth, "dump"):
                client.garth.dump(TOKEN_PATH)
        except Exception:
            pass

        return client

    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Garmin login failed: {type(e).__name__}")


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

        for a in acts or []:
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
