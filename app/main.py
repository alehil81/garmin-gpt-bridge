from datetime import date
from fastapi import Depends, FastAPI, Query

import inspect
from . import garmin_client
from .auth import require_bearer_token
from .models import ActivitiesResponse, WellnessResponse, DailySummaryResponse
from .garmin_client import fetch_activities, fetch_wellness

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
