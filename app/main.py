from datetime import date
from fastapi import Depends, FastAPI, Query

import inspect
from . import garmin_client
from .auth import require_bearer_token
from .models import ActivitiesResponse, WellnessResponse, DailySummaryResponse
from .garmin_client import fetch_activities, fetch_wellness, _get_garmin_client

app = FastAPI(title="Garmin GPT Bridge", version="1.0.0")

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/version")
def version():
    return {"version": "1.0.0"}

@app.get("/activities", response_model=ActivitiesResponse)
def get_activities(
    start: date = Query(...),
    end: date = Query(...),
    _auth: None = Depends(require_bearer_token),
):
    acts = fetch_activities(start, end)
    return ActivitiesResponse(activities=acts)

@app.get("/wellness", response_model=WellnessResponse)
def get_wellness(
    start: date = Query(...),
    end: date = Query(...),
    _auth: None = Depends(require_bearer_token),
):
    days = fetch_wellness(start, end)
    return WellnessResponse(days=days)

@app.get("/daily_summary", response_model=DailySummaryResponse)
def get_daily_summary(
    date_: date = Query(..., alias="date"),
    _auth: None = Depends(require_bearer_token),
):
    wellness = fetch_wellness(date_, date_)
    d = wellness[0] if wellness else None
    return DailySummaryResponse(
        date=date_.isoformat(),
        steps=None,
        calories=None,
        restingHr=d.restingHr if d else None,
        hrv=d.hrv if d else None,
    )

@app.get("/")
def root():
    return {
        "name": "garmin-gpt-bridge",
        "status": "ok",
        "endpoints": ["/health", "/version", "/activities", "/wellness", "/daily_summary"]
    }

import os
import hashlib

@app.get("/auth_fingerprint")
def auth_fingerprint(_auth: None = Depends(require_bearer_token)):
    api_key = (os.getenv("API_KEY") or "").strip()
    if (
        (api_key.startswith('"') and api_key.endswith('"'))
        or (api_key.startswith("'") and api_key.endswith("'"))
    ):
        api_key = api_key[1:-1].strip()

    fp = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
    return {"fingerprint": fp}

@app.get("/debug_source")
def debug_source():
    return {
        "garmin_client_file": garmin_client.__file__,
        "fetch_activities_snippet": "\n".join(inspect.getsource(garmin_client.fetch_activities).splitlines()[:12]),
    }

import os

@app.get("/debug_env")
def debug_env(_auth: None = Depends(require_bearer_token)):
    b1 = os.getenv("GARTH_OAUTH1_B64")
    b2 = os.getenv("GARTH_OAUTH2_B64")
    return {
        "has_oauth1_b64": bool(b1),
        "has_oauth2_b64": bool(b2),
        "len_oauth1_b64": len(b1) if b1 else 0,
        "len_oauth2_b64": len(b2) if b2 else 0,
    }

from datetime import date
from fastapi import Query, Depends
from fastapi.responses import JSONResponse

from fastapi import HTTPException
from fastapi.responses import JSONResponse

@app.get("/debug_sleep")
def debug_sleep(
    day: date = Query(...),
    _auth: None = Depends(require_bearer_token),
):
    try:
        client = _get_garmin_client()

        sleep = client.get_sleep_data(day.isoformat())
        body = client.get_stats_and_body(day.isoformat())

        try:
            readiness = client.get_training_readiness(day.isoformat())
        except Exception as e:
            readiness = {"error_type": type(e).__name__, "error": str(e)}

        return JSONResponse({
            "day": day.isoformat(),
            "sleep_keys": list(sleep.keys()) if isinstance(sleep, dict) else str(type(sleep)),
            "body_keys": list(body.keys()) if isinstance(body, dict) else str(type(body)),
            "training_readiness": readiness,
            "sleep": sleep,
            "body": body,
        })

    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail={
                "where": "/debug_sleep",
                "error_type": type(e).__name__,
                "error": str(e),
            },
        )

    from datetime import date
from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse

from .auth import require_bearer_token
from .garmin_client import _get_garmin_client

@app.get("/sleep_summary")
def sleep_summary(
    day: date = Query(..., description="Wake-date (YYYY-MM-DD). Try the next day if empty."),
    _auth: None = Depends(require_bearer_token),
):
    client = _get_garmin_client()
    d = day.isoformat()

    # Pull raw objects
    sleep = client.get_sleep_data(d)
    body = client.get_stats_and_body(d)

    # --- Sleep score (best-effort; keys vary) ---
    sleep_score = None
    try:
        sleep_score = (
            sleep.get("sleepScores", {})
                .get("overall", {})
                .get("value")
        )
    except Exception:
        pass

    # --- Sleep duration + stages (best-effort) ---
    sleeping_seconds = body.get("sleepingSeconds")
    stages = sleep.get("sleepLevelsMap") or sleep.get("sleepLevelMap") or {}

    # Common stage keys vary; try typical Garmin ones
    deep_sec = stages.get("deepSleepSeconds") or stages.get("deepSeconds")
    light_sec = stages.get("lightSleepSeconds") or stages.get("lightSeconds")
    rem_sec = stages.get("remSleepSeconds") or stages.get("remSeconds")
    awake_sec = stages.get("awakeSleepSeconds") or stages.get("awakeSeconds")

    # --- HRV (you already saw it!) ---
    avg_overnight_hrv = sleep.get("avgOvernightHrv")
    hrv_status = sleep.get("hrvStatus")

    # --- Body Battery (from daily body stats) ---
    bb_during_sleep = body.get("bodyBatteryDuringSleep")
    bb_at_wake = body.get("bodyBatteryAtWakeTime")
    bb_high = body.get("bodyBatteryHighestValue")
    bb_low = body.get("bodyBatteryLowestValue")

    # --- Resting HR ---
    resting_hr = body.get("restingHeartRate") or sleep.get("restingHeartRate")

    # --- Training readiness (may not exist for all accounts/devices) ---
    readiness = None
    readiness_err = None
    try:
        readiness = client.get_training_readiness(d)
    except Exception as e:
        readiness_err = f"{type(e).__name__}: {str(e)}"

    return JSONResponse({
        "date": d,
        "sleep_score": sleep_score,
        "sleeping_seconds": sleeping_seconds,
        "stages_seconds": {
            "deep": deep_sec,
            "light": light_sec,
            "rem": rem_sec,
            "awake": awake_sec,
        },
        "avg_overnight_hrv": avg_overnight_hrv,
        "hrv_status": hrv_status,
        "body_battery": {
            "during_sleep": bb_during_sleep,
            "at_wake": bb_at_wake,
            "highest": bb_high,
            "lowest": bb_low,
        },
        "resting_hr": resting_hr,
        "training_readiness": readiness,
        "training_readiness_error": readiness_err,
    })
