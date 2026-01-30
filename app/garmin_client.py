from datetime import date, datetime, timedelta
from typing import List
from .models import Activity, WellnessDay

def fetch_activities(start: date, end: date) -> List[Activity]:
    """
    TODO: Replace with real Garmin Connect pulls.
    For now: deterministic mock sample data.
    """
    out: List[Activity] = []
    cur = start
    i = 1
    while cur <= end:
        if (cur.toordinal() - start.toordinal()) % 2 == 0:
            dt = datetime(cur.year, cur.month, cur.day, 6, 30, 0)
            out.append(Activity(
                activityId=f"mock-{i}",
                startTime=dt.isoformat(),
                type="cycling",
                durationSec=3600,
                distanceM=25000,
                avgHr=145,
                maxHr=172,
                avgPower=150,
                tss=55,
            ))
            i += 1
        cur += timedelta(days=1)
    return out

def fetch_wellness(start: date, end: date) -> List[WellnessDay]:
    """
    TODO: Replace with real Garmin Connect pulls.
    """
    out: List[WellnessDay] = []
    cur = start
    base_hrv = 55
    base_rhr = 54
    while cur <= end:
        out.append(WellnessDay(
            date=cur.isoformat(),
            restingHr=base_rhr + ((cur.toordinal() % 3) - 1),
            hrv=base_hrv + ((cur.toordinal() % 5) - 2),
            sleepScore=80 + ((cur.toordinal() % 7) - 3),
            bodyBattery=70 + ((cur.toordinal() % 9) - 4),
        ))
        cur += timedelta(days=1)
    return out
