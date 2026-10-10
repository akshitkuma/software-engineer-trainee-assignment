"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query,Path
from pydantic import BaseModel, Field, StrictInt, ConfigDict
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import json_util
import json

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

def to_utc_datetime(punched_at: Optional[int] = None) -> datetime:
    """Convert epoch milliseconds to a naive UTC datetime, truncated to seconds."""
    if punched_at is None:
        instant = datetime.now(timezone.utc)
    else:
        instant = datetime.fromtimestamp(punched_at / 1000, tz=timezone.utc)

    instant = instant.replace(microsecond=0)
    return instant.astimezone(timezone.utc).replace(tzinfo=None)


def get_attendance_date(utc_punch_in: datetime, emp: dict) -> str:
    """Return the attendance date in IST, accounting for overnight shifts."""
    ist_punch_in = utc_punch_in.replace(tzinfo=timezone.utc).astimezone(IST)
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    start_minutes = int(shift_start[:2]) * 60 + int(shift_start[3:])
    end_minutes = int(shift_end[:2]) * 60 + int(shift_end[3:])
    punch_minutes = ist_punch_in.hour * 60 + ist_punch_in.minute

    attendance_day = ist_punch_in.date()

    if end_minutes <= start_minutes and punch_minutes < end_minutes:
        attendance_day -= timedelta(days=1)

    return attendance_day.isoformat()

def serialize_attendance(doc: dict) -> dict:
    """Convert MongoDB attendance document to API response format."""
    result = dict(doc)
    result.pop("_id", None)

    for field in ("punch_in", "punch_out"):
        value = result.get(field)
        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            result[field] = int(value.timestamp() * 1000)

    history = []
    for entry in result.get("history", []):
        item = dict(entry)
        if item.get("at") is not None:
            value = item["at"]
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            item["at"] = int(value.timestamp() * 1000)

        changes = item.get("changes", {})
        for change in changes.values():
            for key in ("from", "to"):
                value = change.get(key)
                if isinstance(value, datetime):
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=timezone.utc)
                    change[key] = int(value.timestamp() * 1000)

        history.append(item)

    result["history"] = history
    return result



app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")



def explain_find(collection, query, sort=None):
    find_command = {
        "find": collection.name,
        "filter": query,
    }

    if sort:
        find_command["sort"] = dict(sort)

    result = db.command(
        "explain",
        find_command,
        verbosity="executionStats"
    )

    # Convert BSON Timestamp and other BSON values to JSON-safe values
    return json.loads(json_util.dumps(result))




@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employees_emp_code",
    )
    db.attendance_logs.create_index(
    [("date", DESCENDING), ("emp_code", ASCENDING)],
    name="attendance_date_desc_emp_code_asc"
    )
    
    db.attendance_logs.create_index(
    [("emp_code", ASCENDING), ("date", ASCENDING)],
    name="attendance_emp_code_date"
    )

    db.attendance_logs.create_index(
    [("date", ASCENDING), ("late_minutes", ASCENDING)],
    name="attendance_date_late_minutes"
    )

    db.employees.create_index(
    [("department", ASCENDING), ("joined_on", ASCENDING)],
    name="employees_department_joined_on"
    )




# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #



def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))

    start = punch_in.replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0
    )

    # Handle overnight shift: an early-morning punch-in
    # may belong to the previous day's shift start.
    if start > punch_in:
        start -= timedelta(days=1)

    elapsed_minutes = int(
        (punch_in - start).total_seconds() // 60
    )

    # Assignment rule: late only when strictly more than 10 minutes.
    elapsed_seconds = (punch_in - start).total_seconds()
    if elapsed_seconds <= 10 * 60:
       return 0
    return int(elapsed_seconds // 60)

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """Calculate work hours rounded to two decimals using half-up rounding."""
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = (seconds / Decimal("3600")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(hours)




def compute_overtime(
    punch_out: datetime, shift_end: str, date_str: str
) -> int:
    """Calculate overtime after shift end, applying a 30-minute threshold."""
    h, m = map(int, shift_end.split(":"))
    shift_date = date.fromisoformat(date_str)

    # punch_out is expected to be in the same timezone as the shift.
    scheduled_end = datetime(
        shift_date.year,
        shift_date.month,
        shift_date.day,
        h,
        m,
        tzinfo=punch_out.tzinfo,
    )

    # If punch-out is on the next day and the scheduled end
    # is an early-morning time, use the next day's shift end.
    if punch_out.date() > shift_date and h < 12:
        scheduled_end += timedelta(days=1)

    elapsed_seconds = (punch_out - scheduled_end).total_seconds()

    # Overtime is counted only after at least 30 full minutes.
    if elapsed_seconds < 30 * 60:
        return 0

    return int(elapsed_seconds // 60)




# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
    )
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
    )

class AttendancePatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = Field(
        default=None, ge=100000000000, le=4102444800000
    )
    punch_out: Optional[StrictInt] = Field(
        default=None, ge=100000000000, le=4102444800000
    )
    reason: str = Field(min_length=1, max_length=500)
    by: str = Field(min_length=1, max_length=100)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable"
        )




