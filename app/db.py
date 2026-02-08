# app/db.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ALLOW_SQLITE_FALLBACK = os.getenv("ALLOW_SQLITE_FALLBACK", "0").strip() == "1"

if not DATABASE_URL:
    if ALLOW_SQLITE_FALLBACK:
        # NOTE: on Azure Linux, ./ is NOT reliably persistent.
        # Use /home if you really insist on sqlite.
        DATABASE_URL = "sqlite:////home/garirakho.db"
        print("⚠️ DATABASE_URL missing → using SQLITE fallback:", DATABASE_URL)
    else:
        raise RuntimeError("❌ DATABASE_URL is missing. Set it in App Service → Configuration.")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
    pool_recycle=1800,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
