"""Modelos do banco. Todo horário é guardado em epoch UTC (segundos)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import (
    JSON, Boolean, Float, ForeignKey, Index, Integer, LargeBinary, String, Text,
    UniqueConstraint, create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import settings


class Base(DeclarativeBase):
    pass


class Camera(Base):
    __tablename__ = "cameras"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    location: Mapped[str] = mapped_column(String(120), default="")
    rtsp_enc: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sample_fps: Mapped[float] = mapped_column(Float, default=2.0)
    armed_start: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)  # "22:00"
    armed_end: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    armed_days: Mapped[list] = mapped_column(JSON, default=lambda: [0, 1, 2, 3, 4, 5, 6])
    zones: Mapped[list] = mapped_column(JSON, default=list)  # polígonos normalizados 0..1
    loiter_seconds: Mapped[int] = mapped_column(Integer, default=180)
    threshold_mult: Mapped[float] = mapped_column(Float, default=1.0)
    state: Mapped[str] = mapped_column(String(16), default="unknown")
    last_seen: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    thumb: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True, deferred=True)
    thumb_ts: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=0)


class Metric(Base):
    """Um registro por câmera por minuto."""
    __tablename__ = "metrics"
    __table_args__ = (UniqueConstraint("camera_id", "ts"), Index("ix_metric_ts", "ts"))
    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"))
    ts: Mapped[int] = mapped_column(Integer)
    uptime: Mapped[float] = mapped_column(Float, default=1.0)
    fps: Mapped[float] = mapped_column(Float, default=0.0)
    fps_cv: Mapped[float] = mapped_column(Float, default=0.0)
    gaps: Mapped[int] = mapped_column(Integer, default=0)
    reconnects: Mapped[int] = mapped_column(Integer, default=0)
    brightness: Mapped[float] = mapped_column(Float, default=0.0)
    contrast: Mapped[float] = mapped_column(Float, default=0.0)
    sharp_ratio: Mapped[float] = mapped_column(Float, default=1.0)
    flicker: Mapped[float] = mapped_column(Float, default=0.0)
    motion: Mapped[float] = mapped_column(Float, default=0.0)  # % médio do quadro em movimento
    motion_p95: Mapped[float] = mapped_column(Float, default=0.0)
    active: Mapped[float] = mapped_column(Float, default=0.0)  # fração do minuto com movimento
    persons: Mapped[int] = mapped_column(Integer, default=0)
    vehicles: Mapped[int] = mapped_column(Integer, default=0)
    z_activity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    iforest: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_event_status", "status", "started_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), nullable=True)
    type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(10))  # info|warning|critical
    status: Mapped[str] = mapped_column(String(10), default="open")  # open|closed
    started_at: Mapped[float] = mapped_column(Float)
    ended_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    title: Mapped[str] = mapped_column(String(120))
    detail: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    snapshot: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True, deferred=True)
    has_snapshot: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    acked_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    feedback: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # real|false


class Recipient(Base):
    __tablename__ = "recipients"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    phone: Mapped[str] = mapped_column(String(20))
    min_severity: Mapped[str] = mapped_column(String(10), default="warning")
    quiet_start: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    quiet_end: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    can_command: Mapped[bool] = mapped_column(Boolean, default=True)


class Operator(Base):
    """Pessoa da equipe com login próprio no painel."""
    __tablename__ = "operators"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    username: Mapped[str] = mapped_column(String(40), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float, default=0)
    last_login: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class Alert(Base):
    """Fila de saída do WhatsApp: cada linha é uma mensagem para uma pessoa."""
    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alert_status", "status", "not_before"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=True)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("recipients.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(10))  # alert|resolved|digest|test
    status: Mapped[str] = mapped_column(String(10), default="pending")
    # pending|sent|failed|cancelled|merged|skipped
    not_before: Mapped[float] = mapped_column(Float, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    provider_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=0)
    sent_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class BaselineBucket(Base):
    """Comportamento aprendido de uma câmera em uma meia hora típica."""
    __tablename__ = "baseline_buckets"
    __table_args__ = (UniqueConstraint("camera_id", "bucket"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"))
    bucket: Mapped[int] = mapped_column(Integer)
    n: Mapped[int] = mapped_column(Integer, default=0)
    mean: Mapped[float] = mapped_column(Float, default=0.0)
    var: Mapped[float] = mapped_column(Float, default=0.0)


class CameraModel(Base):
    __tablename__ = "camera_models"
    camera_id: Mapped[int] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), primary_key=True)
    trained_at: Mapped[float] = mapped_column(Float)
    n_rows: Mapped[int] = mapped_column(Integer)
    blob: Mapped[bytes] = mapped_column(LargeBinary)


class KV(Base):
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


_kwargs: dict = {"future": True, "pool_pre_ping": True}
if settings.database_url.startswith("sqlite"):
    _kwargs["connect_args"] = {"check_same_thread": False}
    if ":memory:" in settings.database_url:
        _kwargs["poolclass"] = StaticPool

engine = create_engine(settings.database_url, **_kwargs)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def kv_get(db: Session, key: str, default: Optional[dict] = None) -> dict:
    row = db.get(KV, key)
    return row.value if row else (default if default is not None else {})


def kv_set(db: Session, key: str, value: dict) -> None:
    row = db.get(KV, key)
    if row:
        row.value = value
    else:
        db.add(KV(key=key, value=value))
