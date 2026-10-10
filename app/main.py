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
    unique=True,
    name="uq_attendance_emp_code_date"
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



def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: str | None = None,
) -> int:
    start_h, start_m = map(int, shift_start.split(":"))

    start = punch_in.replace(
        hour=start_h,
        minute=start_m,
        second=0,
        microsecond=0,
    )

    # Only infer a previous-day shift when shift_end is available.
    if shift_end is not None:
        end_h, end_m = map(int, shift_end.split(":"))
        overnight = (end_h, end_m) <= (start_h, start_m)

        if overnight and punch_in.time() < start.time():
            start -= timedelta(days=1)

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
import re
from datetime import date
from pydantic import BaseModel, Field, ConfigDict, field_validator


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254)
    department: str = Field(min_length=1, max_length=100)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value):
        if not re.fullmatch(r"EMP\d{4,6}", value):
            raise ValueError("emp_code must be EMP followed by 4 to 6 digits")
        return value

    @field_validator("name", "department")
    @classmethod
    def validate_text(cls, value):
        if not value.strip():
            raise ValueError("Field cannot be empty")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Invalid email format")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Shift time must use HH:MM format")
        return value

    @field_validator("shift_end")
    @classmethod
    def validate_shift_times_differ(cls, value, info):
        if info.data.get("shift_start") == value:
            raise ValueError("shift_start and shift_end must differ")
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must use YYYY-MM-DD format")

        if parsed.isoformat() != value:
            raise ValueError("joined_on must use YYYY-MM-DD format")

        return value


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
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


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
        emp["shift_end"],
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
       doc.pop("_id", None)
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
    # 1. Validate the attendance date
    try:
        attendance_day = datetime.strptime(date, "%Y-%m-%d").date()
        if attendance_day.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date must use YYYY-MM-DD format"
        )

    # 2. Check employee
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    # 3. Find attendance record
    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date
    })
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    # 4. Read requested changes
    updates = body.model_dump(exclude_unset=True)
    updates.pop("reason", None)
    updates.pop("regularized_by", None)

    if not updates:
        raise HTTPException(
            status_code=422,
            detail="Provide at least one field to update"
        )

    # 5. Convert supplied epoch milliseconds to UTC datetimes
    for field in ("punch_in", "punch_out"):
        if field in updates and updates[field] is not None:
            updates[field] = to_utc_datetime(updates[field])

    # 6. Determine final values
    final_status = updates.get("status", record["status"])
    allowed_statuses = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

    if final_status not in allowed_statuses:
        raise HTTPException(status_code=422, detail="Invalid attendance status")

    # ABSENT/LEAVE records cannot retain punch times.
    # Clear existing times automatically when switching to these statuses.
    if final_status in {"ABSENT", "LEAVE"}:
        updates["punch_in"] = None
        updates["punch_out"] = None

    final_punch_in = updates.get("punch_in", record.get("punch_in"))
    final_punch_out = updates.get("punch_out", record.get("punch_out"))

    # 7. Presence statuses require punch-in
    if final_status in {"PRESENT", "WFH", "ON_DUTY"} and final_punch_in is None:
        raise HTTPException(
            status_code=422,
            detail="Presence status requires punch_in"
        )

    # 8. Validate punch-in attendance date.
    # Overnight shifts can have a punch-in after midnight that belongs
    # to the previous day's attendance record.
    if final_punch_in is not None:
        local_in = (
            final_punch_in
            .replace(tzinfo=timezone.utc)
            .astimezone(IST)
        )

        shift_start_minutes = (
            int(employee["shift_start"][:2]) * 60
            + int(employee["shift_start"][3:])
        )
        shift_end_minutes = (
            int(employee["shift_end"][:2]) * 60
            + int(employee["shift_end"][3:])
        )
        punch_minutes = local_in.hour * 60 + local_in.minute

        expected_date = local_in.date()

        if (
            shift_end_minutes <= shift_start_minutes
            and punch_minutes < shift_end_minutes
        ):
            expected_date -= timedelta(days=1)

        if expected_date.isoformat() != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the attendance date"
            )

    # 9. Validate punch-out ordering and duration
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

    # 10. Recalculate derived fields
    if final_status in {"ABSENT", "LEAVE"}:
        updates["work_hours"] = None
        updates["half_day"] = False
        updates["late_minutes"] = 0
        updates["overtime_minutes"] = 0

    else:
        if final_punch_in is None:
            updates["work_hours"] = None
            updates["half_day"] = False
            updates["late_minutes"] = 0
            updates["overtime_minutes"] = 0

        else:
            local_in = (
                final_punch_in
                .replace(tzinfo=timezone.utc)
                .astimezone(IST)
                .replace(tzinfo=None)
            )

            updates["late_minutes"] = compute_late_minutes(
                local_in,
                employee["shift_start"]
            )

            if final_punch_out is None:
                updates["work_hours"] = None
                updates["half_day"] = False
                updates["overtime_minutes"] = 0

            else:
                updates["work_hours"] = compute_work_hours(
                    final_punch_in,
                    final_punch_out
                )
                updates["half_day"] = updates["work_hours"] < 4.50

                shift_start_minutes = (
                    int(employee["shift_start"][:2]) * 60
                    + int(employee["shift_start"][3:])
                )
                shift_end_minutes = (
                    int(employee["shift_end"][:2]) * 60
                    + int(employee["shift_end"][3:])
                )

                shift_end_day = attendance_day
                if shift_end_minutes <= shift_start_minutes:
                    shift_end_day += timedelta(days=1)

                end_hour, end_minute = map(
                    int, employee["shift_end"].split(":")
                )
                shift_end_ist = datetime(
                    shift_end_day.year,
                    shift_end_day.month,
                    shift_end_day.day,
                    end_hour,
                    end_minute,
                    tzinfo=IST
                )
                shift_end_utc = (
                    shift_end_ist
                    .astimezone(timezone.utc)
                    .replace(tzinfo=None)
                )

                elapsed_seconds = (
                    final_punch_out - shift_end_utc
                ).total_seconds()

                updates["overtime_minutes"] = (
                    int(elapsed_seconds // 60)
                    if elapsed_seconds >= 30 * 60
                    else 0
                )

    # 11. Record actual changes, including derived fields.
    # Compare final calculated values with the stored document.
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

    # 12. Append one audit entry for this successful correction
    history_entry = {
        "at": datetime.now(timezone.utc).replace(tzinfo=None),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    # 13. Atomic update to prevent concurrent corrections overwriting history
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {
                "$eq": [
                    {"$size": {"$ifNull": ["$history", []]}},
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
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    year, month_num = map(int, month.split("-"))

    from calendar import monthrange
    from datetime import date as date_type

    start_date = date_type(year, month_num, 1)
    end_date = date_type(
        year,
        month_num,
        monthrange(year, month_num)[1]
    )

    joined_on = date_type.fromisoformat(employee["joined_on"])
    effective_start = max(start_date, joined_on)

    # Count weekdays from the employee's joining date.
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
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"]
                                        ]
                                    },
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date",
                                                        "format": "%Y-%m-%d"
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6]
                                        ]
                                    }
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": "$late_minutes"
                },
                "total_overtime_minutes": {
                    "$sum": "$overtime_minutes"
                }
            }
        }
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    stats = result[0] if result else {}

    present_days = stats.get("present_days", 0)

    # Percentage rounded to two decimal places.
    attendance_pct = (
        float(
            (Decimal(str(present_days)) * Decimal("100") /
             Decimal(working_days)).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP
            )
        )
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct
    }
