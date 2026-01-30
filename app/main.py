from datetime import date
from fastapi import Depends, FastAPI, Query

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