@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(IST)

    try:
        result = db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department:
        query["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(query)

    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }




@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(
            status_code=422,
            detail="Invalid punch-in status",
        )

    ts = to_utc_datetime(body.punched_at)
    attendance_date = get_attendance_date(ts, emp)

    # Calculate lateness using the local IST instant.
    ist_ts = ts.replace(tzinfo=timezone.utc).astimezone(IST)
    late_minutes = compute_late_minutes(
        ist_ts.replace(tzinfo=None),
        emp["shift_start"],
    )

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this date",
        )

    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = to_utc_datetime(body.punched_at)

    # Find the most recent attendance record punched in by this time.
    
    record = db.attendance_logs.find_one(
    {
        "emp_code": body.emp_code,
        "punch_in": {"$ne": None, "$lte": ts},
    },
    sort=[("punch_in", DESCENDING)],
     )


    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No punch-in found",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record already punched out",
        )

    punch_in_time = record["punch_in"]

    if ts <= punch_in_time:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in",
        )

    if ts - punch_in_time > timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="Punch-out cannot be more than 24 hours after punch-in",
        )

    work_hours = compute_work_hours(punch_in_time, ts)

    # Calculate the scheduled shift end in IST.
    shift_start_minutes = (
        int(emp["shift_start"][:2]) * 60
        + int(emp["shift_start"][3:])
    )
    shift_end_minutes = (
        int(emp["shift_end"][:2]) * 60
        + int(emp["shift_end"][3:])
    )

    attendance_day = date.fromisoformat(record["date"])
    end_day = attendance_day

    # Overnight shifts end on the following calendar day.
    if shift_end_minutes <= shift_start_minutes:
        end_day += timedelta(days=1)

    end_hour, end_minute = map(int, emp["shift_end"].split(":"))
    shift_end_ist = datetime(
        end_day.year,
        end_day.month,
        end_day.day,
        end_hour,
        end_minute,
        tzinfo=IST,
    )
    shift_end_utc = (
        shift_end_ist.astimezone(timezone.utc).replace(tzinfo=None)
    )

    overtime = compute_overtime(
    punch_out=ts.replace(tzinfo=timezone.utc).astimezone(IST),
    shift_end=emp["shift_end"],
    date_str=record["date"],
)



    # Atomically update only if this record is still open.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": max(0, overtime),
                "half_day": work_hours < 4.50,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record already punched out",
        )

    return serialize_attendance(updated)