@app.get("/analytics/departments/summary")
def department_summary_analytics(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None
):
    year, month_num = map(int, month.split("-"))

    from calendar import monthrange
    from datetime import date as date_type

    month_start_date = date_type(year, month_num, 1)
    month_end_date = date_type(
        year, month_num, monthrange(year, month_num)[1]
    )

    month_start = month_start_date.isoformat()
    month_end = month_end_date.isoformat()

    employee_match = {"joined_on": {"$lte": month_end}}
    if department:
        employee_match["department"] = department

    pipeline = [
        {
            "$match": employee_match
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
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"]
                                                    ]
                                                },
                                                {
                                                    "$in": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$date",
                                                                    "format": "%Y-%m-%d"
                                                                }
                                                            }
                                                        },
                                                        [2, 3, 4, 5, 6]
                                                    ]
                                                }
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1
                                            ]
                                        },
                                        0
                                    ]
                                }
                            },

                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"]
                                                    ]
                                                },
                                                {"$ne": ["$work_hours", None]}
                                            ]
                                        },
                                        "$work_hours",
                                        0
                                    ]
                                }
                            },

                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"]
                                                    ]
                                                },
                                                {"$ne": ["$work_hours", None]}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },

                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": ["$late_minutes", 0]},
                                        1,
                                        0
                                    ]
                                }
                            },

                            "total_late_minutes": {
                                "$sum": "$late_minutes"
                            },

                            "leave_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "LEAVE"]},
                                        1,
                                        0
                                    ]
                                }
                            },

                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "ON_DUTY"]},
                                        1,
                                        0
                                    ]
                                }
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
                "present_days": {
                    "$sum": {"$ifNull": ["$stats.present_days", 0]}
                },
                "work_hours_sum": {
                    "$sum": {"$ifNull": ["$stats.work_hours_sum", 0]}
                },
                "work_hours_count": {
                    "$sum": {"$ifNull": ["$stats.work_hours_count", 0]}
                },
                "late_count": {
                    "$sum": {"$ifNull": ["$stats.late_count", 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$stats.total_late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$ifNull": ["$stats.leave_count", 0]}
                },
                "on_duty_count": {
                    "$sum": {"$ifNull": ["$stats.on_duty_count", 0]}
                }
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
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            "$work_hours_sum",
                                                            "$work_hours_count"
                                                        ]
                                                    },
                                                    100
                                                ]
                                            },
                                            0.5
                                        ]
                                    }
                                },
                                100
                            ]
                        },
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

    return {
        "month": month,
        "items": list(db.employees.aggregate(pipeline))
    }

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    year, month_num = map(int, month.split("-"))

    from calendar import monthrange
    from datetime import date as date_type

    month_start = date_type(year, month_num, 1).isoformat()
    month_end = date_type(
        year, month_num, monthrange(year, month_num)[1]
    ).isoformat()

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },
        {"$unwind": "$employee"}
    ]

    if department:
        pipeline.append({
            "$match": {
                "employee.department": department
            }
        })

    pipeline.extend([
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {
                    "total_late_minutes": -1
                },
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
                "rank": 1
            }
        }
    ])

    return {
        "month": month,
        "items": list(db.attendance_logs.aggregate(pipeline))
    }


