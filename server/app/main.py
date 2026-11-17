"""API do Aether Vigia: agente, painel do dono e webhook do WhatsApp."""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, undefer

from . import alerts, anomaly, security
from .catalog import CATALOG
from .config import settings
from .db import (Alert, Camera, Event, Metric, Operator, Recipient, SessionLocal, get_db, init_db,
                 kv_get, kv_set, session_scope)
from .whatsapp import SendError, digits, get_provider, parse_incoming, verify_meta_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vigia")
STATIC = Path(__file__).parent / "static"
MAX_JPEG = 2 * 1024 * 1024
ALLOWED_SCHEMES = {"rtsp", "rtsps", "http", "https"}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


async def _worker() -> None:
    while True:
        try:
            await asyncio.to_thread(_tick_once)
        except Exception:  # o laço de alertas nunca pode morrer
            log.exception("Falha no ciclo de alertas")
        await asyncio.sleep(5)


def _tick_once() -> None:
    with session_scope() as db:
        alerts.tick(db)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.validate()
    init_db()
    task = asyncio.create_task(_worker()) if settings.worker_enabled else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Aether Vigia", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


# Agente ---------------------------------------------------------------------------

class CameraState(BaseModel):
    id: int
    state: str


class Heartbeat(BaseModel):
    agent_id: str = "agente"
    version: str = ""
    cameras: List[CameraState] = []


class MinuteIn(BaseModel):
    ts: int
    uptime: float = 1.0
    fps: float = 0.0
    fps_cv: float = 0.0
    gaps: int = 0
    reconnects: int = 0
    brightness: float = 0.0
    contrast: float = 0.0
    sharp_ratio: float = 1.0
    flicker: float = 0.0
    motion: float = 0.0
    motion_p95: float = 0.0
    active: float = 0.0
    persons: int = 0
    vehicles: int = 0


class MetricsIn(BaseModel):
    camera_id: int
    minutes: List[MinuteIn] = Field(max_length=2000)


class EventIn(BaseModel):
    camera_id: int
    type: str
    state: str  # open|close
    ts: float
    data: Dict[str, Any] = {}
    snapshot_b64: Optional[str] = Field(default=None, max_length=3_000_000)

    @field_validator("type")
    @classmethod
    def known_type(cls, v: str) -> str:
        if v not in CATALOG:
            raise ValueError("tipo desconhecido")
        return v


def _camera_or_404(db: Session, camera_id: int) -> Camera:
    camera = db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(404, "Câmera não encontrada")
    return camera


@app.get("/api/agent/config", dependencies=[Depends(security.require_agent)])
def agent_config(db: Session = Depends(get_db)) -> Dict[str, Any]:
    cams = db.scalars(select(Camera).where(Camera.enabled.is_(True))).all()
    return {"timezone": settings.timezone, "cameras": [{
        "id": c.id, "name": c.name, "url": security.decrypt(c.rtsp_enc),
        "sample_fps": c.sample_fps, "armed_start": c.armed_start, "armed_end": c.armed_end,
        "armed_days": c.armed_days, "zones": c.zones, "loiter_seconds": c.loiter_seconds,
    } for c in cams]}


@app.post("/api/agent/heartbeat", dependencies=[Depends(security.require_agent)])
def agent_heartbeat(body: Heartbeat, db: Session = Depends(get_db)) -> Dict[str, bool]:
    now = time.time()
    kv_set(db, "agent", {"last_seen": now, "agent_id": body.agent_id, "version": body.version})
    for item in body.cameras:
        camera = db.get(Camera, item.id)
        if camera:
            camera.state = item.state if item.state in {"online", "offline"} else "unknown"
            if camera.state == "online":
                camera.last_seen = now
    db.commit()
    return {"ok": True}


