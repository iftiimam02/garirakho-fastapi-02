import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Any

from fastapi import FastAPI, Request, Form, Depends, HTTPException, Query, Body
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import text, and_
from pydantic import BaseModel

from .db import Base, engine, SessionLocal
from .models import User, Device, Booking
from .auth import hash_password, verify_password, make_session, read_session
from .iot import send_c2d

# ------------------ CONFIG ------------------
INGEST_API_KEY = os.getenv("INGEST_API_KEY", "devkey")
DEVICE_ID = os.getenv("SINGLE_DEVICE_ID", "DEV001").strip() or "DEV001"

BOOKING_TTL_MINUTES = int(os.getenv("BOOKING_TTL_MINUTES", "30"))  # ✅ change here if needed

app = FastAPI(title="Garirakho")
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

@app.get("/api/version")
def version():
    return {"version": "v3-booking-2026-02-08", "deviceId": DEVICE_ID, "bookingTTLMin": BOOKING_TTL_MINUTES}

# -------------------- DATABASE --------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.on_event("startup")
def startup_db():
    Base.metadata.create_all(bind=engine)
    print("✅ DB OK: tables ensured")

@app.get("/api/db-check")
def db_check(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"db": "ok"}

# -------------------- AUTH HELPERS --------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def get_current_user(request: Request, db: Session) -> Optional[User]:
    uid = read_session(request)
    if not uid:
        return None
    return db.query(User).filter(User.id == uid).first()

def require_login(request: Request, db: Session) -> User:
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")
    return user

def require_admin(request: Request, db: Session) -> User:
    user = require_login(request, db)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user

def require_approved_user(request: Request, db: Session) -> User:
    user = require_login(request, db)
    if user.role != "admin" and user.status != "approved":
        raise HTTPException(status_code=403, detail="User not approved by admin yet")
    return user

# -------------------- EXPIRY (lazy) --------------------
def expire_old_bookings(db: Session):
    # Expire any pending/approved bookings past expires_at
    now = now_utc()
    q = db.query(Booking).filter(
        Booking.status.in_(["pending", "approved"]),
        Booking.expires_at.isnot(None),
        Booking.expires_at < now
    )
    changed = False
    for b in q.all():
        b.status = "expired"
        b.finished_at = now
        changed = True
    if changed:
        db.commit()

# -------------------- SLOT HELPERS --------------------
def ensure_device_row(db: Session) -> Device:
    d = db.query(Device).filter(Device.device_id == DEVICE_ID).first()
    if not d:
        d = Device(device_id=DEVICE_ID, slots=default_slots_4())
        db.add(d)
        db.commit()
        db.refresh(d)
    return d

def default_slots_4():
    return [{"id": 1, "occupied": False}, {"id": 2, "occupied": False}, {"id": 3, "occupied": False}, {"id": 4, "occupied": False}]

def normalize_slots(payload_slots: Any) -> list[dict]:
    """
    Accept BOTH:
    1) array: [{"id":1,"occupied":true}, ...]
    2) object: {"available":8,"occupied":2} (old format) -> convert to 4 slots
    """
    if isinstance(payload_slots, list):
        out = []
        for i, s in enumerate(payload_slots):
            sid = int(s.get("id", i + 1))
            out.append({"id": sid, "occupied": bool(s.get("occupied", False))})
        # ensure 4 slots minimum (your system)
        out = [x for x in out if 1 <= int(x.get("id", 0)) <= 4]
        if len(out) < 4:
            existing = {x["id"] for x in out}
            for sid in range(1, 5):
                if sid not in existing:
                    out.append({"id": sid, "occupied": False})
        out.sort(key=lambda x: x["id"])
        return out

    if isinstance(payload_slots, dict):
        # old demo format -> convert to 4 slots
        occ = int(payload_slots.get("occupied", 0) or 0)
        occ = max(0, min(4, occ))
        slots = []
        for i in range(1, 5):
            slots.append({"id": i, "occupied": (i <= occ)})
        return slots

    return default_slots_4()

def booked_slot_ids(db: Session) -> set[int]:
    # booked means: approved and not expired/cancelled/rejected/completed
    expire_old_bookings(db)
    rows = db.query(Booking).filter(
        Booking.device_id == DEVICE_ID,
        Booking.status == "approved"
    ).all()
    return {int(r.slot_id) for r in rows}

def build_slots_view(device: Device, booked_ids: set[int]) -> list[dict]:
    # combine occupancy + booked
    slots = normalize_slots(device.slots)
    out = []
    for s in slots:
        sid = int(s["id"])
        occupied = bool(s.get("occupied", False))
        booked = sid in booked_ids
        state = "occupied" if occupied else ("booked" if booked else "free")
        out.append({"id": sid, "occupied": occupied, "booked": booked, "state": state})
    return out

