"""Motor de ocorrências e alertas: abre, correlaciona, filtra e envia."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from . import anomaly, vision_ai
from .catalog import CATALOG, SEVERITY_ICON, SEVERITY_RANK
from .config import settings
from .db import Alert, Camera, Event, Metric, Recipient, kv_get, kv_set
from .whatsapp import Outgoing, SendError, get_provider

log = logging.getLogger("vigia.alerts")
_tz = ZoneInfo(settings.timezone)

MAX_ATTEMPTS = 5
BACKOFF_S = (15, 60, 180, 600, 1800)
SITE_MIN_CAMERAS = 2
SITE_RATIO = 0.6
FLAP_WINDOW_S = 3600
FLAP_COUNT = 4
AI_DESCRIBED = {"INTRUSION", "LOITERING", "BEHAVIOR_SPIKE"}


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, _tz).strftime("%d/%m %H:%M")


def fmt_duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}" if m else f"{h}h"


def _in_window(start: Optional[str], end: Optional[str], ts: float) -> bool:
    if not start or not end:
        return False
    dt = datetime.fromtimestamp(ts, _tz)
    now = dt.hour * 60 + dt.minute
    s = int(start[:2]) * 60 + int(start[3:5])
    e = int(end[:2]) * 60 + int(end[3:5])
    return s <= now < e if s <= e else now >= s or now < e


def is_muted(db: Session, now: float, camera_id: Optional[int] = None) -> bool:
    mute = kv_get(db, "mute")
    if mute.get("until", 0) > now:
        return True
    return camera_id is not None and mute.get("cameras", {}).get(str(camera_id), 0) > now


def open_event(db: Session, camera_id: Optional[int], type_: str, ts: float,
               data: Optional[dict] = None, snapshot: Optional[bytes] = None,
               detail: Optional[str] = None, severity: Optional[str] = None) -> Event:
    existing = db.scalars(select(Event).where(
        Event.camera_id == camera_id if camera_id is not None else Event.camera_id.is_(None),
        Event.type == type_, Event.status == "open")).first()
    if existing:
        if snapshot and not existing.has_snapshot:
            existing.snapshot, existing.has_snapshot = snapshot, True
        return existing
    info = CATALOG[type_]
    event = Event(camera_id=camera_id, type=type_, severity=severity or info.severity, status="open",
                  started_at=ts, title=info.title, detail=detail or info.detail,
                  data=data or {}, snapshot=snapshot, has_snapshot=bool(snapshot))
    db.add(event)
    db.flush()
    enqueue_alerts(db, event, ts)
    return event


def close_event(db: Session, camera_id: Optional[int], type_: str, ts: float) -> Optional[Event]:
    event = db.scalars(select(Event).where(
        Event.camera_id == camera_id if camera_id is not None else Event.camera_id.is_(None),
        Event.type == type_, Event.status == "open")).first()
    if not event:
        return None
    event.status, event.ended_at = "closed", ts
    # Ainda na espera: voltou sozinho, não incomoda ninguém.
    held = db.scalars(select(Alert).where(Alert.event_id == event.id, Alert.status == "pending",
                                          Alert.kind == "alert", Alert.attempts == 0,
                                          Alert.not_before > time.time())).all()
    for alert in held:
        alert.status = "cancelled"
    already_sent = db.scalar(select(func.count()).select_from(Alert).where(
        Alert.event_id == event.id, Alert.kind == "alert", Alert.status == "sent"))
    if already_sent and CATALOG[type_].notify_resolved:
        recipients = db.scalars(select(Recipient).join(Alert, Alert.recipient_id == Recipient.id).where(
            Alert.event_id == event.id, Alert.kind == "alert", Alert.status == "sent")).all()
        for r in recipients:
            db.add(Alert(event_id=event.id, recipient_id=r.id, kind="resolved",
                         status="pending", not_before=0, created_at=time.time()))
    return event


def _recent_alert_exists(db: Session, event: Event, cooldown: int, now: float) -> bool:
    q = select(func.count()).select_from(Alert).join(Event, Alert.event_id == Event.id).where(
        Event.camera_id == event.camera_id, Event.type == event.type, Event.id != event.id,
        Alert.kind == "alert", Alert.status.in_(["sent", "pending", "review"]),
        Alert.created_at >= now - cooldown)
    return bool(db.scalar(q))


def enqueue_alerts(db: Session, event: Event, ts: float) -> None:
    info = CATALOG[event.type]
    now = time.time()
    if is_muted(db, now, event.camera_id) and event.severity != "critical":
        return
    if kv_get(db, "mute").get("until", 0) > now and event.type not in {"INTRUSION", "OBSTRUCTED"}:
        return
    if _recent_alert_exists(db, event, info.cooldown_s, now):
        return
    # Revisão: o crítico chega na hora; o resto espera o operador por alguns minutos.
    review = (settings.operator_review and event.severity != "critical"
              and event.type != "MANUAL")
    status = "review" if review else "pending"
    not_before = now + (settings.review_timeout_s if review else info.hold_s)
    for r in db.scalars(select(Recipient).where(Recipient.active.is_(True))):
        if SEVERITY_RANK[event.severity] < SEVERITY_RANK.get(r.min_severity, 1):
            continue
        if event.severity != "critical" and _in_window(r.quiet_start, r.quiet_end, now):
            continue
        db.add(Alert(event_id=event.id, recipient_id=r.id, kind="alert", status=status,
                     not_before=not_before, created_at=now))


def apply_review(db: Session, event: Event, real: bool, role: str,
                 name: Optional[str] = None) -> int:
    """Confirmação libera os alertas retidos; descarte cancela. Devolve quantos mudaram."""
    held = db.scalars(select(Alert).where(Alert.event_id == event.id,
                                          Alert.status == "review")).all()
    for alert in held:
        if real:
            alert.status, alert.not_before = "pending", 0
        else:
            alert.status = "cancelled"
    event.data = {**(event.data or {}), "reviewed_by": role, "reviewed_name": name}
    return len(held)


def review_ids(db: Session) -> set:
    return set(db.scalars(select(Alert.event_id).where(Alert.status == "review")).all())


def correlate_site_outage(db: Session, now: float) -> None:
    """Se quase todas as câmeras caem juntas, manda uma mensagem só, não uma por câmera."""
    held = db.execute(
        select(Alert, Event).join(Event, Alert.event_id == Event.id).where(
            Alert.status == "pending", Alert.kind == "alert", Alert.attempts == 0,
            Event.type == "OFFLINE", Event.status == "open")).all()
    cam_ids = {e.camera_id for _, e in held}
    if len(cam_ids) < SITE_MIN_CAMERAS:
        return
    total = db.scalar(select(func.count()).select_from(Camera).where(Camera.enabled.is_(True))) or 0
    if total == 0 or len(cam_ids) / total < SITE_RATIO:
        return
    site = db.scalars(select(Event).where(Event.camera_id.is_(None), Event.type == "SITE_DOWN",
                                          Event.status == "open")).first()
    if site is None:
        names = db.scalars(select(Camera.name).where(Camera.id.in_(cam_ids))).all()
        site = open_event(db, None, "SITE_DOWN", min(e.started_at for _, e in held),
                          data={"cameras": sorted(cam_ids)},
                          detail=f"{len(cam_ids)} de {total} câmeras pararam juntas: "
                                 + ", ".join(sorted(names)) + ".")
        for a in db.scalars(select(Alert).where(Alert.event_id == site.id)):
            a.not_before = 0
    for alert, _ in held:
        alert.status = "merged"


def close_site_outage_if_recovered(db: Session, now: float) -> None:
    site = db.scalars(select(Event).where(Event.camera_id.is_(None), Event.type == "SITE_DOWN",
                                          Event.status == "open")).first()
    if not site:
        return
    still = db.scalar(select(func.count()).select_from(Event).where(
        Event.type == "OFFLINE", Event.status == "open",
        Event.camera_id.in_(site.data.get("cameras", []))))
    if not still:
        close_event(db, None, "SITE_DOWN", now)


def detect_flapping(db: Session, now: float) -> None:
    """Quedas repetidas viram uma ocorrência só de 'sinal oscilando'."""
    rows = db.execute(
        select(Event.camera_id, func.count()).where(
            Event.type == "OFFLINE", Event.started_at >= now - FLAP_WINDOW_S,
            Event.camera_id.isnot(None)).group_by(Event.camera_id)).all()
    for camera_id, count in rows:
        if count >= FLAP_COUNT:
            open_event(db, camera_id, "UNSTABLE", now,
                       detail=f"A câmera caiu {count} vezes na última hora.",
                       data={"drops": count})


def evaluate_stability(db: Session, camera_id: int, recent: List[Metric], ts: float) -> None:
    """Oscilação curta: reconexões e buracos que não chegam a derrubar a câmera."""
    last10 = recent[:10]
    last15 = recent[:15]
    if len(last10) < 10:
        return
    reconnects = sum(m.reconnects for m in last10)
    gaps = sum(m.gaps for m in last10)
    jitter = sum(m.fps_cv for m in last10) / len(last10)
    if reconnects >= 3 or gaps >= 10 or jitter > 0.5:
        open_event(db, camera_id, "UNSTABLE", ts, data={
            "reconnects": reconnects, "gaps": gaps, "jitter": round(jitter, 2)},
            detail=f"Nos últimos 10 minutos: {reconnects} reconexões e {gaps} falhas no vídeo.")
        return
    drops = db.scalar(select(func.count()).select_from(Event).where(
        Event.camera_id == camera_id, Event.type == "OFFLINE",
        Event.started_at >= ts - FLAP_WINDOW_S)) or 0
    clean = (len(last15) >= 15 and drops < FLAP_COUNT
             and all(m.reconnects == 0 and m.gaps <= 1 and m.uptime >= 0.9 for m in last15))
    if clean:
        close_event(db, camera_id, "UNSTABLE", ts)


def check_heartbeat(db: Session, now: float) -> None:
    last = kv_get(db, "agent").get("last_seen")
    if last is None:
        return
    if now - last > settings.heartbeat_timeout_s:
        open_event(db, None, "AGENT_OFFLINE", last)
        db.execute(update(Camera).where(Camera.state == "online").values(state="unknown"))
    else:
        close_event(db, None, "AGENT_OFFLINE", now)


def _build_message(db: Session, alert: Alert, recipient: Recipient) -> Outgoing:
    if alert.kind in {"digest", "test"}:
        text = alert.payload.get("text", "")
        return Outgoing(to=recipient.phone, kind=alert.kind, text=text,
                        params=alert.payload.get("params", [text]))
    event = db.get(Event, alert.event_id)
    camera = db.get(Camera, event.camera_id) if event.camera_id else None
    place = camera.name if camera else settings.site_name
    info = CATALOG[event.type]
    when = fmt_time(event.started_at)
    if alert.kind == "resolved":
        duration = fmt_duration((event.ended_at or time.time()) - event.started_at)
        text = (f"✅ *Normalizado: {event.title}*\n📍 {place}\n"
                f"Voltou ao normal depois de {duration}.")
        return Outgoing(to=recipient.phone, kind="resolved", text=text,
                        params=[event.title, place, duration], event_id=event.id)
    note = event.ai_note or event.detail
    reviewer = (event.data or {}).get("reviewed_name")
    if event.type == "MANUAL":
        note = f"Observação do operador{f' {reviewer}' if reviewer else ''}: {event.detail}"
    extra = ""
    if (event.data or {}).get("reviewed_by") == "operator" and event.type != "MANUAL":
        extra = (f"\n✔ Confirmado por {reviewer}, da equipe de monitoramento." if reviewer
                 else "\n✔ Confirmado pela equipe de monitoramento.")
    elif alert.payload.get("unreviewed"):
        extra = "\nAinda não revisado pelo operador."
    text = (f"{SEVERITY_ICON[event.severity]} *{event.title}*\n📍 {place}\n🕒 {when}\n\n"
            f"{note}\n{info.advice}{extra}\n\n"
            f"Responda *ok {event.id}* se viu, *falso {event.id}* se foi alarme falso "
            f"ou *silenciar 1h*.")
    image = event.snapshot if event.has_snapshot else None
    return Outgoing(to=recipient.phone, kind="alert", text=text,
                    params=[event.title, place, when, f"{note} {info.advice}{extra}"],
                    image=image, event_id=event.id)


def _maybe_describe(db: Session, event: Event) -> None:
    if (event.type in AI_DESCRIBED and event.has_snapshot and not event.ai_note
            and settings.anthropic_key):
        event.ai_note = vision_ai.describe(event.snapshot, event.title)


def dispatch(db: Session, now: float) -> int:
    provider = get_provider()
    due = db.scalars(select(Alert).where(Alert.status.in_(["pending", "review"]),
                                         Alert.not_before <= now)
                     .order_by(Alert.id).limit(30)).all()
    sent = 0
    for alert in due:
        recipient = db.get(Recipient, alert.recipient_id)
        event = db.get(Event, alert.event_id) if alert.event_id else None
        if recipient is None or not recipient.active:
            alert.status = "cancelled"
            continue
        if not provider.configured:
            alert.status, alert.last_error = "failed", "WhatsApp não configurado"
            continue
        if alert.status == "review":   # o operador não revisou a tempo: segue assim mesmo
            alert.payload = {**(alert.payload or {}), "unreviewed": True}
        if event is not None and alert.kind == "alert":
            _maybe_describe(db, event)
        try:
            alert.provider_id = provider.send(_build_message(db, alert, recipient))
            alert.status, alert.sent_at = "sent", now
            sent += 1
        except SendError as exc:
            alert.attempts += 1
            alert.last_error = str(exc)[:500]
            if not exc.retryable or alert.attempts >= MAX_ATTEMPTS:
                alert.status = "failed"
                log.error("Alerta %s não entregue: %s", alert.id, exc)
            else:
                alert.not_before = now + BACKOFF_S[alert.attempts - 1]
        except Exception as exc:  # rede, resposta inesperada do provedor
            alert.attempts += 1
            alert.last_error = f"{type(exc).__name__}: {exc}"[:500]
            if alert.attempts >= MAX_ATTEMPTS:
                alert.status = "failed"
            else:
                alert.not_before = now + BACKOFF_S[alert.attempts - 1]
        db.commit()
    return sent


def status_text(db: Session, now: float) -> str:
    cams = db.scalars(select(Camera).where(Camera.enabled.is_(True)).order_by(Camera.name)).all()
    online = sum(1 for c in cams if c.state == "online")
    open_events = db.scalars(select(Event).where(Event.status == "open")
                             .order_by(Event.started_at.desc()).limit(8)).all()
    lines = [f"📷 *{settings.site_name}*: {online} de {len(cams)} câmeras online."]
    if open_events:
        names = {c.id: c.name for c in cams}
        lines.append("\nEm aberto agora:")
        for e in open_events:
            lines.append(f"{SEVERITY_ICON[e.severity]} {e.id}. {e.title} ({names.get(e.camera_id, 'Local')})")
    else:
        lines.append("Nenhum problema em aberto.")
    mute_until = kv_get(db, "mute").get("until", 0)
    if mute_until > now:
        lines.append(f"\nAlertas silenciados até {fmt_time(mute_until)}.")
    return "\n".join(lines)


def digest_text(db: Session, now: float) -> tuple[str, list[str]]:
    since = now - 86400
    events = db.scalars(select(Event).where(Event.started_at >= since)).all()
    critical = sum(1 for e in events if e.severity == "critical")
    behavior = sum(1 for e in events if e.type.startswith("BEHAVIOR"))
    row = db.execute(select(func.avg(Metric.uptime)).where(Metric.ts >= since)).first()
    uptime = (row[0] if row and row[0] is not None else 0.0) * 100
    open_now = db.scalar(select(func.count()).select_from(Event).where(Event.status == "open")) or 0
    worst = db.execute(
        select(Camera.name, func.count(Event.id).label("n"))
        .join(Event, Event.camera_id == Camera.id).where(Event.started_at >= since)
        .group_by(Camera.name).order_by(func.count(Event.id).desc()).limit(1)).first()
    worst_txt = f"{worst[0]} ({worst[1]})" if worst else "nenhuma"
    text = (f"🗓️ *Resumo das últimas 24h*\n{settings.site_name}\n\n"
            f"Câmeras no ar: {uptime:.1f}% do tempo\n"
            f"Ocorrências: {len(events)} ({critical} críticas)\n"
            f"Comportamento fora do normal: {behavior}\n"
            f"Câmera com mais ocorrências: {worst_txt}\n"
            f"Em aberto agora: {open_now}")
    params = [settings.site_name, f"{uptime:.1f}%", str(len(events)), str(critical),
              str(behavior), worst_txt, str(open_now)]
    return text, params


def maybe_send_digest(db: Session, now: float) -> None:
    local = datetime.fromtimestamp(now, _tz)
    day = local.strftime("%Y-%m-%d")
    if local.hour != settings.digest_hour or kv_get(db, "digest").get("day") == day:
        return
    text, params = digest_text(db, now)
    for r in db.scalars(select(Recipient).where(Recipient.active.is_(True))):
        db.add(Alert(recipient_id=r.id, kind="digest", status="pending", not_before=0,
                     created_at=now, payload={"text": text, "params": params}))
    kv_set(db, "digest", {"day": day})


def apply_retention(db: Session, now: float) -> None:
    last = kv_get(db, "retention").get("at", 0)
    if now - last < 3600:
        return
    db.execute(delete(Metric).where(Metric.ts < now - settings.retention_metrics_days * 86400))
    db.execute(update(Event).where(Event.started_at < now - settings.retention_snapshots_days * 86400,
                                   Event.has_snapshot.is_(True))
               .values(snapshot=None, has_snapshot=False))
    db.execute(delete(Event).where(Event.started_at < now - settings.retention_events_days * 86400))
    db.execute(delete(Alert).where(Alert.created_at < now - settings.retention_events_days * 86400))
    kv_set(db, "retention", {"at": now})


def train_models(db: Session, now: float) -> None:
    last = kv_get(db, "training").get("at", 0)
    if now - last < 600:
        return
    for cam in db.scalars(select(Camera).where(Camera.enabled.is_(True))):
        if anomaly.needs_training(db, cam.id, now):
            anomaly.train_camera(db, cam.id, now)
    kv_set(db, "training", {"at": now})


def tick(db: Session, now: Optional[float] = None) -> None:
    now = now or time.time()
    check_heartbeat(db, now)
    detect_flapping(db, now)
    correlate_site_outage(db, now)
    close_site_outage_if_recovered(db, now)
    maybe_send_digest(db, now)
    train_models(db, now)
    apply_retention(db, now)
    db.commit()
    dispatch(db, now)
    db.commit()


# Comandos recebidos pelo WhatsApp ------------------------------------------------

HELP = ("Comandos:\n*status*: como estão as câmeras\n*ok 12*: marcar ocorrência como vista\n"
        "*falso 12*: avisar que foi alarme falso\n*silenciar 2h*: pausar avisos\n"
        "*reativar*: voltar a avisar")


def _parse_duration(token: str) -> Optional[timedelta]:
    token = token.strip().lower()
    try:
        if token.endswith("min"):
            return timedelta(minutes=int(token[:-3]))
        if token.endswith("m"):
            return timedelta(minutes=int(token[:-1]))
        if token.endswith("h"):
            return timedelta(hours=int(token[:-1]))
        if token.endswith("d"):
            return timedelta(days=int(token[:-1]))
        return timedelta(hours=int(token))
    except ValueError:
        return None


def register_false_alarm(db: Session, event: Event, role: str = "owner",
                         name: Optional[str] = None) -> None:
    """Alarme falso de comportamento deixa aquela câmera menos sensível."""
    apply_review(db, event, real=False, role=role, name=name)
    event.feedback = "false"
    event.acked_at = event.acked_at or time.time()
    if event.type.startswith("BEHAVIOR") and event.camera_id:
        camera = db.get(Camera, event.camera_id)
        camera.threshold_mult = min(round(camera.threshold_mult + 0.15, 2), 2.0)


def handle_command(db: Session, text: str, now: Optional[float] = None) -> str:
    now = now or time.time()
    raw = text.strip()
    if raw.startswith("ACK:"):
        raw = "ok " + raw[4:]
    elif raw.startswith("MUTE:"):
        raw = "silenciar " + raw[5:]
    parts = raw.lower().split()
    if not parts:
        return HELP
    cmd = parts[0]
    if cmd in {"status", "câmeras", "cameras", "situação", "situacao"}:
        return status_text(db, now)
    if cmd in {"ok", "ciente", "falso"}:
        if len(parts) < 2 or not parts[1].isdigit():
            return f"Diga o número da ocorrência. Exemplo: *{cmd} 12*."
        event = db.get(Event, int(parts[1]))
        if event is None:
            return f"Não encontrei a ocorrência {parts[1]}."
        if cmd == "falso":
            register_false_alarm(db, event)
            return f"Certo, marquei a ocorrência {event.id} como alarme falso."
        apply_review(db, event, real=True, role="owner")
        event.acked_at, event.feedback = now, event.feedback or "real"
        return f"Certo, ocorrência {event.id} marcada como vista."
    if cmd == "silenciar":
        delta = _parse_duration(parts[1]) if len(parts) > 1 else timedelta(hours=1)
        if delta is None or delta.total_seconds() <= 0:
            return "Não entendi o tempo. Exemplo: *silenciar 2h*."
        delta = min(delta, timedelta(days=7))
        mute = kv_get(db, "mute")
        mute = {**mute, "until": now + delta.total_seconds()}
        kv_set(db, "mute", mute)
        return (f"Avisos pausados até {fmt_time(mute['until'])}. "
                "Invasão e câmera tampada continuam chegando.")
    if cmd in {"reativar", "ativar"}:
        mute = kv_get(db, "mute")
        kv_set(db, "mute", {**mute, "until": 0})
        return "Avisos reativados."
    return HELP


def recipient_for(db: Session, phone: str) -> Optional[Recipient]:
    from .whatsapp import phones_match

    for r in db.scalars(select(Recipient).where(Recipient.active.is_(True),
                                                Recipient.can_command.is_(True))):
        if phones_match(r.phone, phone):
            return r
    return None


def enqueue_simple(db: Session, recipient: Recipient, kind: str, text: str,
                   params: Optional[List[str]] = None) -> Alert:
    alert = Alert(recipient_id=recipient.id, kind=kind, status="pending", not_before=0,
                  created_at=time.time(), payload={"text": text, "params": params or [text]})
    db.add(alert)
    return alert