@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    from datetime import date as date_type

    # Validate dates
    try:
        if date_from:
            date_type.fromisoformat(date_from)
        if date_to:
            date_type.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format"
        )

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be before or equal to date_to"
        )

    # Build MongoDB filter
    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to

    if status:
        allowed_statuses = {
            "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
        }
        if status not in allowed_statuses:
            raise HTTPException(
                status_code=422,
                detail="Invalid attendance status"
            )
        query["status"] = status

    # Count after applying filters
    total = db.attendance_logs.count_documents(query)

    # Sort and paginate in MongoDB
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = []
    for doc in cursor:
        doc["id"] = str(doc.pop("_id"))
        items.append(serialize_attendance(doc))

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def patch_attendance(
    emp_code: str,
    date: str,
    body: AttendancePatchIn
):
    # 1. Check whether employee exists
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    # 2. Find attendance record
    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date
    })
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    
    # Validate requested fields
    updates = body.model_dump(exclude_unset=True)
    updates.pop("reason", None)
    updates.pop("by", None)

    if not updates:
        raise HTTPException(
            status_code=422,
            detail="Provide at least one field to update"
        )

    allowed_statuses = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

    if "status" in updates and updates["status"] not in allowed_statuses:
        raise HTTPException(status_code=422, detail="Invalid attendance status")

    # Convert supplied epoch milliseconds to UTC datetimes
    for field in ("punch_in", "punch_out"):
        if field in updates and updates[field] is not None:
            updates[field] = to_utc_datetime(updates[field])

    # ABSENT and LEAVE records cannot contain punch times
    final_status = updates.get("status", record["status"])
    if final_status in {"ABSENT", "LEAVE"}:
        if updates.get("punch_in", record.get("punch_in")) is not None:
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE records cannot have punch times"
            )
        if updates.get("punch_out", record.get("punch_out")) is not None:
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE records cannot have punch times"
            )

    # A presence status requires punch-in time
    final_punch_in = updates.get("punch_in", record.get("punch_in"))
    final_punch_out = updates.get("punch_out", record.get("punch_out"))

    if final_status in {"PRESENT", "WFH", "ON_DUTY"} and final_punch_in is None:
        raise HTTPException(
            status_code=422,
            detail="Presence status requires punch_in"
        )

    # Validate punch-in belongs to this attendance date in IST
    if final_punch_in is not None:
        local_in = final_punch_in.replace(tzinfo=timezone.utc).astimezone(IST)
        if local_in.date().isoformat() != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must fall on the attendance date"
            )

    # Validate punch-out ordering and maximum duration
    if final_punch_out is not None:
        if final_punch_in is None or final_punch_out <= final_punch_in:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be later than punch_in"
            )
        if final_punch_out - final_punch_in > timedelta(hours=24):
            raise HTTPException(
                status_code=422,
                detail="Punch duration cannot exceed 24 hours"
            )

    
    # Calculate fields that actually changed
    changes = {}

    for field, new_value in updates.items():
        old_value = record.get(field)

        if old_value != new_value:
            changes[field] = {
                "from": old_value,
                "to": new_value
            }

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="No changes detected"
        )

    # Recalculate derived fields when relevant
    final_punch_in = updates.get("punch_in", record.get("punch_in"))
    final_punch_out = updates.get("punch_out", record.get("punch_out"))

    if final_punch_in is None or final_punch_out is None:
        updates["work_hours"] = None
        updates["half_day"] = False
        updates["overtime_minutes"] = 0
    else:
        updates["work_hours"] = compute_work_hours(
            final_punch_in, final_punch_out
        )
        updates["half_day"] = updates["work_hours"] < 4.50

        shift_end_minutes = (
            int(employee["shift_end"][:2]) * 60
            + int(employee["shift_end"][3:])
        )
        shift_start_minutes = (
            int(employee["shift_start"][:2]) * 60
            + int(employee["shift_start"][3:])
        )

        attendance_day = datetime.strptime(date, "%Y-%m-%d").date()
        end_day = attendance_day
        if shift_end_minutes <= shift_start_minutes:
            end_day += timedelta(days=1)

        end_hour, end_minute = map(
            int, employee["shift_end"].split(":")
        )
        shift_end_ist = datetime(
            end_day.year, end_day.month, end_day.day,
            end_hour, end_minute, tzinfo=IST
        )
        shift_end_utc = (
            shift_end_ist.astimezone(timezone.utc).replace(tzinfo=None)
        )

        overtime = int(
            (final_punch_out - shift_end_utc).total_seconds() // 60
        )
        updates["overtime_minutes"] = max(0, overtime) if overtime >= 30 else 0

    # Recalculate late minutes when punch-in changes
    if final_punch_in is not None:
        local_in = final_punch_in.replace(
            tzinfo=timezone.utc
        ).astimezone(IST).replace(tzinfo=None)

        shift_start_hour, shift_start_minute = map(
            int, employee["shift_start"].split(":")
        )
        shift_start = datetime(
            local_in.year, local_in.month, local_in.day,
            shift_start_hour, shift_start_minute
        )
        late = int((local_in - shift_start).total_seconds() // 60)
        updates["late_minutes"] = late if late > 10 else 0
    else:
        updates["late_minutes"] = 0

    # Record the manual correction in history
    history_entry = {
        "at": datetime.now(timezone.utc).replace(tzinfo=None),
        "by": body.by,
        "reason": body.reason,
        "changes": changes
    }

    
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {
                "$eq": [
                    {
                        "$size": {
                            "$ifNull": ["$history", []]
                        }
                    },
                    len(record.get("history", []))
                ]
            }
        },
        {
            "$set": updates,
            "$push": {"history": history_entry}
        },
        return_document=ReturnDocument.AFTER
    )

    if result is None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently; retry the request"
        )

    return serialize_attendance(result)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$")
):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, month_num = map(int, month.split("-"))
    if month_num < 1 or month_num > 12:
        raise HTTPException(status_code=422, detail="Invalid month")

    from calendar import monthrange
    from datetime import date as date_type

    start_date = date_type(year, month_num, 1)
    end_date = date_type(year, month_num, monthrange(year, month_num)[1])

    joined_on = date_type.fromisoformat(employee["joined_on"])
    effective_start = max(start_date, joined_on)

    working_days = 0
    current_day = effective_start
    while current_day <= end_date:
        if current_day.weekday() < 5:
            working_days += 1
        current_day += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": effective_start.isoformat(),
                    "$lte": end_date.isoformat()
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {
                                "$cond": [
                                    "$half_day", 0.5, 1
                                ]
                            },
                            0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                }
            }
        }
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    stats = result[0] if result else {}

    attendance_pct = (
        round(stats.get("present_days", 0) / working_days * 100, 2)
        if working_days else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": stats.get("present_days", 0),
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary_analytics(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$")
):
    year, month_num = map(int, month.split("-"))
    if month_num < 1 or month_num > 12:
        raise HTTPException(status_code=422, detail="Invalid month")

    from calendar import monthrange
    from datetime import date as date_type

    month_start = date_type(year, month_num, 1).isoformat()
    month_end = date_type(
        year, month_num, monthrange(year, month_num)[1]
    ).isoformat()

    pipeline = [
        {
            "$match": {
                "joined_on": {"$lte": month_end}
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$code"]},
                            "date": {"$gte": month_start, "$lte": month_end}
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": ["$half_day", 0.5, 1]},
                                        0
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {"$and": [
                                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$work_hours", None]}
                                        ]},
                                        "$work_hours", 0
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$and": [
                                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$work_hours", None]}
                                        ]},
                                        1, 0
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                            },
                            "total_late_minutes": {"$sum": "$late_minutes"},
                            "leave_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                            },
                            "on_duty_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                            }
                        }
                    }
                ],
                "as": "stats"
            }
        },
        {
            "$unwind": {
                "path": "$stats",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": ["$stats.present_days", 0]}},
                "work_hours_sum": {"$sum": {"$ifNull": ["$stats.work_hours_sum", 0]}},
                "work_hours_count": {"$sum": {"$ifNull": ["$stats.work_hours_count", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$stats.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$stats.total_late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$stats.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$stats.on_duty_count", 0]}}
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$round": [{"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2]},
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": 1}}
    ]

    return list(db.employees.aggregate(pipeline))


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    limit: int = Query(10, ge=1, le=100),
    department: Optional[str] = None
):
    year, month_num = map(int, month.split("-"))
    if month_num < 1 or month_num > 12:
        raise HTTPException(status_code=422, detail="Invalid month")

    from calendar import monthrange
    from datetime import date as date_type

    month_start = date_type(year, month_num, 1).isoformat()
    month_end = date_type(
        year, month_num, monthrange(year, month_num)[1]
    ).isoformat()

    pipeline = [
        {"$match": {
            "date": {"$gte": month_start, "$lte": month_end},
            "late_minutes": {"$gt": 0}
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "employee"
        }},
        {"$unwind": "$employee"}
    ]

    if department:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}}
            }
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "rank": 1
        }}
    ])

    return list(db.attendance_logs.aggregate(pipeline))


