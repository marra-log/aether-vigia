"""Regras de segurança: área protegida em horário definido e permanência longa."""
from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Deque, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .detector import Detection, point_in_polygon
from .health import Transition


def in_schedule(start: Optional[str], end: Optional[str], days: Sequence[int], ts: float,
                tz: ZoneInfo) -> bool:
    if not start or not end:
        return False
    dt = datetime.fromtimestamp(ts, tz)
    now = dt.hour * 60 + dt.minute
    s = int(start[:2]) * 60 + int(start[3:5])
    e = int(end[:2]) * 60 + int(end[3:5])
    if s <= e:
        return s <= now < e and dt.weekday() in days
    if now >= s:
        return dt.weekday() in days
    return now < e and (dt.weekday() - 1) % 7 in days


class SecurityRules:
    """Exige confirmação em 2 de 3 detecções, para ignorar erro de um quadro só."""

    CLEAR_AFTER_S = 45.0

    def __init__(self, cfg: dict, tz: ZoneInfo) -> None:
        self.tz = tz
        self.configure(cfg)
        self.hits: Deque[bool] = deque(maxlen=3)
        self.intrusion_active = False
        self.last_person_in_zone: Optional[float] = None
        self.presence_since: Optional[float] = None
        self.last_person: Optional[float] = None
        self.loitering_active = False

    def configure(self, cfg: dict) -> None:
        self.start, self.end = cfg.get("armed_start"), cfg.get("armed_end")
        self.days = cfg.get("armed_days") or list(range(7))
        self.zones = cfg.get("zones") or []
        self.loiter_s = cfg.get("loiter_seconds", 180)

    def _in_zone(self, d: Detection) -> bool:
        return not self.zones or any(point_in_polygon(d.foot, z) for z in self.zones)

    def update(self, detections: List[Detection], ts: float) -> List[Transition]:
        out: List[Transition] = []
        persons = [d for d in detections if d.label == "person"]
        armed = in_schedule(self.start, self.end, self.days, ts, self.tz)
        in_zone = [d for d in persons if self._in_zone(d)]

        self.hits.append(armed and bool(in_zone))
        if in_zone:
            self.last_person_in_zone = ts
        if sum(self.hits) >= 2 and not self.intrusion_active:
            self.intrusion_active = True
            out.append(Transition("INTRUSION", "open", ts, {
                "score": round(max(d.score for d in in_zone) if in_zone else 0.0, 2)}))
        if self.intrusion_active and (self.last_person_in_zone is None
                                      or ts - self.last_person_in_zone > self.CLEAR_AFTER_S):
            self.intrusion_active = False
            self.hits.clear()
            out.append(Transition("INTRUSION", "close", ts, {}))

        if persons:
            self.last_person = ts
            self.presence_since = self.presence_since if self.presence_since is not None else ts
            if not self.loitering_active and ts - self.presence_since >= self.loiter_s:
                self.loitering_active = True
                out.append(Transition("LOITERING", "open", ts,
                                      {"seconds": int(ts - self.presence_since)}))
        elif self.last_person is not None and ts - self.last_person > self.CLEAR_AFTER_S:
            self.presence_since, self.last_person = None, None
            if self.loitering_active:
                self.loitering_active = False
                out.append(Transition("LOITERING", "close", ts, {}))
        return out