@app.get("/analytics/departments/{department}/trend")
def department_attendance_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to")
):
    try:
        start = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD"
        )

    if end < start:
        raise HTTPException(
            status_code=422,
            detail="date_to must be on or after date_from"
        )

    if (end - start).days > 91:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days"
        )

    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    start_dt = datetime(start.year, start.month, start.day)

    pipeline = [
        {"$documents": [{}]},
        {"$project": {
            "days": {
                "$map": {
                    "input": {"$range": [0, (end - start).days + 1]},
                    "as": "offset",
                    "in": {
                        "$dateAdd": {
                            "startDate": start_dt,
                            "unit": "day",
                            "amount": "$$offset"
                        }
                    }
                }
            }
        }},
        {"$unwind": "$days"},
        {"$set": {
            "day_string": {
                "$dateToString": {
                    "format": "%Y-%m-%d",
                    "date": "$days",
                    "timezone": "UTC"
                }
            },
            "weekday": {"$dayOfWeek": "$days"}
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$day_string"},
            "pipeline": [
                {"$match": {
                    "department": department,
                    "$expr": {"$lte": ["$joined_on", "$$day"]}
                }},
                {"$project": {"_id": 0, "emp_code": 1}}
            ],
            "as": "employees"
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {
                "day": "$day_string",
                "codes": "$employees.emp_code"
            },
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$date", "$$day"]},
                            {"$in": ["$emp_code", "$$codes"]}
                        ]
                    }
                }},
                {"$project": {
                    "_id": 0,
                    "status": 1,
                    "half_day": 1,
                    "late_minutes": 1
                }}
            ],
            "as": "logs"
        }},
        {"$set": {
            "headcount": {"$size": "$employees"},
            "present_count": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "log",
                        "in": {
                            "$cond": [
                                {"$in": [
                                    "$$log.status",
                                    ["PRESENT", "WFH", "ON_DUTY"]
                                ]},
                                {"$cond": [
                                    {"$eq": ["$$log.half_day", True]},
                                    0.5,
                                    1
                                ]},
                                0
                            ]
                        }
                    }
                }
            },
            "late_count": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "log",
                        "in": {
                            "$cond": [
                                {"$gt": ["$$log.late_minutes", 0]},
                                1,
                                0
                            ]
                        }
                    }
                }
            },
            "is_working_day": {
                "$and": [
                    {"$gte": ["$weekday", 2]},
                    {"$lte": ["$weekday", 6]}
                ]
            }
        }},
        {"$set": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {
                        "$divide": [
                            {
                                "$floor": {
                                    "$add": [
                                        {
                                            "$multiply": [
                                                {"$divide": [
                                                    "$present_count",
                                                    "$headcount"
                                                ]},
                                                10000
                                            ]
                                        },
                                        0.5
                                    ]
                                }
                            },
                            10000
                        ]
                    },
                    None
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"days": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]}
                }
            }
        }},
        {"$set": {
            "moving_avg_7d": {
                "$cond": [
                    {"$ne": ["$moving_avg_7d", None]},
                    {
                        "$divide": [
                            {
                                "$floor": {
                                    "$add": [
                                        {"$multiply": ["$moving_avg_7d", 10000]},
                                        0.5
                                    ]
                                }
                            },
                            10000
                        ]
                    },
                    None
                ]
            }
        }},
        {"$project": {
            "_id": 0,
            "date": "$day_string",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1
        }},
        {"$sort": {"date": 1}}
    ]

    items = list(db.aggregate(pipeline))

    return {
        "department": department,
        "items": items
    }