@app.get("/analytics/departments/{department}/trend")
def department_attendance_trend(
    department: str,
    date_from: str = Query(...),
    date_to: str = Query(...)
):
    from datetime import date as date_type

    try:
        start = date_type.fromisoformat(date_from)
        end = date_type.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(status_code=422, detail="Dates must use YYYY-MM-DD")

    if end < start:
        raise HTTPException(status_code=422, detail="date_to must be on or after date_from")

    if (end - start).days > 91:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = [
        {"$match": {
            "department": department,
            "joined_on": {"$lte": date_to}
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$emp_code", "$$code"]},
                    "date": {"$gte": date_from, "$lte": date_to}
                }},
                {"$project": {
                    "date": 1, "status": 1, "late_minutes": 1,"half_day": 1,"_id": 0
                }}
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$logs.date",
            "present_count": {
    "$sum": {
        "$cond": [
            {
                "$in": [
                    "$logs.status",
                    ["PRESENT", "WFH", "ON_DUTY"]
                ]
            },
            {
                "$cond": [
                    {"$eq": ["$logs.half_day", True]},
                    0.5,
                    1
                ]
            },
            0
        ]
    }
},
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
            }
        }},
        {"$project": {
            "_id": 0,
            "date": "$_id",
            "present_count": 1,
            "late_count": 1
        }},
        {"$sort": {"date": 1}}
    ]

    daily_stats = {
        item["date"]: item
        for item in db.employees.aggregate(pipeline)
        if item.get("date") is not None
    }

    # Temporary implementation: fill calendar days in Python.
    output = []
    current = start
    while current <= end:
        day = current.isoformat()
        headcount = db.employees.count_documents({
            "department": department,
            "joined_on": {"$lte": day}
        })
        stats = daily_stats.get(day, {})
        present_count = stats.get("present_count", 0)
        late_count = stats.get("late_count", 0)
        working_day = current.weekday() < 5
        rate = round(present_count / headcount, 4) if working_day and headcount else (
            0.0 if working_day else None
        )

        output.append({
            "date": day,
            "is_working_day": working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": rate
        })
        current += timedelta(days=1)

    for i, row in enumerate(output):
        recent_rates = [
            item["attendance_rate"]
            for item in output[max(0, i - 6):i + 1]
            if item["attendance_rate"] is not None
        ]
        row["moving_avg_7d"] = (
            round(sum(recent_rates) / len(recent_rates), 4)
            if recent_rates else None
        )

    return output