@app.post("/api/agent/metrics", dependencies=[Depends(security.require_agent)])
def agent_metrics(body: MetricsIn, db: Session = Depends(get_db)) -> Dict[str, Any]:
    camera = _camera_or_404(db, body.camera_id)
    buckets = anomaly.load_buckets(db, camera.id)
    for item in sorted(body.minutes, key=lambda m: m.ts):
        exists = db.scalar(select(Metric.id).where(Metric.camera_id == camera.id, Metric.ts == item.ts))
        if exists:
            continue
        metric = Metric(camera_id=camera.id, **item.model_dump())
        anomaly.score_minute(db, camera, metric, buckets)
        db.add(metric)
    db.flush()
    want_snapshot: List[int] = []
    if body.minutes:
        recent = db.scalars(select(Metric).where(Metric.camera_id == camera.id)
                            .order_by(desc(Metric.ts)).limit(anomaly.QUIET_WINDOW)).all()
        latest_ts = recent[0].ts
        alerts.evaluate_stability(db, camera.id, recent, latest_ts)
        bucket = buckets.get(anomaly.bucket_of(latest_ts))
        decision = anomaly.evaluate(recent, camera.threshold_mult, bucket.mean if bucket else None)
        for type_, verdict in decision.items():
            if verdict is True:
                z = recent[0].z_activity
                event = alerts.open_event(db, camera.id, type_, latest_ts,
                                          data={"z": z, "motion": recent[0].motion})
                if not event.has_snapshot and type_ == "BEHAVIOR_SPIKE":
                    want_snapshot.append(event.id)
                    # o alerta espera a imagem chegar, por no máximo 20 segundos
                    for a in db.scalars(select(Alert).where(Alert.event_id == event.id,
                                                            Alert.status == "pending")):
                        a.not_before = max(a.not_before, time.time() + 20)
            elif verdict is False:
                alerts.close_event(db, camera.id, type_, latest_ts)
    db.commit()
    return {"snapshot_for": want_snapshot}


@app.post("/api/agent/events", dependencies=[Depends(security.require_agent)])
def agent_event(body: EventIn, db: Session = Depends(get_db)) -> Dict[str, Any]:
    _camera_or_404(db, body.camera_id)
    if body.state == "open":
        snapshot = None
        if body.snapshot_b64:
            try:
                snapshot = base64.b64decode(body.snapshot_b64, validate=True)
            except ValueError as exc:
                raise HTTPException(422, "Imagem inválida") from exc
        event = alerts.open_event(db, body.camera_id, body.type, body.ts, data=body.data,
                                  snapshot=snapshot)
    elif body.state == "close":
        event = alerts.close_event(db, body.camera_id, body.type, body.ts)
    else:
        raise HTTPException(422, "state deve ser open ou close")
    db.commit()
    return {"id": event.id if event else None}


async def _read_jpeg(request: Request) -> bytes:
    data = await request.body()
    if len(data) > MAX_JPEG or not data.startswith(b"\xff\xd8"):
        raise HTTPException(422, "Envie um JPEG de até 2 MB")
    return data


@app.post("/api/agent/cameras/{camera_id}/thumb", dependencies=[Depends(security.require_agent)])
async def agent_thumb(camera_id: int, request: Request) -> Dict[str, bool]:
    data = await _read_jpeg(request)
    with session_scope() as db:
        camera = _camera_or_404(db, camera_id)
        camera.thumb, camera.thumb_ts = data, time.time()
    return {"ok": True}


@app.post("/api/agent/events/{event_id}/snapshot", dependencies=[Depends(security.require_agent)])
async def agent_snapshot(event_id: int, request: Request) -> Dict[str, bool]:
    data = await _read_jpeg(request)
    with session_scope() as db:
        event = db.get(Event, event_id)
        if event is None:
            raise HTTPException(404, "Ocorrência não encontrada")
        event.snapshot, event.has_snapshot = data, True
    return {"ok": True}


# Painel do dono --------------------------------------------------------------------