@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    month: Optional[str] = None,
    emp_code: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    limit: int = Query(10, ge=1, le=50)
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

    def run_find_explain(collection, query, sort=None, skip=0, limit_value=0):
        command = {
            "find": collection.name,
            "filter": query,
            "skip": skip
        }

        if sort:
            command["sort"] = dict(sort)

        if limit_value:
            command["limit"] = limit_value

        result = db.command(
            "explain",
            command,
            verbosity="executionStats"
        )

        return json.loads(json_util.dumps(result))

    def run_aggregate_explain(collection_name, pipeline):
        result = db.command(
            "explain",
            {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )

        return json.loads(json_util.dumps(result))

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

        if date_from:
            try:
                date.fromisoformat(date_from)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail="from must use YYYY-MM-DD format"
                )

        if date_to:
            try:
                date.fromisoformat(date_to)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail="to must use YYYY-MM-DD format"
                )

        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=422,
                detail="from must be before or equal to to"
            )

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

        explain = run_find_explain(
            db.attendance_logs,
            query,
            sort=[("date", DESCENDING), ("emp_code", ASCENDING)],
            skip=(page - 1) * page_size,
            limit_value=page_size
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain
        }

    if endpoint in {
        "employee_monthly",
        "department_summary",
        "late_leaderboard"
    }:
        if not month or not re.fullmatch(
            r"\d{4}-(0[1-9]|1[0-2])",
            month
        ):
            raise HTTPException(
                status_code=422,
                detail="month is required in YYYY-MM format"
            )

        year, month_num = map(int, month.split("-"))

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

            pipeline = [
                {
                    "$match": {
                        "emp_code": emp_code,
                        "date": {
                            "$gte": month_start,
                            "$lte": month_end
                        }
                    }
                },
                {
                    "$group": {
                        "_id": "$emp_code",
                        "records": {"$sum": 1}
                    }
                }
            ]

            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": run_aggregate_explain(
                    "attendance_logs", pipeline
                )
            }

        if endpoint == "late_leaderboard":
            pipeline = [
                {
                    "$match": {
                        "date": {
                            "$gte": month_start,
                            "$lte": month_end
                        },
                        "late_minutes": {"$gt": 0}
                    }
                },
                {
                    "$group": {
                        "_id": "$emp_code",
                        "total_late_minutes": {"$sum": "$late_minutes"},
                        "late_count": {"$sum": 1}
                    }
                },
                {
                    "$lookup": {
                        "from": "employees",
                        "localField": "_id",
                        "foreignField": "emp_code",
                        "as": "employee"
                    }
                },
                {"$unwind": "$employee"}
            ]

            if department:
                pipeline.append({
                    "$match": {
                        "employee.department": department
                    }
                })

            pipeline.extend([
                {"$sort": {"total_late_minutes": -1, "_id": 1}},
                {
                    "$setWindowFields": {
                        "sortBy": {"total_late_minutes": -1},
                        "output": {"rank": {"$rank": {}}}
                    }
                },
                {"$match": {"rank": {"$lte": limit}}}
            ])

            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": run_aggregate_explain(
                    "attendance_logs", pipeline
                )
            }

        employee_match = {"joined_on": {"$lte": month_end}}

        if department:
            employee_match["department"] = department

        pipeline = [
            {"$match": employee_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {"$eq": ["$emp_code", "$$code"]},
                                "date": {
                                    "$gte": month_start,
                                    "$lte": month_end
                                }
                            }
                        }
                    ],
                    "as": "attendance"
                }
            }
        ]

        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": run_aggregate_explain("employees", pipeline)
        }

    # Department trend
    if not department:
        raise HTTPException(
            status_code=422,
            detail="department is required"
        )

    if not date_from or not date_to:
        raise HTTPException(
            status_code=422,
            detail="from and to are required"
        )

    try:
        start = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format"
        )

    if end < start or (end - start).days > 91:
        raise HTTPException(
            status_code=422,
            detail="Invalid date range; maximum range is 92 days"
        )

    if not db.employees.find_one({"department": department}):
        raise HTTPException(
            status_code=404,
            detail="Department not found"
        )

    pipeline = [
        {"$documents": [{}]},
        {
            "$project": {
                "days": {
                    "$map": {
                        "input": {"$range": [0, (end - start).days + 1]},
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": datetime(
                                    start.year, start.month, start.day
                                ),
                                "unit": "day",
                                "amount": "$$offset"
                            }
                        }
                    }
                }
            }
        },
        {"$unwind": "$days"},
        {
            "$set": {
                "day_string": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$days",
                        "timezone": "UTC"
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$day_string"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {"$lte": ["$joined_on", "$$day"]}
                        }
                    }
                ],
                "as": "employees"
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "day": "$day_string",
                    "codes": "$employees.emp_code"
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {"$in": ["$emp_code", "$$codes"]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        }
    ]

    return {
        "endpoint": endpoint,
        "collection": "employees",
        "explain": run_aggregate_explain("1", pipeline)
    }






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