@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    month: Optional[str] = None,
    emp_code: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = Query(10, ge=1, le=100)
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend"
    }

    if endpoint not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"endpoint must be one of: {', '.join(sorted(allowed))}"
        )

    if endpoint == "attendance_list":
        query = {}

        if emp_code:
            query["emp_code"] = emp_code

        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to

        return explain_find(
            db.attendance_logs,
            query,
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        )

    if endpoint in {"employee_monthly", "late_leaderboard", "department_summary"}:
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required in YYYY-MM format"
            )

        try:
            year, month_num = map(int, month.split("-"))
            if month_num not in range(1, 13):
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format"
            )

        from calendar import monthrange

        month_start = f"{year:04d}-{month_num:02d}-01"
        month_end = (
            f"{year:04d}-{month_num:02d}-"
            f"{monthrange(year, month_num)[1]:02d}"
        )

        if endpoint == "employee_monthly":
            if not emp_code:
                raise HTTPException(
                    status_code=422,
                    detail="emp_code is required"
                )

            return explain_find(
                db.attendance_logs,
                {
                    "emp_code": emp_code,
                    "date": {"$gte": month_start, "$lte": month_end}
                }
            )

        if endpoint == "late_leaderboard":
            query = {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0}
            }

            return explain_find(db.attendance_logs, query)

        return explain_find(
            db.employees,
            {"joined_on": {"$lte": month_end}}
        )

    # department_trend
    if not department:
        raise HTTPException(
            status_code=422,
            detail="department is required"
        )

    query = {"department": department}

    if date_to:
        query["joined_on"] = {"$lte": date_to}

    return explain_find(db.employees, query)







# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
