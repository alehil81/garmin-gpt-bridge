from pydantic import BaseModel
from typing import List, Optional

class Activity(BaseModel):
    activityId: str
    startTime: str  # ISO string
    type: str
    durationSec: float
    distanceM: Optional[float] = None
    avgHr: Optional[float] = None
    maxHr: Optional[float] = None
    avgPower: Optional[float] = None
    tss: Optional[float] = None

class ActivitiesResponse(BaseModel):
    activities: List[Activity]

class WellnessDay(BaseModel):
    date: str  # YYYY-MM-DD
    restingHr: Optional[float] = None
    hrv: Optional[float] = None
    sleepScore: Optional[float] = None
    bodyBattery: Optional[float] = None

class WellnessResponse(BaseModel):
    days: List[WellnessDay]

class DailySummaryResponse(BaseModel):
    date: str
    steps: Optional[float] = None
    calories: Optional[float] = None
    restingHr: Optional[float] = None
    hrv: Optional[float] = None
