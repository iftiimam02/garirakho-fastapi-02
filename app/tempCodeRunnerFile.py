import os
from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from .db import Base, engine, SessionLocal
from .models import User, Device
from .auth import hash_password, verify_password, make_session, read_session
from .iot import send_c2d

INGEST_API_KEY = os.getenv("INGEST_API_KEY", "devkey")

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Garirakho")
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(request: Request, db: Session) -> User | None:
    uid = read_session(request)
    if not uid:
        return None
    return db.query(User).filter(User.id == uid).first()


def require_login(request: Request, db: Session) -> User:
    u = get_current_user(request, db)
    if not u:
        raise HTTPException(status_code=401, detail="Not logged in")
    return u


def require_admin(request: Request, db: Session) -> User:
    u = require_login(request, db)
    if not u.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return u


@app.get("/", response_class=HTMLResponse)
def root(request: Request, db: Session = Depends(get_db)):
    u = get_current_user(request, db)
    if u:
        return RedirectResponse("/dashboard", status_code=302)
    return RedirectResponse("/login", status_code=302)


# -------------------- AUTH UI --------------------

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

    # First user becomes admin automatically (easy bootstrap)
    any_user = db.query(User).first()
    is_admin = (any_user is None)

    u = User(
        full_name=full_name,
        email=email,
        password_hash=hash_password(password),
        is_admin=is_admin,
    )
    db.add(u)
    db.commit()
    db.refresh(u)

    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie("session", make_session(u.id), httponly=True, samesite="lax")
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
    u = db.query(User).filter(User.email == email).first()
    if not u or not verify_password(password, u.password_hash):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid email or password"})

    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie("session", make_session(u.id), httponly=True, samesite="lax")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("session")
    return resp


# -------------------- DASHBOARD UI --------------------

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    u = get_current_user(request, db)
    if not u:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": u})


# -------------------- API (dashboard uses this) --------------------

@app.get("/api/me")
def api_me(request: Request, db: Session = Depends(get_db)):
    u = require_login(request, db)
    return {"email": u.email, "fullName": u.full_name, "isAdmin": u.is_admin}


@app.get("/api/devices")
def api_devices(request: Request, db: Session = Depends(get_db)):
    u = require_login(request, db)
    devices = db.query(Device).order_by(Device.last_seen.desc()).all()
    return [
        {
            "deviceId": d.device_id,
            "entranceCm": d.entrance_cm,
            "exitApproved": d.exit_approved,
            "slots": d.slots or [],
            "lastMsgCount": d.last_msg_count,
            "lastSeen": d.last_seen.isoformat() if d.last_seen else None,
            "isAdmin": u.is_admin,
        }
        for d in devices
    ]


# -------------------- TELEMETRY INGEST (Azure Function calls this) --------------------

@app.post("/api/ingest")
async def ingest(request: Request, db: Session = Depends(get_db)):
    key = request.headers.get("x-api-key", "")
    if key != INGEST_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    payload = await request.json()

    device_id = payload.get("deviceId")
    if not device_id:
        raise HTTPException(status_code=400, detail="deviceId missing")

    d = db.query(Device).filter(Device.device_id == device_id).first()
    if not d:
        d = Device(device_id=device_id)
        db.add(d)

    d.entrance_cm = int(payload.get("entranceCm") or 0)
    d.exit_approved = bool(payload.get("exitApproved") or False)
    d.slots = payload.get("slots") or []
    d.last_msg_count = int(payload.get("msgCount") or 0)

    db.commit()
    return {"ok": True}


# -------------------- ADMIN COMMANDS (C2D) --------------------

@app.post("/api/cmd/open-gate")
def cmd_open_gate(request: Request, deviceId: str, db: Session = Depends(get_db)):
    require_admin(request, db)
    send_c2d(deviceId, {"openGate": True})
    return {"ok": True}


@app.post("/api/cmd/exit-approved")
def cmd_exit_approved(request: Request, deviceId: str, approved: bool, db: Session = Depends(get_db)):
    require_admin(request, db)
    send_c2d(deviceId, {"exitApproved": bool(approved)})
    return {"ok": True}


@app.post("/api/cmd/book-slots")
def cmd_book_slots(
    request: Request,
    deviceId: str,
    slot1: bool = False,
    slot2: bool = False,
    slot3: bool = False,
    slot4: bool = False,
    db: Session = Depends(get_db),
):
    require_admin(request, db)
    send_c2d(deviceId, {
        "slot1Booked": bool(slot1),
        "slot2Booked": bool(slot2),
        "slot3Booked": bool(slot3),
        "slot4Booked": bool(slot4),
    })
    return {"ok": True}
