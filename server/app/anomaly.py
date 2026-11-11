"""IA de comportamento: aprende o padrão de cada câmera e aponta o que foge dele.

Duas camadas trabalham juntas:

1. Base sazonal: média e variância do movimento por meia hora, separando dia útil
   de fim de semana. Responde "isso é normal para uma terça às 15h?".
2. Isolation Forest: olha o minuto como um todo (movimento, tempo ativo, pessoas,
   veículos e hora do dia) e confirma combinações raras. É treinada por câmera,
   a partir do histórico, e ajuda a separar pico real de ruído.

Nada dispara enquanto a câmera ainda está aprendendo.
"""
from __future__ import annotations

import math
import pickle
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
from sklearn.ensemble import IsolationForest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import settings
from .db import BaselineBucket, Camera, CameraModel, Metric

MIN_SAMPLES = 60          # amostras por meia hora antes de confiar (cerca de 2 dias)
SIGMA_FLOOR = 0.30        # impede que variância quase zero (madrugada) gere alarme
Z_SPIKE = 3.5
Z_QUIET = 2.5
SPIKE_WINDOW = 5          # precisa aparecer em 3 dos últimos 5 minutos
SPIKE_MIN_HITS = 3
QUIET_WINDOW = 20         # 20 minutos seguidos anormalmente parados
MIN_MOTION_PCT = 0.30     # abaixo disso é ruído de sensor
MIN_EXPECTED_FOR_QUIET = 1.0
TRAIN_EVERY_S = 6 * 3600
TRAIN_MIN_ROWS = 2880     # 2 dias de minutos
TRAIN_DAYS = 21
BUCKETS = 96              # 48 meias horas * (dia útil, fim de semana)

_tz = ZoneInfo(settings.timezone)
_model_cache: Dict[int, Tuple[float, IsolationForest]] = {}


def activity_index(motion_pct: float) -> float:
    """Escala logarítmica: 0,5% a 1% importa mais do que 40% a 40,5%."""
    return math.log1p(max(motion_pct, 0.0) * 10.0)


def activity_from_index(x: float) -> float:
    return max(math.expm1(x) / 10.0, 0.0)


def bucket_of(ts: float) -> int:
    dt = datetime.fromtimestamp(ts, _tz)
    slot = dt.hour * 2 + (1 if dt.minute >= 30 else 0)
    return slot + (48 if dt.weekday() >= 5 else 0)


def _neighbors(bucket: int) -> List[int]:
    base = 48 if bucket >= 48 else 0
    slot = bucket - base
    return [base + (slot - 1) % 48, bucket, base + (slot + 1) % 48]


def _expected(buckets: Dict[int, BaselineBucket], bucket: int) -> Optional[Tuple[float, float]]:
    """Média e desvio do horário, suavizados com as meias horas vizinhas."""
    center = buckets.get(bucket)
    if center is None or center.n < MIN_SAMPLES:
        return None
    total, mean, var = 0.0, 0.0, 0.0
    for b, weight in zip(_neighbors(bucket), (0.25, 0.5, 0.25)):
        row = buckets.get(b)
        if row is None or row.n < MIN_SAMPLES / 2:
            continue
        total += weight
        mean += weight * row.mean
        var += weight * row.var
    mean, var = mean / total, var / total
    return mean, max(math.sqrt(max(var, 0.0)), SIGMA_FLOOR)


def _update_bucket(row: BaselineBucket, x: float, z: Optional[float]) -> None:
    # Um pico muito anormal não deve ensinar à IA que aquilo é o novo normal.
    if z is not None and abs(z) > 5:
        alpha = 0.002
    else:
        alpha = max(1.0 / (row.n + 1), 0.005)  # memória de cerca de uma semana
    delta = x - row.mean
    row.mean += alpha * delta
    row.var = (1 - alpha) * (row.var + alpha * delta * delta)
    row.n += 1


def _vector(m: Metric) -> List[float]:
    dt = datetime.fromtimestamp(m.ts, _tz)
    minute = dt.hour * 60 + dt.minute
    angle = 2 * math.pi * minute / 1440
    return [
        activity_index(m.motion), m.active, float(m.persons), float(m.vehicles),
        math.sin(angle), math.cos(angle), 1.0 if dt.weekday() >= 5 else 0.0,
    ]


def train_camera(db: Session, camera_id: int, now: Optional[float] = None) -> bool:
    now = now or time.time()
    rows = db.scalars(
        select(Metric).where(Metric.camera_id == camera_id,
                             Metric.ts >= now - TRAIN_DAYS * 86400,
                             Metric.uptime > 0.5)
    ).all()
    if len(rows) < TRAIN_MIN_ROWS:
        return False
    X = np.array([_vector(m) for m in rows], dtype=np.float32)
    forest = IsolationForest(n_estimators=120, max_samples=min(4096, len(X)),
                             contamination=0.01, random_state=7, n_jobs=1).fit(X)
    db.execute(delete(CameraModel).where(CameraModel.camera_id == camera_id))
    db.add(CameraModel(camera_id=camera_id, trained_at=now, n_rows=len(rows),
                       blob=pickle.dumps(forest)))
    _model_cache[camera_id] = (now, forest)
    return True


