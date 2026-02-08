# app/auth.py
import os
from typing import Optional
from passlib.context import CryptContext
from itsdangerous import URLSafeSerializer, BadSignature
from fastapi import Request

pwd = CryptContext(schemes=["argon2"], deprecated="auto")

SESSION_SECRET = os.getenv("SESSION_SECRET", "change-this-in-azure")
serializer = URLSafeSerializer(SESSION_SECRET, salt="session")

def hash_password(p: str) -> str:
    return pwd.hash(p)

def verify_password(p: str, h: str) -> bool:
    return pwd.verify(p, h)

def make_session(user_id: int) -> str:
    return serializer.dumps({"uid": user_id})

def read_session(request: Request) -> Optional[int]:
    token = request.cookies.get("session")
    if not token:
        return None
    try:
        data = serializer.loads(token)
        return int(data["uid"])
    except (BadSignature, KeyError, ValueError):
        return None
