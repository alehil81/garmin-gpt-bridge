from __future__ import annotations

from datetime import date
import os
import hashlib
import inspect
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse

from .auth import require_bearer_token
from .models import ActivitiesResponse, WellnessResponse, DailySummaryResponse
from .garmin_client import fetch_activities, fetch_wellness, _get_garmin_client

app = FastAPI(title="Garmin GPT Bridge", version="1.0.0")

from fastapi import Header

@app.get("/debug_auth")
def debug_auth(authorization: str | None = Header(default=None)):
    # Don’t ever return the raw token
    if not authorization:
        return {"has_authorization_header": False}

    return {
        "has_authorization_header": True,
        "starts_with_bearer": authorization.startswith("Bearer "),
        "auth_length": len(authorization),
        "auth_prefix": authorization[:12],  # e.g. "Bearer abc..."
    }

from fastapi import Depends
from .auth import require_bearer_token

@app.get("/debug_auth")
def debug_auth(_auth: None = Depends(require_bearer_token)):
    return {"ok": True, "msg": "auth passed"}

# -----------------------
# Public endpoints
# -----------------------
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/version")
def version():
    return {"version": "1.0.0"}


@app.get("/")
def root():
    return {
        "name": "garmin-gpt-bridge",
        "status": "ok",
        "endpoints": [
            "/",
            "/health",
            "/version",
            "/auth_fingerprint",
            "/activities",
            "/wellness",
            "/daily_summary",
            "/sleep_summary",
            "/debug_env",
            "/debug_source",
            "/debug_sleep",
        ],
    }


# -----------------------
# Auth debug (protected)
# -----------------------
@app.get("/auth_fingerprint")
def auth_fingerprint(_auth: None = Depends(require_bearer_token)):
    api_key = (os.getenv("API_KEY") or "").strip()

    # Strip accidental quotes if user pasted them into Render
    if (api_key.startswith('"') and api_key.endswith('"')) or (
        api_key.startswith("'") and api_key.endswith("'")
    ):
        api_key = api_key[1:-1].strip()

    fp = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12] if api_key else ""
    return {"fingerprint": fp}


# -----------------------
# Core endpoints (protected)
# -----------------------
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


# -----------------------
# Debug helpers (protected)
# -----------------------
@app.get("/debug_env")
def debug_env(_auth: None = Depends(require_bearer_token)):
    # Safe: only reports whether variables exist + lengths
    def _val(name: str) -> str:
        return (os.getenv(name) or "").strip()

    api = _val("API_KEY")
    g1 = _val("GARMIN_OAUTH1_B64")
    g2 = _val("GARMIN_OAUTH2_B64")
    email = _val("GARMIN_EMAIL")
    pwd = _val("GARMIN_PASSWORD")

    return {
        "has_api_key": bool(api),
        "len_api_key": len(api),
        "has_oauth1_b64": bool(g1),
        "has_oauth2_b64": bool(g2),
        "len_oauth1_b64": len(g1),
        "len_oauth2_b64": len(g2),
        "has_garmin_email": bool(email),
        "has_garmin_password": bool(pwd),
    }


@app.get("/debug_source")
def debug_source(_auth: None = Depends(require_bearer_token)):
    # Shows which code is deployed (helps cache/debug)
    try:
        src = inspect.getsource(fetch_activities)
    except Exception:
        src = "<could not read source>"
    try:
        import app.garmin_client as gc  # type: ignore
        file_path = getattr(gc, "__file__", None)
    except Exception:
        file_path = None
    return {
        "garmin_client_file": file_path,
        "fetch_activities_snippet": (src[:900] + "...") if isinstance(src, str) else str(src),
    }


@app.get("/debug_sleep")
def debug_sleep(
    day: date = Query(...),
    _auth: None = Depends(require_bearer_token),
):
    """
    Raw Garmin responses for one day.
    NOTE: This can be large.
    """
    try:
        client = _get_garmin_client()
        sleep = client.get_sleep_data(day.isoformat())
        body = client.get_stats_and_body(day.isoformat())
        try:
            readiness = client.get_training_readiness(day.isoformat())
        except Exception as e:
            readiness = None
            readiness_error = f"{type(e).__name__}: {e}"
        else:
            readiness_error = None

        # return keys too so you can see shape quickly
        sleep_keys = list(sleep.keys()) if isinstance(sleep, dict) else []
        return JSONResponse(
            {
                "day": day.isoformat(),
                "sleep_keys": sleep_keys,
                "sleep": sleep,
                "body": body,
                "training_readiness": readiness,
                "training_readiness_error": readiness_error,
            }
        )
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail={
                "where": "/debug_sleep",
                "error_type": type(e).__name__,
                "error": str(e),
            },
        )


