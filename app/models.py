from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, JSON
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from .db import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    full_name = Column(String(120), nullable=False)
    email = Column(String(200), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)

    # roles: "admin" | "user"
    role = Column(String(20), default="user")

    # status: "pending" | "approved" | "rejected"
    status = Column(String(20), default="pending")

    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Device(Base):
    __tablename__ = "devices"
    id = Column(Integer, primary_key=True)
    device_id = Column(String(128), unique=True, index=True, nullable=False)

    entrance_cm = Column(Integer, default=0)
    exit_approved = Column(Boolean, default=False)

    # We store occupancy here (booked is computed from bookings)
    # Example: [{"id":1,"occupied":false}, ...]
    slots = Column(JSON, default=list)

    last_msg_count = Column(Integer, default=0)
    last_seen = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class Booking(Base):
    __tablename__ = "bookings"
    id = Column(Integer, primary_key=True)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    device_id = Column(String(128), nullable=False)
    slot_id = Column(Integer, nullable=False)

    # "pending" | "approved" | "cancelled" | "rejected" | "expired" | "completed"
    status = Column(String(20), default="pending")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User")
