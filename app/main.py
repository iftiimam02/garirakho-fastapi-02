# app/main.py
import os
from typing import Optional

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import text

from .db import Base, engine, SessionLocal
from .models import User, Device
from .auth import hash_password, verify_password, make_session, read_session
from .iot import send_c2d

INGEST_API_KEY = os.getenv("INGEST_API_KEY", "devkey")

app = FastAPI(title="Garirakho")
@app.get("/api/version")
def version():
    return {"version": "v2-2026-02-08"}
from starlette.middleware.base import BaseHTTPMiddleware

class NoCacheForStaticJS(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path == "/static/app.js":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

app.add_middleware(NoCacheForStaticJS)

templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# -------------------- DATABASE --------------------

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.on_event("startup")
def startup_db():
    # For permanent/prod stability: fail fast if DB is broken (don’t silently continue)
    Base.metadata.create_all(bind=engine)
    print("✅ DB OK: tables ensured")

@app.get("/api/db-check")
def db_check(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"db": "ok"}

# -------------------- AUTH HELPERS --------------------

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
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return user

# -------------------- ROUTES --------------------

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
    is_admin = (any_user is None)

    user = User(full_name=full_name, email=email, password_hash=hash_password(password), is_admin=is_admin)
    db.add(user)
    db.commit()
    db.refresh(user)

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
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user})
@app.get("/api/devices")
def api_devices(request: Request, db: Session = Depends(get_db)):
    # Allow either: logged-in session OR API key
    user = get_current_user(request, db)
    key = request.headers.get("x-api-key", "")

    if not user and key != INGEST_API_KEY:
        raise HTTPException(status_code=401, detail="Not logged in (or missing x-api-key)")

    devices = db.query(Device).order_by(Device.last_seen.desc()).all()

    return [
        {
            "deviceId": d.device_id,
            "entranceCm": d.entrance_cm,
            "exitApproved": d.exit_approved,
            "slots": d.slots if isinstance(d.slots, (dict, list)) else (d.slots or []),
            "lastMsgCount": d.last_msg_count,
            "lastSeen": d.last_seen.isoformat() if d.last_seen else None,
            "isAdmin": bool(user.is_admin) if user else False,
        }
        for d in devices
    ]
@app.post("/api/ingest")
async def ingest(request: Request, db: Session = Depends(get_db)):
    key = request.headers.get("x-api-key", "")
    if key != INGEST_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    payload = await request.json()
    device_id = payload.get("deviceId")
    if not device_id:
        raise HTTPException(status_code=400, detail="deviceId missing")

    device = db.query(Device).filter(Device.device_id == device_id).first()
    if not device:
        device = Device(device_id=device_id)
        db.add(device)

    device.entrance_cm = int(payload.get("entranceCm") or 0)
    device.exit_approved = bool(payload.get("exitApproved") or False)
    device.slots = payload.get("slots") or []
    device.last_msg_count = int(payload.get("msgCount") or 0)

    db.commit()
    return {"ok": True}

@app.post("/api/cmd/open-gate")
def cmd_open_gate(request: Request, deviceId: str, db: Session = Depends(get_db)):
    require_admin(request, db)
    send_c2d(deviceId, {"openGate": True})
    return {"ok": True}
