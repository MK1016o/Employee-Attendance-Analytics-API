"""
Employee Attendance & Analytics API
Run: uvicorn app.main:app --port 8000
"""

import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def init_db_indexes():
    """Creates required indexes idempotently at startup."""
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("date", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R10)
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    if val is None:
        return None
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    return float(Decimal(str(val)).quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def trunc_sec(dt: datetime) -> datetime:
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC).replace(microsecond=0)


def ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    return datetime.fromtimestamp(ms // 1000, tz=UTC) if ms is not None else None


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    return int((dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()) * 1000 if dt else None


def is_overnight_shift(sh: str, eh: str) -> bool:
    return tuple(map(int, eh.split(":"))) <= tuple(map(int, sh.split(":")))


def compute_attendance_date(p_in: datetime, sh: str, eh: str) -> str:
    ist_dt = p_in.astimezone(IST)
    if is_overnight_shift(sh, eh):
        eh_h, eh_m = map(int, eh.split(":"))
        if ist_dt.time() < time(eh_h, eh_m):
            return (ist_dt.date() - timedelta(days=1)).isoformat()
    return ist_dt.date().isoformat()


def get_shift_instants(d_str: str, sh: str, eh: str) -> Tuple[datetime, datetime]:
    d = date.fromisoformat(d_str)
    sh_h, sh_m = map(int, sh.split(":"))
    eh_h, eh_m = map(int, eh.split(":"))
    start_utc = datetime(d.year, d.month, d.day, sh_h, sh_m, tzinfo=IST).astimezone(UTC)
    end_date = d + timedelta(days=1) if is_overnight_shift(sh, eh) else d
    end_utc = datetime(end_date.year, end_date.month, end_date.day, eh_h, eh_m, tzinfo=IST).astimezone(UTC)
    return start_utc, end_utc


def compute_late_minutes(p_in: datetime, s_start: datetime) -> int:
    diff = (trunc_sec(p_in) - trunc_sec(s_start)).total_seconds()
    return int(diff // 60) if diff > 600 else 0


def compute_work_hours(p_in: datetime, p_out: datetime) -> Optional[float]:
    diff = max(0.0, (trunc_sec(p_out) - trunc_sec(p_in)).total_seconds())
    return float((Decimal(str(diff)) / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(p_out: datetime, s_end: datetime) -> int:
    diff = (trunc_sec(p_out) - trunc_sec(s_end)).total_seconds()
    return int(diff // 60) if diff >= 1800 else 0


def count_working_days(month_str: str, joined_on_str: str) -> int:
    y, m = map(int, month_str.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    cur = max(date(y, m, 1), date.fromisoformat(joined_on_str))
    end = date(y, m, num_days)
    count = 0
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Models & Serialization
# --------------------------------------------------------------------------- #
EMP_REGEX = r"^EMP\d{4,6}$"
HH_MM_REGEX = r"^([01]\d|2[0-3]):[0-5]\d$"
MONTH_REGEX = r"^\d{4}-(0[1-9]|1[0-2])$"

StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=EMP_REGEX)
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HH_MM_REGEX)
    shift_end: str = Field(default="18:30", pattern=HH_MM_REGEX)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined(cls, v: str) -> str:
        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: PresenceStatusType = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[StatusType] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


def serialize_emp(doc: Dict[str, Any]) -> Dict[str, Any]:
    c_at = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"), "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(c_at) if isinstance(c_at, datetime) else int(c_at or 0),
    }


def serialize_att(doc: Dict[str, Any]) -> Dict[str, Any]:
    history = []
    for h in doc.get("history", []):
        at_val = h.get("at")
        at_ms = dt_to_ms(at_val) if isinstance(at_val, datetime) else int(at_val or 0)
        changes = {}
        for k, v in h.get("changes", {}).items():
            if isinstance(v, dict):
                changes[k] = {
                    "from": dt_to_ms(v["from"]) if isinstance(v.get("from"), datetime) else v.get("from"),
                    "to": dt_to_ms(v["to"]) if isinstance(v.get("to"), datetime) else v.get("to"),
                }
            else:
                changes[k] = v
        history.append({**h, "at": at_ms, "changes": changes})

    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")), "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0), "half_day": doc.get("half_day", False),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    doc = {**body.model_dump(), "created_at": trunc_sec(datetime.now(UTC))}
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_emp(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_emp(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    p_in_utc = ms_to_dt(body.punched_at) if body.punched_at else trunc_sec(datetime.now(UTC))
    sh, eh = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    att_date = compute_attendance_date(p_in_utc, sh, eh)
    s_start, _ = get_shift_instants(att_date, sh, eh)

    doc = {
        "emp_code": body.emp_code, "date": att_date, "status": body.status,
        "punch_in": p_in_utc, "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(p_in_utc, s_start), "overtime_minutes": 0,
        "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return serialize_att(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    p_out_utc = ms_to_dt(body.punched_at) if body.punched_at else trunc_sec(datetime.now(UTC))
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": p_out_utc}},
        sort=[("punch_in", -1)],
    )
    if not rec:
        raise HTTPException(status_code=404, detail="No punch-in found")

    p_in_utc = rec["punch_in"]
    if p_out_utc <= p_in_utc:
        raise HTTPException(status_code=422, detail="punched_at must be strictly greater than punch_in")
    if (p_out_utc - p_in_utc).total_seconds() > 86400:
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")
    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    sh, eh = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    _, s_end = get_shift_instants(rec["date"], sh, eh)
    work_hrs = compute_work_hours(p_in_utc, p_out_utc)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": p_out_utc, "work_hours": work_hrs,
            "overtime_minutes": compute_overtime(p_out_utc, s_end),
            "half_day": bool(work_hrs < 4.50),
        }},
        return_document=True,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Record is already punched out")
    return serialize_att(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status_val: Optional[StatusType] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q: Dict[str, Any] = {}
    if emp_code: q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    if status_val: q["status"] = status_val

    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_att(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date_param: str = Path(..., alias="date"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(status_code=404, detail="Employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_param})
    if not rec: raise HTTPException(status_code=404, detail="Attendance record not found")

    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(status_code=422, detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE")

    target_status = body.status if body.status is not None else rec["status"]
    sh, eh = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")

    if target_status in ("ABSENT", "LEAVE"):
        new_in, new_out, new_work, new_late, new_ot, new_half = None, None, None, 0, 0, False
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        if new_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")
        if compute_attendance_date(new_in, sh, eh) != date_param:
            raise HTTPException(status_code=422, detail="punch_in does not belong to record's attendance date")

        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_out:
            if new_out <= new_in:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if (new_out - new_in).total_seconds() > 86400:
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

        s_start, s_end = get_shift_instants(date_param, sh, eh)
        new_late = compute_late_minutes(new_in, s_start)
        if new_out:
            new_work = compute_work_hours(new_in, new_out)
            new_ot = compute_overtime(new_out, s_end)
            new_half = bool(new_work < 4.50)
        else:
            new_work, new_ot, new_half = None, 0, False

    changes = {}
    for k, old_v, new_v in [
        ("status", rec.get("status"), target_status),
        ("punch_in", rec.get("punch_in"), new_in),
        ("punch_out", rec.get("punch_out"), new_out),
        ("work_hours", rec.get("work_hours"), new_work),
        ("late_minutes", rec.get("late_minutes", 0), new_late),
        ("overtime_minutes", rec.get("overtime_minutes", 0), new_ot),
        ("half_day", rec.get("half_day", False), new_half),
    ]:
        if old_v != new_v:
            changes[k] = {"from": old_v, "to": new_v}

    if not changes:
        raise HTTPException(status_code=422, detail="A request that changes nothing is 422")

    entry = {"at": trunc_sec(datetime.now(UTC)), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "history": {"$size": len(rec.get("history", []))}},
        {"$set": {
            "status": target_status, "punch_in": new_in, "punch_out": new_out,
            "work_hours": new_work, "late_minutes": new_late, "overtime_minutes": new_ot, "half_day": new_half,
        }, "$push": {"history": entry}},
        return_document=True,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent modification detected on attendance history")
    return serialize_att(updated)


# --------------------------------------------------------------------------- #
# Pipelines & Analytics
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    last_d = calendar.monthrange(y, m)[1]
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": f"{month}-{last_d:02d}"}}},
        {"$addFields": {"dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$in": ["$dow", [2,3,4,5,6]]}]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]}, 0.0
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }}
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str = Path(...), month: str = Query(..., pattern=MONTH_REGEX)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(status_code=404, detail="Employee not found")

    w_days = count_working_days(month, emp.get("joined_on", "2000-01-01"))
    res = list(db.attendance_logs.aggregate(build_employee_monthly_pipeline(emp_code, month)))
    agg = res[0] if res else {}
    p_days = float(agg.get("present_days", 0.0))

    return {
        "emp_code": emp_code, "month": month, "working_days": w_days,
        "present_days": p_days, "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)), "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": round_half_up((p_days / w_days) * 100, 2) if w_days > 0 else None,
    }


def build_department_summary_pipeline(month: str, dept: Optional[str] = None) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    last_d = calendar.monthrange(y, m)[1]
    match_f: Dict[str, Any] = {"joined_on": {"$lte": f"{month}-{last_d:02d}"}}
    if dept: match_f["department"] = dept

    return [
        {"$match": match_f},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$emp"]},
                    {"$gte": ["$date", f"{month}-01"]},
                    {"$lte": ["$date", f"{month}-{last_d:02d}"]}
                ]}}},
                {"$addFields": {"dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}}
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": {"dept": "$department", "emp": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$in": ["$logs.dow", [2,3,4,5,6]]}]},
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1.0]}, 0.0
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]},
                "$logs.work_hours", 0.0
            ]}},
            "work_hours_cnt": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]},
                1, 0
            ]}},
        }},
        {"$group": {
            "_id": "$_id.dept",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "work_hours_sum": {"$sum": "$work_hours_sum"},
            "work_hours_count": {"$sum": "$work_hours_cnt"},
        }},
        {"$sort": {"_id": 1}}
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(month: str = Query(..., pattern=MONTH_REGEX), department: Optional[str] = Query(None)):
    results = list(db.employees.aggregate(build_department_summary_pipeline(month, department)))
    items = []
    for r in results:
        cnt = int(r["work_hours_count"])
        avg_w = round_half_up(float(r["work_hours_sum"]) / cnt, 2) if cnt > 0 else None
        items.append({
            "department": r["_id"], "headcount": int(r["headcount"]), "present_days": float(r["present_days"]),
            "avg_work_hours": avg_w, "late_count": int(r["late_count"]), "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]), "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month: str, limit: int, dept: Optional[str] = None) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    last_d = calendar.monthrange(y, m)[1]
    pipeline: List[Dict[str, Any]] = [
        {"$match": {"date": {"$gte": f"{month}-01", "$lte": f"{month}-{last_d:02d}"}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if dept: pipeline.append({"$match": {"emp.department": dept}})
    pipeline.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": "$rank", "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department", "total_late_minutes": "$total_late_minutes", "late_count": "$late_count"}},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_REGEX),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    return {"month": month, "items": list(db.attendance_logs.aggregate(build_late_leaderboard_pipeline(month, limit, department)))}


def build_department_trend_pipeline(dept: str, from_str: str, to_str: str) -> List[Dict[str, Any]]:
    from_d = date.fromisoformat(from_str)
    to_d = date.fromisoformat(to_str)
    from_dt = datetime(from_d.year, from_d.month, from_d.day, tzinfo=UTC)
    to_dt_next = datetime(to_d.year, to_d.month, to_d.day, tzinfo=UTC) + timedelta(days=1)

    return [
        {"$match": {"date": {"$gte": from_str, "$lte": to_str}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": dept}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]}, 0.0
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "date_dt": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "date_dt", "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt_next]}}},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
            "present_count": {"$ifNull": ["$present_count", 0.0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "day_of_week": {"$dayOfWeek": "$date_dt"},
        }},
        {"$addFields": {"is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}}},
        {"$lookup": {
            "from": "employees", "let": {"cur_date": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$department", dept]}, {"$lte": ["$joined_on", "$$cur_date"]}]}}},
                {"$count": "count"},
            ],
            "as": "hc",
        }},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]}}},
        {"$sort": {"date": 1}},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": [{"$eq": ["$is_working_day", True]}, {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None
        ]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"raw_moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, "current"]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "raw_moving_avg_7d": 1}},
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(department: str = Path(...), from_p: str = Query(..., alias="from"), to_p: str = Query(..., alias="to")):
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    try:
        from_d, to_d = date.fromisoformat(from_p), date.fromisoformat(to_p)
    except ValueError:
        raise HTTPException(status_code=422, detail="Dates must be in YYYY-MM-DD format")

    if to_d < from_d: raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
    if (to_d - from_d).days + 1 > 92: raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

    docs = list(db.attendance_logs.aggregate(build_department_trend_pipeline(department, from_p, to_p)))
    items = [{
        "date": d["date"], "is_working_day": d["is_working_day"], "headcount": int(d["headcount"]),
        "present_count": float(d["present_count"]), "late_count": int(d["late_count"]),
        "attendance_rate": round_half_up(d.get("attendance_rate"), 4),
        "moving_avg_7d": round_half_up(d.get("raw_moving_avg_7d"), 4),
    } for d in docs]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
def sanitize_bson(obj: Any) -> Any:
    if isinstance(obj, dict): return {k: sanitize_bson(v) for k, v in obj.items()}
    if isinstance(obj, list): return [sanitize_bson(x) for x in obj]
    if isinstance(obj, datetime): return obj.isoformat()
    if hasattr(obj, "__str__") and type(obj).__name__ == "ObjectId": return str(obj)
    return obj


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern="^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=MONTH_REGEX),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status_val: Optional[StatusType] = Query(None, alias="status"),
    from_p: Optional[str] = Query(None, alias="from"),
    to_p: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        q: Dict[str, Any] = {}
        if emp_code: q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        if status_val: q["status"] = status_val
        cmd = {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}
        coll = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(status_code=422, detail="emp_code and month are required")
        cmd = {"aggregate": "attendance_logs", "pipeline": build_employee_monthly_pipeline(emp_code, month), "cursor": {}}
        coll = "attendance_logs"
    elif endpoint == "department_summary":
        if not month: raise HTTPException(status_code=422, detail="month is required")
        cmd = {"aggregate": "employees", "pipeline": build_department_summary_pipeline(month, department), "cursor": {}}
        coll = "employees"
    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(status_code=422, detail="month is required")
        cmd = {"aggregate": "attendance_logs", "pipeline": build_late_leaderboard_pipeline(month, limit, department), "cursor": {}}
        coll = "attendance_logs"
    elif endpoint == "department_trend":
        if not department or not from_p or not to_p: raise HTTPException(status_code=422, detail="department, from, and to are required")
        try:
            if date.fromisoformat(to_p) < date.fromisoformat(from_p) or (date.fromisoformat(to_p) - date.fromisoformat(from_p)).days + 1 > 92:
                raise HTTPException(status_code=422, detail="Invalid date range")
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format")
        cmd = {"aggregate": "attendance_logs", "pipeline": build_department_trend_pipeline(department, from_p, to_p), "cursor": {}}
        coll = "attendance_logs"
    else:
        raise HTTPException(status_code=404, detail="Unknown endpoint")

    exp = db.command("explain", cmd, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": coll, "explain": sanitize_bson(exp)}