class LoginIn(BaseModel):
    password: str = Field(max_length=200)
    username: str = Field(default="", max_length=40)


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response) -> Dict[str, Any]:
    security.check_login_rate(request)
    username = body.username.strip().lower()
    role, operator_id, name = None, None, None
    if username:   # operador com login próprio
        with SessionLocal() as db:
            operator = db.scalars(select(Operator).where(Operator.username == username,
                                                         Operator.active.is_(True))).first()
            if operator and security.verify_password(body.password, operator.password_hash):
                role, operator_id, name = "operator", operator.id, operator.name
                operator.last_login = time.time()
                db.commit()
    else:          # dono, ou a senha única de operador
        role = security.password_role(body.password)
    if role is None:
        security.record_failed_login(request)
        raise HTTPException(401, "Usuário ou senha incorretos" if username else "Senha incorreta")
    response.set_cookie(security.COOKIE, security.create_session(role, operator_id, name),
                        httponly=True, samesite="lax", secure=not settings.is_dev,
                        max_age=security.SESSION_DAYS * 86400)
    return {"ok": True, "role": role}


@app.post("/api/logout")
def logout(response: Response) -> Dict[str, bool]:
    response.delete_cookie(security.COOKIE)
    return {"ok": True}


owner = [Depends(security.require_owner)]
user = [Depends(security.require_user)]


