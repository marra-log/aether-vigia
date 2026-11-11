"""Simula 6 semanas de uma câmera e mede a qualidade da IA de comportamento."""
import json
import math
import random
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select

from app import anomaly
from app.db import BaselineBucket, Camera, CameraModel, Metric, SessionLocal, init_db

TZ = ZoneInfo("America/Sao_Paulo")
START = int(datetime(2026, 9, 7, 0, 0, tzinfo=TZ).timestamp())


def normal_motion(ts, rnd):
    dt = datetime.fromtimestamp(ts, TZ)
    hour = dt.hour + dt.minute / 60
    weekend = dt.weekday() >= 5
    if 8 <= hour < 18:
        level = (1.2 if weekend else 4.0) * (0.8 + 0.4 * math.sin((hour - 8) / 10 * math.pi))
    else:
        level = 0.05
    return max(level * rnd.lognormvariate(0, 0.55), 0.0)


def simulate(db, cam, days, inject=None, seed=1):
    """inject: lista de (início, duração_min, fator). Devolve o número de dias simulados."""
    rnd = random.Random(seed)
    buckets = anomaly.load_buckets(db, cam.id)
    recent, openings, spike_open = [], [], False
    quiet_open = False
    for i in range(days * 1440):
        ts = START + i * 60
        motion = normal_motion(ts, rnd)
        for begin, minutes, value in inject or []:
            if begin <= ts < begin + minutes * 60:
                motion = value(motion) if callable(value) else value
        m = Metric(camera_id=cam.id, ts=ts, uptime=1.0, motion=motion, active=min(motion / 5, 1.0),
                   persons=0, vehicles=0)
        anomaly.score_minute(db, cam, m, buckets)
        recent.insert(0, m)
        del recent[anomaly.QUIET_WINDOW:]
        b = buckets.get(anomaly.bucket_of(ts))
        verdict = anomaly.evaluate(recent, cam.threshold_mult, b.mean if b else None)
        if verdict["BEHAVIOR_SPIKE"] and not spike_open:
            spike_open = True
            openings.append(("BEHAVIOR_SPIKE", ts))
        elif verdict["BEHAVIOR_SPIKE"] is False:
            spike_open = False
        if verdict["BEHAVIOR_QUIET"] and not quiet_open:
            quiet_open = True
            openings.append(("BEHAVIOR_QUIET", ts))
        elif verdict["BEHAVIOR_QUIET"] is False:
            quiet_open = False
    db.commit()
    return openings


@pytest.fixture()
def cam():
    init_db()
    with SessionLocal() as db:
        db.execute(delete(BaselineBucket))
        db.execute(delete(Camera))
        db.commit()
        c = Camera(name="Teste", created_at=0)
        db.add(c)
        db.commit()
        yield db, c


def test_nada_dispara_enquanto_aprende(cam):
    db, c = cam
    assert simulate(db, c, 2) == []


def test_precisao_e_cobertura(cam):
    db, c = cam
    # 3 semanas de aprendizado + 3 semanas com anomalias conhecidas
    day = 86400
    night = lambda d: START + d * day + 2 * 3600 + 900          # terça ou qualquer dia, 2h15
    spikes = [(night(d), 12, lambda m: 6.0) for d in (22, 26, 30, 34, 38)]
    quiet = [(START + d * day + 13 * 3600, 60, lambda m: 0.0) for d in (23, 29, 37)]   # dias úteis
    openings = simulate(db, c, 42, inject=spikes + quiet)
    after = [o for o in openings if o[1] >= START + 21 * day]

    def matched(begin, minutes, kind):
        return any(k == kind and begin <= t < begin + (minutes + 8) * 60 for k, t in after)

    found_spikes = sum(matched(b, m, "BEHAVIOR_SPIKE") for b, m, _ in spikes)
    found_quiet = sum(matched(b, m, "BEHAVIOR_QUIET") for b, m, _ in quiet)
    known = spikes + quiet
    false_alarms = [o for o in after if not any(b <= o[1] < b + (m + 8) * 60 for b, m, _ in known)]
    print(json.dumps({"picos": f"{found_spikes}/5", "paradas": f"{found_quiet}/3",
                      "alarmes_falsos_em_21_dias": len(false_alarms)}))
    assert found_spikes == 5
    assert found_quiet == 3
    assert len(false_alarms) <= 3


def test_isolation_forest_treina_e_pontua(cam):
    db, c = cam
    simulate(db, c, 8)
    rows = db.scalars(select(Metric).where(Metric.camera_id == c.id)).all()
    if not rows:
        for i in range(4000):
            db.add(Metric(camera_id=c.id, ts=START + i * 60, motion=1.0, active=0.2, uptime=1.0))
        db.commit()
    now = START + 3 * 86400
    assert anomaly.train_camera(db, c.id, now)
    assert db.get(CameraModel, c.id).n_rows >= anomaly.TRAIN_MIN_ROWS
    db.execute(delete(Metric))
    db.commit()


@pytest.mark.slow
def test_as_duas_camadas_juntas(cam):
    """Com a Isolation Forest ligada, os picos continuam aparecendo e sem alarme falso."""
    db, c = cam
    day = 86400
    rnd = random.Random(2)
    rows = [Metric(camera_id=c.id, ts=START + i * 60, uptime=1.0,
                   motion=(m := normal_motion(START + i * 60, rnd)), active=min(m / 5, 1.0),
                   persons=0, vehicles=0) for i in range(14 * 1440)]
    db.add_all(rows)
    db.commit()
    assert anomaly.train_camera(db, c.id, START + 14 * day)
    db.execute(delete(Metric))
    db.commit()
    spikes = [(START + d * day + 2 * 3600 + 900, 12, lambda m: 6.0) for d in (15, 18, 20)]
    openings = simulate(db, c, 21, inject=spikes, seed=9)
    late = [o for o in openings if o[1] >= START + 14 * day]
    found = sum(any(k == "BEHAVIOR_SPIKE" and b <= t < b + 20 * 60 for k, t in late) for b, _, _ in spikes)
    false_alarms = [o for o in late if not any(b <= o[1] < b + 20 * 60 for b, _, _ in spikes)]
    print(json.dumps({"com_forest_picos": f"{found}/3", "alarmes_falsos_em_7_dias": len(false_alarms)}))
    assert found == 3 and len(false_alarms) <= 1
    db.execute(delete(CameraModel))
    db.commit()