# -----------------------
# Sleep summary (protected)
# -----------------------
@app.get("/sleep_summary")
def sleep_summary(
    day: date = Query(...),
    _auth: None = Depends(require_bearer_token),
):
    """
    Compact sleep summary for a day:
    - sleep score (overall)
    - sleep time (seconds)
    - stages seconds (deep/light/rem/awake)
    - avg overnight HRV + status (if present)
    - body battery during sleep + at wake + highest/lowest (if present)
    - resting HR (if present)
    - training readiness (if available)
    """
    try:
        client = _get_garmin_client()
        sleep = client.get_sleep_data(day.isoformat())
        body = client.get_stats_and_body(day.isoformat())

        # Training readiness
        try:
            readiness = client.get_training_readiness(day.isoformat())
            readiness_error = None
        except Exception as e:
            readiness = None
            readiness_error = f"{type(e).__name__}: {e}"

        # ---- Robustly locate dailySleepDTO (handles multiple shapes) ----
        dto: Dict[str, Any] = {}
        if isinstance(sleep, dict):
            if isinstance(sleep.get("dailySleepDTO"), dict):
                dto = sleep["dailySleepDTO"]
            elif isinstance(sleep.get("sleep"), dict) and isinstance(sleep["sleep"].get("dailySleepDTO"), dict):
                dto = sleep["sleep"]["dailySleepDTO"]

        # Sleep score
        sleep_scores = dto.get("sleepScores") if isinstance(dto, dict) else None
        sleep_score = None
        if isinstance(sleep_scores, dict):
            overall = sleep_scores.get("overall")
            if isinstance(overall, dict):
                sleep_score = overall.get("value")

        # Sleep seconds
        sleeping_seconds = dto.get("sleepTimeSeconds") if isinstance(dto, dict) else None
        if sleeping_seconds is None and isinstance(body, dict):
            sleeping_seconds = body.get("sleepingSeconds")

        # Stages seconds (most reliable from dailySleepDTO)
        stages_seconds = {
            "deep": dto.get("deepSleepSeconds") if isinstance(dto, dict) else None,
            "light": dto.get("lightSleepSeconds") if isinstance(dto, dict) else None,
            "rem": dto.get("remSleepSeconds") if isinstance(dto, dict) else None,
            "awake": dto.get("awakeSleepSeconds") if isinstance(dto, dict) else None,
        }

        # HRV: can live in different places depending on Garmin payload
        avg_overnight_hrv = None
        hrv_status = None
        if isinstance(sleep, dict):
            # Sometimes at top-level in sleep response
            avg_overnight_hrv = sleep.get("avgOvernightHrv") or sleep.get("avg_overnight_hrv")
            hrv_status = sleep.get("hrvStatus") or sleep.get("hrv_status")
            # Sometimes nested inside sleep["sleep"]
            if avg_overnight_hrv is None and isinstance(sleep.get("sleep"), dict):
                avg_overnight_hrv = sleep["sleep"].get("avgOvernightHrv") or sleep["sleep"].get("avg_overnight_hrv")
            if hrv_status is None and isinstance(sleep.get("sleep"), dict):
                hrv_status = sleep["sleep"].get("hrvStatus") or sleep["sleep"].get("hrv_status")

        # Body Battery: best effort from body daily stats
        body_battery = {
            "during_sleep": None,
            "at_wake": None,
            "highest": None,
            "lowest": None,
        }
        if isinstance(body, dict):
            body_battery["during_sleep"] = body.get("bodyBatteryDuringSleep")
            body_battery["at_wake"] = body.get("bodyBatteryAtWakeTime")
            body_battery["highest"] = body.get("bodyBatteryHighestValue")
            body_battery["lowest"] = body.get("bodyBatteryLowestValue")

        resting_hr = body.get("restingHeartRate") if isinstance(body, dict) else None

        return {
            "date": day.isoformat(),
            "sleep_score": sleep_score,
            "sleeping_seconds": sleeping_seconds,
            "stages_seconds": stages_seconds,
            "avg_overnight_hrv": avg_overnight_hrv,
            "hrv_status": hrv_status,
            "body_battery": body_battery,
            "resting_hr": resting_hr,
            "training_readiness": readiness,
            "training_readiness_error": readiness_error,
        }

    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail={
                "where": "/sleep_summary",
                "error_type": type(e).__name__,
                "error": str(e),
            },
        )