@app.get("/api/me")
def me(who: dict = Depends(security.current_user)) -> Dict[str, Any]:
    return {"role": who["role"], "name": who["name"], "review": settings.operator_review,
            "review_minutes": settings.review_timeout_s // 60}


def _event_json(e: Event, names: Dict[int, str], pending_review: Optional[set] = None) -> Dict[str, Any]:
    info = CATALOG[e.type]
    return {
        "awaiting_review": e.id in (pending_review or set()),
        "reviewed_by": (e.data or {}).get("reviewed_by"),
        "reviewed_name": (e.data or {}).get("reviewed_name"),
        "id": e.id, "camera_id": e.camera_id, "camera": names.get(e.camera_id, settings.site_name),
        "type": e.type, "kind": info.kind, "severity": e.severity, "status": e.status,
        "started_at": e.started_at, "ended_at": e.ended_at, "title": e.title,
        "detail": e.ai_note or e.detail, "advice": info.advice, "has_snapshot": e.has_snapshot,
        "acked": e.acked_at is not None, "feedback": e.feedback,
    }


def _names(db: Session) -> Dict[int, str]:
    return dict(db.execute(select(Camera.id, Camera.name)).all())


def _bucketize(rows: List[Metric], start: float, step: int, count: int) -> List[Dict[str, Any]]:
    grouped: List[List[Metric]] = [[] for _ in range(count)]
    for m in rows:
        idx = int((m.ts - start) // step)
        if 0 <= idx < count:
            grouped[idx].append(m)
    out = []
    for i, items in enumerate(grouped):
        t = start + i * step
        if not items:
            out.append({"t": t, "uptime": None})
            continue
        n = len(items)
        uptime = sum(m.uptime for m in items) / n
        fps_cv = sum(m.fps_cv for m in items) / n
        gaps = sum(m.gaps for m in items)
        sharp = sum(m.sharp_ratio for m in items) / n
        health = 100 * uptime * (1 - min(fps_cv, 0.4)) * (1 - min(gaps / (n * 10), 0.3))
        health *= min(1.0, 0.6 + 0.4 * min(sharp, 1.0))
        out.append({
            "t": t, "uptime": round(uptime, 3), "fps": round(sum(m.fps for m in items) / n, 2),
            "sharp": round(sharp * 100, 1),
            "brightness": round(sum(m.brightness for m in items) / n, 1),
            "motion": round(sum(m.motion for m in items) / n, 3),
            "flicker": round(max(m.flicker for m in items), 2),
            "reconnects": sum(m.reconnects for m in items), "gaps": gaps,
            "health": round(health, 1), "persons": max(m.persons for m in items),
            "vehicles": max(m.vehicles for m in items),
        })
    return out


@app.get("/api/overview", dependencies=user)
def overview(db: Session = Depends(get_db)) -> Dict[str, Any]:
    now = time.time()
    names = _names(db)
    cams = db.scalars(select(Camera).where(Camera.enabled.is_(True)).order_by(Camera.name)).all()
    open_events = db.scalars(select(Event).where(Event.status == "open")
                             .order_by(desc(Event.started_at))).all()
    step, count = 900, 96
    start = (int(now) // step) * step - (count - 1) * step
    rows = db.scalars(select(Metric).where(Metric.ts >= start)).all()
    by_cam: Dict[int, List[Metric]] = {}
    for m in rows:
        by_cam.setdefault(m.camera_id, []).append(m)
    cameras = []
    for c in cams:
        series = _bucketize(by_cam.get(c.id, []), start, step, count)
        buckets = anomaly.load_buckets(db, c.id)
        for point in series:
            band = anomaly.expected_band(buckets, point["t"] + step / 2)
            if band:
                point["low"], point["exp"], point["high"] = [round(v, 3) for v in band]
        known = [p["health"] for p in series if p.get("health") is not None]
        cam_events = [e for e in open_events if e.camera_id == c.id]
        cameras.append({
            "id": c.id, "name": c.name, "location": c.location, "state": c.state,
            "health": round(sum(known) / len(known), 1) if known else None,
            "learning": round(anomaly.learning_progress(buckets, now), 2),
            "has_thumb": c.thumb_ts is not None, "thumb_ts": c.thumb_ts,
            "open_events": len(cam_events),
            "worst": max((e.severity for e in cam_events),
                         key=lambda s: {"info": 0, "warning": 1, "critical": 2}[s], default=None),
            "series": series,
        })
    agent = kv_get(db, "agent")
    since = now - 86400
    pending = alerts.review_ids(db)
    to_review = db.scalars(select(Event).where(Event.id.in_(pending))
                           .order_by(desc(Event.started_at))).all() if pending else []
    shown = {e.id for e in open_events}
    listed = list(open_events) + [e for e in to_review if e.id not in shown]
    return {
        "site": settings.site_name, "now": now, "step": step, "cameras": cameras,
        "review_count": len(pending),
        "open_events": [_event_json(e, names, pending) for e in listed],
        "agent": {"last_seen": agent.get("last_seen"),
                  "online": bool(agent.get("last_seen")) and now - agent["last_seen"] < settings.heartbeat_timeout_s},
        "events_24h": db.scalar(select(func.count()).select_from(Event).where(Event.started_at >= since)) or 0,
        "alerts_24h": db.scalar(select(func.count()).select_from(Alert).where(Alert.sent_at >= since)) or 0,
        "mute_until": kv_get(db, "mute").get("until", 0),
    }


@app.get("/api/cameras/{camera_id}/series", dependencies=user)
def camera_series(camera_id: int, hours: int = Query(24, ge=1, le=168),
                  db: Session = Depends(get_db)) -> Dict[str, Any]:
    camera = _camera_or_404(db, camera_id)
    now = time.time()
    step = 300 if hours <= 24 else 1800
    count = hours * 3600 // step
    start = (int(now) // step) * step - (count - 1) * step
    rows = db.scalars(select(Metric).where(Metric.camera_id == camera_id, Metric.ts >= start)).all()
    series = _bucketize(rows, start, step, count)
    buckets = anomaly.load_buckets(db, camera_id)
    for point in series:
        band = anomaly.expected_band(buckets, point["t"] + step / 2)
        if band:
            point["low"], point["exp"], point["high"] = [round(v, 3) for v in band]
    events = db.scalars(select(Event).where(Event.camera_id == camera_id, Event.started_at >= start)
                        .order_by(desc(Event.started_at))).all()
    names = {camera.id: camera.name}
    return {"camera": {"id": camera.id, "name": camera.name, "location": camera.location,
                       "state": camera.state, "has_thumb": camera.thumb_ts is not None,
                       "learning": round(anomaly.learning_progress(buckets, now), 2),
                       "threshold_mult": camera.threshold_mult},
            "step": step, "series": series,
            "events": [_event_json(e, names, alerts.review_ids(db)) for e in events]}


@app.get("/api/cameras/{camera_id}/thumb", dependencies=user)
def camera_thumb(camera_id: int, db: Session = Depends(get_db)) -> Response:
    row = db.scalars(select(Camera).options(undefer(Camera.thumb)).where(Camera.id == camera_id)).first()
    if row is None or not row.thumb:
        raise HTTPException(404, "Sem imagem ainda")
    return Response(row.thumb, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=60"})


@app.get("/api/events", dependencies=user)
def list_events(status: Optional[str] = None, kind: Optional[str] = None,
                limit: int = Query(50, ge=1, le=200), before: Optional[float] = None,
                db: Session = Depends(get_db)) -> Dict[str, Any]:
    q = select(Event).order_by(desc(Event.started_at)).limit(limit if not kind else 200)
    pending = alerts.review_ids(db)
    if status in {"open", "closed"}:
        q = q.where(Event.status == status)
    elif status == "review":
        q = q.where(Event.id.in_(pending or {-1}))
    if before:
        q = q.where(Event.started_at < before)
    names = _names(db)
    items = [_event_json(e, names, pending) for e in db.scalars(q)]
    if kind:
        items = [i for i in items if i["kind"] == kind][:limit]
    return {"events": items}


@app.get("/api/events/{event_id}/snapshot", dependencies=user)
def event_snapshot(event_id: int, db: Session = Depends(get_db)) -> Response:
    row = db.scalars(select(Event).options(undefer(Event.snapshot)).where(Event.id == event_id)).first()
    if row is None or not row.snapshot:
        raise HTTPException(404, "Sem imagem")
    return Response(row.snapshot, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


class FeedbackIn(BaseModel):
    feedback: str  # real|false


@app.post("/api/events/{event_id}/feedback")
def event_feedback(event_id: int, body: FeedbackIn, who: dict = Depends(security.current_user),
                   db: Session = Depends(get_db)) -> Dict[str, bool]:
    role, name = who["role"], who["name"]
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(404, "Ocorrência não encontrada")
    if body.feedback == "false":
        alerts.register_false_alarm(db, event, role, name)
    elif body.feedback == "real":
        alerts.apply_review(db, event, real=True, role=role, name=name)
        event.feedback, event.acked_at = "real", event.acked_at or time.time()
    else:
        raise HTTPException(422, "feedback inválido")
    db.commit()
    return {"ok": True}


class FlagIn(BaseModel):
    note: str = Field(min_length=3, max_length=500)
    severity: str = "warning"

    @field_validator("severity")
    @classmethod
    def valid_severity(cls, v: str) -> str:
        if v not in {"warning", "critical"}:
            raise ValueError("Gravidade inválida")
        return v


@app.post("/api/cameras/{camera_id}/flag")
def flag_anomaly(camera_id: int, body: FlagIn, who: dict = Depends(security.current_user),
                 db: Session = Depends(get_db)) -> Dict[str, Any]:
    """O operador (ou o dono) aponta algo que viu. Vai ao WhatsApp com a última imagem."""
    camera = db.scalars(select(Camera).options(undefer(Camera.thumb))
                        .where(Camera.id == camera_id)).first()
    if camera is None:
        raise HTTPException(404, "Câmera não encontrada")
    now = time.time()
    event = alerts.open_event(db, camera.id, "MANUAL", now, data={"reviewed_by": who["role"], "reviewed_name": who["name"]},
                              snapshot=camera.thumb, detail=body.note.strip(),
                              severity=body.severity)
    event.status, event.ended_at, event.feedback, event.acked_at = "closed", now, "real", now
    db.commit()
    return _event_json(event, {camera.id: camera.name})


class CameraIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    location: str = Field(default="", max_length=120)
    url: Optional[str] = Field(default=None, max_length=500)
    enabled: bool = True
    armed_start: Optional[str] = None
    armed_end: Optional[str] = None
    armed_days: List[int] = [0, 1, 2, 3, 4, 5, 6]
    zones: List[List[List[float]]] = []
    loiter_seconds: int = Field(default=180, ge=30, le=3600)

    @field_validator("zones")
    @classmethod
    def valid_zones(cls, v: List[List[List[float]]]) -> List[List[List[float]]]:
        if len(v) > 5:
            raise ValueError("No máximo 5 áreas por câmera")
        for polygon in v:
            if not 3 <= len(polygon) <= 20:
                raise ValueError("Cada área precisa de 3 a 20 pontos")
            for point in polygon:
                if len(point) != 2 or not all(0.0 <= c <= 1.0 for c in point):
                    raise ValueError("Ponto fora da imagem")
        return [[[round(x, 4), round(y, 4)] for x, y in polygon] for polygon in v]

    @field_validator("armed_days")
    @classmethod
    def valid_days(cls, v: List[int]) -> List[int]:
        if not v or any(d not in range(7) for d in v):
            raise ValueError("Escolha pelo menos um dia da semana")
        return sorted(set(v))

    @field_validator("url")
    @classmethod
    def valid_url(cls, v: Optional[str]) -> Optional[str]:
        if v and urlsplit(v).scheme.lower() not in ALLOWED_SCHEMES:
            raise ValueError("Use um endereço rtsp:// ou http://")
        return v

    @field_validator("armed_start", "armed_end")
    @classmethod
    def valid_time(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        if not TIME_RE.match(v):
            raise ValueError("Horário no formato 22:00")
        return v


def _camera_json(c: Camera) -> Dict[str, Any]:
    return {"id": c.id, "name": c.name, "location": c.location, "enabled": c.enabled, "state": c.state,
            "url_masked": security.mask_url(security.decrypt(c.rtsp_enc)),
            "armed_start": c.armed_start, "armed_end": c.armed_end, "armed_days": c.armed_days,
            "zones": c.zones, "loiter_seconds": c.loiter_seconds,
            "has_thumb": c.thumb_ts is not None}


@app.get("/api/cameras", dependencies=user)
def list_cameras(db: Session = Depends(get_db)) -> Dict[str, Any]:
    return {"cameras": [_camera_json(c) for c in db.scalars(select(Camera).order_by(Camera.name))]}


@app.post("/api/cameras", dependencies=owner)
def create_camera(body: CameraIn, db: Session = Depends(get_db)) -> Dict[str, Any]:
    if not body.url:
        raise HTTPException(422, "Informe o endereço da câmera")
    data = body.model_dump(exclude={"url"})
    camera = Camera(**data, rtsp_enc=security.encrypt(body.url), created_at=time.time())
    db.add(camera)
    db.commit()
    return _camera_json(camera)


@app.put("/api/cameras/{camera_id}", dependencies=owner)
def update_camera(camera_id: int, body: CameraIn, db: Session = Depends(get_db)) -> Dict[str, Any]:
    camera = _camera_or_404(db, camera_id)
    for key, value in body.model_dump(exclude={"url"}).items():
        setattr(camera, key, value)
    if body.url:  # vazio mantém a URL atual, que nunca volta para o navegador
        camera.rtsp_enc = security.encrypt(body.url)
    db.commit()
    return _camera_json(camera)


@app.delete("/api/cameras/{camera_id}", dependencies=owner)
def delete_camera(camera_id: int, db: Session = Depends(get_db)) -> Dict[str, bool]:
    db.delete(_camera_or_404(db, camera_id))
    db.commit()
    return {"ok": True}


USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,40}$")


class OperatorIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=8, max_length=200)

    @field_validator("username")
    @classmethod
    def clean_username(cls, v: str) -> str:
        v = v.strip().lower()
        if not USERNAME_RE.match(v):
            raise ValueError("Usuário com 3 a 40 letras minúsculas, números, ponto ou hífen")
        return v


def _operator_json(o: Operator) -> Dict[str, Any]:
    return {"id": o.id, "name": o.name, "username": o.username, "active": o.active,
            "last_login": o.last_login}


@app.get("/api/operators", dependencies=owner)
def list_operators(db: Session = Depends(get_db)) -> Dict[str, Any]:
    return {"operators": [_operator_json(o) for o in db.scalars(select(Operator).order_by(Operator.name))],
            "review": settings.operator_review, "review_minutes": settings.review_timeout_s // 60}


@app.post("/api/operators", dependencies=owner)
def create_operator(body: OperatorIn, db: Session = Depends(get_db)) -> Dict[str, Any]:
    if db.scalar(select(Operator.id).where(Operator.username == body.username)):
        raise HTTPException(409, "Já existe um operador com esse usuário")
    operator = Operator(name=body.name.strip(), username=body.username,
                        password_hash=security.hash_password(body.password), created_at=time.time())
    db.add(operator)
    db.commit()
    return _operator_json(operator)


@app.delete("/api/operators/{operator_id}", dependencies=owner)
def delete_operator(operator_id: int, db: Session = Depends(get_db)) -> Dict[str, bool]:
    operator = db.get(Operator, operator_id)
    if operator is None:
        raise HTTPException(404, "Operador não encontrado")
    db.delete(operator)
    db.commit()
    return {"ok": True}


class RecipientIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=10, max_length=20)
    min_severity: str = "warning"
    quiet_start: Optional[str] = None
    quiet_end: Optional[str] = None
    active: bool = True
    can_command: bool = True

    @field_validator("phone")
    @classmethod
    def clean_phone(cls, v: str) -> str:
        d = digits(v)
        if len(d) < 12:
            raise ValueError("Use o número com DDI e DDD. Exemplo: 5531999998888")
        return d

    @field_validator("min_severity")
    @classmethod
    def valid_severity(cls, v: str) -> str:
        if v not in {"info", "warning", "critical"}:
            raise ValueError("Gravidade inválida")
        return v

    @field_validator("quiet_start", "quiet_end")
    @classmethod
    def valid_time(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        if not TIME_RE.match(v):
            raise ValueError("Horário no formato 22:00")
        return v


def _recipient_json(r: Recipient) -> Dict[str, Any]:
    return {"id": r.id, "name": r.name, "phone": r.phone, "min_severity": r.min_severity,
            "quiet_start": r.quiet_start, "quiet_end": r.quiet_end, "active": r.active,
            "can_command": r.can_command}


@app.get("/api/recipients", dependencies=owner)
def list_recipients(db: Session = Depends(get_db)) -> Dict[str, Any]:
    provider = get_provider()
    recent = db.scalars(select(Alert).order_by(desc(Alert.id)).limit(20)).all()
    return {
        "recipients": [_recipient_json(r) for r in db.scalars(select(Recipient).order_by(Recipient.name))],
        "provider": {"name": provider.name, "configured": provider.configured},
        "mute_until": kv_get(db, "mute").get("until", 0),
        "recent": [{"id": a.id, "kind": a.kind, "status": a.status, "created_at": a.created_at,
                    "sent_at": a.sent_at, "error": a.last_error, "event_id": a.event_id} for a in recent],
    }


@app.post("/api/recipients", dependencies=owner)
def create_recipient(body: RecipientIn, db: Session = Depends(get_db)) -> Dict[str, Any]:
    r = Recipient(**body.model_dump())
    db.add(r)
    db.commit()
    return _recipient_json(r)


def _recipient_or_404(db: Session, recipient_id: int) -> Recipient:
    r = db.get(Recipient, recipient_id)
    if r is None:
        raise HTTPException(404, "Pessoa não encontrada")
    return r


@app.put("/api/recipients/{recipient_id}", dependencies=owner)
def update_recipient(recipient_id: int, body: RecipientIn, db: Session = Depends(get_db)) -> Dict[str, Any]:
    r = _recipient_or_404(db, recipient_id)
    for key, value in body.model_dump().items():
        setattr(r, key, value)
    db.commit()
    return _recipient_json(r)


@app.delete("/api/recipients/{recipient_id}", dependencies=owner)
def delete_recipient(recipient_id: int, db: Session = Depends(get_db)) -> Dict[str, bool]:
    db.delete(_recipient_or_404(db, recipient_id))
    db.commit()
    return {"ok": True}


@app.post("/api/recipients/{recipient_id}/test", dependencies=owner)
def test_recipient(recipient_id: int, db: Session = Depends(get_db)) -> Dict[str, bool]:
    r = _recipient_or_404(db, recipient_id)
    alerts.enqueue_simple(db, r, "test",
                          f"✅ Teste do Aether Vigia: os alertas de {settings.site_name} vão chegar aqui.",
                          [settings.site_name])
    db.commit()
    return {"ok": True}


@app.get("/api/whatsapp/connection", dependencies=owner)
def whatsapp_connection() -> Dict[str, Any]:
    """Estado da conexão por QR Code. Só existe para o conector Baileys."""
    provider = get_provider()
    if provider.name != "baileys":
        return {"supported": False}
    if not provider.configured:
        return {"supported": True, "status": "unconfigured"}
    try:
        data = provider.status()
    except SendError:
        return {"supported": True, "status": "unreachable"}
    return {"supported": True, "status": data.get("status"), "qr": data.get("qr"),
            "number": data.get("number")}


@app.post("/api/whatsapp/disconnect", dependencies=owner)
def whatsapp_disconnect() -> Dict[str, bool]:
    provider = get_provider()
    if provider.name != "baileys":
        raise HTTPException(400, "Este provedor não usa QR Code")
    try:
        provider.logout()
    except SendError as exc:
        raise HTTPException(502, "Não consegui falar com o conector de WhatsApp") from exc
    return {"ok": True}


class MuteIn(BaseModel):
    hours: float = Field(ge=0, le=168)


@app.post("/api/mute", dependencies=owner)
def set_mute(body: MuteIn, db: Session = Depends(get_db)) -> Dict[str, float]:
    until = time.time() + body.hours * 3600 if body.hours else 0
    kv_set(db, "mute", {**kv_get(db, "mute"), "until": until})
    db.commit()
    return {"until": until}


# Webhook do WhatsApp ----------------------------------------------------------------

@app.get("/webhook/whatsapp", response_class=PlainTextResponse)
def meta_verify(mode: str = Query("", alias="hub.mode"), token: str = Query("", alias="hub.verify_token"),
                challenge: str = Query("", alias="hub.challenge")) -> str:
    if mode == "subscribe" and settings.meta_verify_token and token == settings.meta_verify_token:
        return challenge
    raise HTTPException(403, "Token de verificação inválido")


def _handle_incoming(data: dict) -> None:
    provider = get_provider()
    with SessionLocal() as db:
        for message in parse_incoming(provider.name, data):
            recipient = alerts.recipient_for(db, message.phone)
            if recipient is None:
                log.warning("Comando ignorado de número não autorizado")
                continue
            answer = alerts.handle_command(db, message.text)
            db.commit()
            try:
                provider.reply(recipient.phone, answer)
            except Exception as exc:
                log.error("Não consegui responder no WhatsApp: %s", exc)


@app.post("/webhook/whatsapp")
async def meta_webhook(request: Request, x_hub_signature_256: str = Header(default="")) -> JSONResponse:
    raw = await request.body()
    if not verify_meta_signature(raw, x_hub_signature_256):
        raise HTTPException(403, "Assinatura inválida")
    await asyncio.to_thread(_handle_incoming, await request.json())
    return JSONResponse({"ok": True})


@app.post("/webhook/whatsapp/{secret}")
async def secret_webhook(secret: str, data: dict = Body(...)) -> JSONResponse:
    import hmac

    if not settings.wa_webhook_secret or not hmac.compare_digest(secret, settings.wa_webhook_secret):
        raise HTTPException(403, "Segredo inválido")
    await asyncio.to_thread(_handle_incoming, data)
    return JSONResponse({"ok": True})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/demo")
def demo() -> FileResponse:
    """Painel de demonstração: dados simulados, sem login, não toca no banco."""
    return FileResponse(STATIC / "demo.html", headers={"Cache-Control": "no-cache"})
