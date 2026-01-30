from datetime import date
from fastapi import Depends, FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from .auth import require_bearer_token
from .models import ActivitiesResponse, WellnessResponse, DailySummaryResponse
from .garmin_client import fetch_activities, fetch_wellness

app = FastAPI(title="Garmin GPT Bridge", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"ok": True}

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