# -------------------- PAGES --------------------
@app.get("/", response_class=HTMLResponse)
def root(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return RedirectResponse("/dashboard" if user else "/login", status_code=302)

@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    return templates.TemplateResponse("signup.html", {"request": request, "error": None})

@app.post("/signup")
def signup(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    full_name = full_name.strip()
    email = email.strip().lower()

    if password != confirm_password:
        return templates.TemplateResponse("signup.html", {"request": request, "error": "Passwords do not match"})
    if len(password) < 6:
        return templates.TemplateResponse("signup.html", {"request": request, "error": "Password must be at least 6 characters"})
    if db.query(User).filter(User.email == email).first():
        return templates.TemplateResponse("signup.html", {"request": request, "error": "Email already exists"})

    any_user = db.query(User).first()
    if any_user is None:
        # first user becomes admin + approved
        role = "admin"
        status = "approved"
    else:
        role = "user"
        status = "pending"

    user = User(
        full_name=full_name,
        email=email,
        password_hash=hash_password(password),
        role=role,
        status=status,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # ensure device row exists
    ensure_device_row(db)

    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie("session", make_session(user.id), httponly=True, samesite="lax")
    return resp

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid email or password"})

    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie("session", make_session(user.id), httponly=True, samesite="lax")
    return resp

@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("session")
    return resp

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user, "device_id": DEVICE_ID})

# -------------------- API: SLOTS --------------------
@app.get("/api/slots")
def api_slots(request: Request, db: Session = Depends(get_db)):
    user = require_approved_user(request, db)
    device = ensure_device_row(db)
    booked_ids = booked_slot_ids(db)
    return {
        "deviceId": DEVICE_ID,
        "userRole": user.role,
        "userStatus": user.status,
        "slots": build_slots_view(device, booked_ids),
        "bookingTTLMin": BOOKING_TTL_MINUTES,
    }

# -------------------- API: BOOKINGS (USER) --------------------
class BookingRequestBody(BaseModel):
    slotId: int

@app.post("/api/bookings/request")
def request_booking(request: Request, body: BookingRequestBody, db: Session = Depends(get_db)):
    user = require_approved_user(request, db)

    # normal users only; admin can also test, but allowed
    expire_old_bookings(db)
    device = ensure_device_row(db)

    slot_id = int(body.slotId)
    if slot_id not in [1, 2, 3, 4]:
        raise HTTPException(status_code=400, detail="Invalid slotId")

    booked_ids = booked_slot_ids(db)
    slots_view = build_slots_view(device, booked_ids)
    slot = next((s for s in slots_view if s["id"] == slot_id), None)

    if not slot:
        raise HTTPException(status_code=400, detail="Slot not found")

    if slot["occupied"]:
        raise HTTPException(status_code=400, detail="Slot is occupied (IR sensor)")
    if slot["booked"]:
        raise HTTPException(status_code=400, detail="Slot already booked")

    # one active booking per user at a time (recommended)
    existing = db.query(Booking).filter(
        Booking.user_id == user.id,
        Booking.device_id == DEVICE_ID,
        Booking.status.in_(["pending", "approved"])
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="You already have a pending/approved booking")

    b = Booking(
        user_id=user.id,
        device_id=DEVICE_ID,
        slot_id=slot_id,
        status="pending",
        expires_at=now_utc() + timedelta(minutes=BOOKING_TTL_MINUTES),
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    return {"ok": True, "bookingId": b.id, "status": b.status, "expiresAt": b.expires_at.isoformat()}

class CancelBookingBody(BaseModel):
    bookingId: int

@app.post("/api/bookings/cancel")
def cancel_booking(request: Request, body: CancelBookingBody, db: Session = Depends(get_db)):
    user = require_approved_user(request, db)
    expire_old_bookings(db)

    b = db.query(Booking).filter(Booking.id == int(body.bookingId)).first()
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found")

    if user.role != "admin" and b.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your booking")

    if b.status not in ["pending", "approved"]:
        raise HTTPException(status_code=400, detail=f"Cannot cancel booking in status {b.status}")

    b.status = "cancelled"
    b.finished_at = now_utc()
    db.commit()

    # optional: notify device (not required for basic logic)
    try:
        send_c2d(DEVICE_ID, {"cancelBooking": True, "slotId": int(b.slot_id)})
    except Exception:
        pass

    return {"ok": True}

@app.get("/api/bookings/me")
def my_bookings(request: Request, db: Session = Depends(get_db)):
    user = require_approved_user(request, db)
    expire_old_bookings(db)

    rows = db.query(Booking).filter(Booking.user_id == user.id, Booking.device_id == DEVICE_ID).order_by(Booking.created_at.desc()).limit(20).all()
    return [
        {
            "id": r.id,
            "slotId": r.slot_id,
            "status": r.status,
            "createdAt": r.created_at.isoformat() if r.created_at else None,
            "expiresAt": r.expires_at.isoformat() if r.expires_at else None,
        }
        for r in rows
    ]

# -------------------- ADMIN: USERS --------------------
@app.get("/api/admin/users/pending")
def admin_pending_users(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    rows = db.query(User).filter(User.role == "user", User.status == "pending").order_by(User.created_at.asc()).all()
    return [{"id": u.id, "fullName": u.full_name, "email": u.email, "status": u.status} for u in rows]

class AdminUserAction(BaseModel):
    userId: int

@app.post("/api/admin/users/approve")
def admin_approve_user(request: Request, body: AdminUserAction, db: Session = Depends(get_db)):
    require_admin(request, db)
    u = db.query(User).filter(User.id == int(body.userId), User.role == "user").first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    u.status = "approved"
    db.commit()
    return {"ok": True}

@app.post("/api/admin/users/reject")
def admin_reject_user(request: Request, body: AdminUserAction, db: Session = Depends(get_db)):
    require_admin(request, db)
    u = db.query(User).filter(User.id == int(body.userId), User.role == "user").first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    u.status = "rejected"
    db.commit()
    return {"ok": True}

# -------------------- ADMIN: BOOKINGS --------------------
@app.get("/api/admin/bookings/pending")
def admin_pending_bookings(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    expire_old_bookings(db)
    rows = db.query(Booking).filter(
        Booking.device_id == DEVICE_ID,
        Booking.status == "pending"
    ).order_by(Booking.created_at.asc()).all()

    out = []
    for r in rows:
        u = db.query(User).filter(User.id == r.user_id).first()
        out.append({
            "id": r.id,
            "slotId": r.slot_id,
            "status": r.status,
            "expiresAt": r.expires_at.isoformat() if r.expires_at else None,
            "user": {"id": u.id, "fullName": u.full_name, "email": u.email} if u else None,
        })
    return out

class AdminBookingAction(BaseModel):
    bookingId: int

@app.post("/api/admin/bookings/approve")
def admin_approve_booking(request: Request, body: AdminBookingAction, db: Session = Depends(get_db)):
    require_admin(request, db)
    expire_old_bookings(db)

    b = db.query(Booking).filter(Booking.id == int(body.bookingId)).first()
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found")
    if b.status != "pending":
        raise HTTPException(status_code=400, detail=f"Booking is not pending (status={b.status})")

    # check slot still free
    device = ensure_device_row(db)
    booked_ids = booked_slot_ids(db)
    slots_view = build_slots_view(device, booked_ids)
    slot = next((s for s in slots_view if s["id"] == int(b.slot_id)), None)
    if not slot:
        raise HTTPException(status_code=400, detail="Slot not found")
    if slot["occupied"]:
        b.status = "rejected"
        b.finished_at = now_utc()
        db.commit()
        raise HTTPException(status_code=400, detail="Slot is occupied now, booking rejected")

    # approve
    b.status = "approved"
    b.approved_at = now_utc()
    b.expires_at = now_utc() + timedelta(minutes=BOOKING_TTL_MINUTES)  # renew time window for arrival
    db.commit()

    # ✅ AUTO OPEN GATE + tell device which slot was booked
    try:
        send_c2d(DEVICE_ID, {"slotBooked": True, "slotId": int(b.slot_id)})
        send_c2d(DEVICE_ID, {"openGate": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Approved booking, but C2D failed: {type(e).__name__}: {e}")

    return {"ok": True}

@app.post("/api/admin/bookings/reject")
def admin_reject_booking(request: Request, body: AdminBookingAction, db: Session = Depends(get_db)):
    require_admin(request, db)
    expire_old_bookings(db)

    b = db.query(Booking).filter(Booking.id == int(body.bookingId)).first()
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found")
    if b.status != "pending":
        raise HTTPException(status_code=400, detail=f"Booking is not pending (status={b.status})")

    b.status = "rejected"
    b.finished_at = now_utc()
    db.commit()
    return {"ok": True}

# -------------------- ADMIN: GATE + EXIT (manual) --------------------
@app.post("/api/cmd/open-gate")
def cmd_open_gate(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    try:
        send_c2d(DEVICE_ID, {"openGate": True})
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"open-gate failed: {type(e).__name__}: {e}")

class ExitBody(BaseModel):
    approved: bool

@app.post("/api/cmd/exit-approved")
def cmd_exit_approved(request: Request, body: ExitBody, db: Session = Depends(get_db)):
    require_admin(request, db)
    try:
        send_c2d(DEVICE_ID, {"exitApproved": bool(body.approved)})
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"exit-approved failed: {type(e).__name__}: {e}")

# -------------------- INGEST (from bridge/function) --------------------
@app.post("/api/ingest")
async def ingest(request: Request, db: Session = Depends(get_db)):
    key = request.headers.get("x-api-key", "")
    if key != INGEST_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    payload = await request.json()
    device_id = payload.get("deviceId") or payload.get("device_id")
    if device_id != DEVICE_ID:
        raise HTTPException(status_code=400, detail=f"Only {DEVICE_ID} is accepted")

    device = ensure_device_row(db)

    device.entrance_cm = int(payload.get("entranceCm") or 0)
    device.exit_approved = bool(payload.get("exitApproved") or False)
    device.last_msg_count = int(payload.get("msgCount") or 0)

    # slots can be array or old object
    device.slots = normalize_slots(payload.get("slots"))

    db.commit()
    return {"ok": True}