def _load_model(db: Session, camera_id: int) -> Optional[IsolationForest]:
    row = db.get(CameraModel, camera_id)
    if row is None:
        _model_cache.pop(camera_id, None)
        return None
    cached = _model_cache.get(camera_id)
    if cached and cached[0] == row.trained_at:
        return cached[1]
    # O blob só é gravado por train_camera, neste mesmo servidor.
    forest = pickle.loads(row.blob)  # noqa: S301
    _model_cache[camera_id] = (row.trained_at, forest)
    return forest


def needs_training(db: Session, camera_id: int, now: float) -> bool:
    row = db.get(CameraModel, camera_id)
    return row is None or now - row.trained_at >= TRAIN_EVERY_S


def load_buckets(db: Session, camera_id: int) -> Dict[int, BaselineBucket]:
    return {b.bucket: b for b in db.scalars(
        select(BaselineBucket).where(BaselineBucket.camera_id == camera_id))}


def learning_progress(buckets: Dict[int, BaselineBucket], ts: float) -> float:
    """Quanto a câmera já aprendeu do horário atual, de 0 a 1."""
    row = buckets.get(bucket_of(ts))
    return min((row.n if row else 0) / MIN_SAMPLES, 1.0)


def score_minute(db: Session, camera: Camera, metric: Metric,
                 buckets: Optional[Dict[int, BaselineBucket]] = None) -> None:
    """Calcula o quão anormal é o minuto e atualiza o aprendizado."""
    if metric.uptime < 0.5:          # sem imagem o movimento não quer dizer nada
        metric.z_activity, metric.iforest = None, None
        return
    buckets = buckets if buckets is not None else load_buckets(db, camera.id)
    idx = bucket_of(metric.ts)
    x = activity_index(metric.motion)
    expected = _expected(buckets, idx)
    z = (x - expected[0]) / expected[1] if expected else None
    metric.z_activity = round(z, 3) if z is not None else None
    forest = _load_model(db, camera.id)
    if forest is not None:
        metric.iforest = round(float(forest.decision_function(
            np.array([_vector(metric)], dtype=np.float32))[0]), 4)
    row = buckets.get(idx)
    if row is None:
        row = BaselineBucket(camera_id=camera.id, bucket=idx, n=0, mean=x, var=0.0)
        db.add(row)
        buckets[idx] = row
    _update_bucket(row, x, z)


def evaluate(recent: List[Metric], threshold_mult: float,
             expected_mean: Optional[float] = None) -> Dict[str, Optional[bool]]:
    """Decide abrir/fechar ocorrências a partir dos últimos minutos (mais novo primeiro).

    Devolve {"BEHAVIOR_SPIKE": True/False/None, "BEHAVIOR_QUIET": ...}; None mantém
    o estado atual, para que a ocorrência não fique abrindo e fechando.
    """
    result: Dict[str, Optional[bool]] = {"BEHAVIOR_SPIKE": None, "BEHAVIOR_QUIET": None}
    scored = [m for m in recent if m.z_activity is not None]
    if len(scored) < SPIKE_WINDOW:
        return result

    z_spike = Z_SPIKE * threshold_mult
    window = scored[:SPIKE_WINDOW]
    hits = [m for m in window
            if m.z_activity >= z_spike and m.motion >= MIN_MOTION_PCT]
    forest_known = any(m.iforest is not None for m in window)
    forest_agrees = any(m.iforest is not None and m.iforest < 0 for m in window)
    strong = any(m.z_activity >= z_spike * 1.5 for m in hits)
    if len(hits) >= SPIKE_MIN_HITS and (not forest_known or forest_agrees or strong):
        result["BEHAVIOR_SPIKE"] = True
    elif all(m.z_activity < 1.5 for m in window):
        result["BEHAVIOR_SPIKE"] = False

    z_quiet = Z_QUIET * threshold_mult
    long_window = scored[:QUIET_WINDOW]
    if (len(long_window) >= QUIET_WINDOW and expected_mean is not None
            and activity_from_index(expected_mean) >= MIN_EXPECTED_FOR_QUIET
            and all(m.z_activity <= -z_quiet for m in long_window)):
        result["BEHAVIOR_QUIET"] = True
    elif all(m.z_activity > -1.0 for m in window):
        result["BEHAVIOR_QUIET"] = False
    return result


def expected_band(buckets: Dict[int, BaselineBucket],
                  ts: float) -> Optional[Tuple[float, float, float]]:
    """(mínimo, esperado, máximo) em % do quadro para o gráfico."""
    e = _expected(buckets, bucket_of(ts))
    if e is None:
        return None
    mean, sigma = e
    return (activity_from_index(mean - 2 * sigma), activity_from_index(mean),
            activity_from_index(mean + 2 * sigma))
