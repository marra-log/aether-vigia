"""Saúde do sinal: tudo que dá para saber olhando só para os quadros.

Cada problema só abre depois de persistir e só fecha depois de sumir por um tempo,
para não virar uma enxurrada de alertas. O relógio é o `ts` recebido em update(),
o que permite testar horas de vídeo em segundos.

Dia e noite têm referências separadas: quando a câmera entra no infravermelho a
imagem muda de natureza, e comparar uma com a outra geraria alarme falso toda noite.

Dica de instalação: mantenha o relógio na imagem da câmera. Ele garante que os
quadros mudem sempre, e assim uma cena parada nunca é confundida com imagem travada.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

import cv2
import numpy as np

W, H = 320, 180


@dataclass
class Rule:
    open_after: float
    close_after: float


RULES: Dict[str, Rule] = {
    "FROZEN": Rule(30, 6),
    "BLACK": Rule(10, 6),
    "OBSTRUCTED": Rule(8, 8),
    "BLUR": Rule(60, 30),
    "MOVED": Rule(20, 20),
    "FLICKER": Rule(20, 30),
}
RELEARN_AFTER_S = {"MOVED": 1800.0, "BLUR": 86400.0}  # depois disso, é o novo normal


@dataclass
class IssueState:
    active: bool = False
    since: Optional[float] = None
    clear_since: Optional[float] = None


@dataclass
class Reference:
    """O que é normal para esta câmera em um modo de imagem (cor ou infravermelho)."""
    started: float
    gradient: np.ndarray
    sharp: float
    edges: float
    contrast: float


@dataclass
class Features:
    ts: float
    brightness: float
    contrast: float
    sharpness: float
    edges: float
    delta: float
    max_delta: float
    motion: float           # % do quadro em movimento
    ncc: float              # semelhança com o enquadramento de referência
    flicker: float
    sharp_ratio: float
    mode: str
    conditions: Dict[str, bool] = field(default_factory=dict)


@dataclass
class Transition:
    type: str
    state: str              # open|close
    ts: float
    data: dict


def _gradient(gray: np.ndarray) -> np.ndarray:
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    gx = cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.magnitude(gx, gy)


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    a0, b0 = a - a.mean(), b - b.mean()
    denom = float(np.sqrt((a0 * a0).sum() * (b0 * b0).sum()))
    return float((a0 * b0).sum() / denom) if denom > 1e-6 else 0.0


def _mode(small: np.ndarray) -> str:
    if small.ndim != 3:
        return "mono"
    saturation = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[:, :, 1]
    return "mono" if float(saturation.mean()) < 6.0 else "color"


class SignalMonitor:
    """Recebe quadros e devolve medidas e mudanças de estado."""

    WARMUP_S = 15.0        # tempo para formar a referência
    REF_TAU_S = 1800.0     # a referência acompanha mudanças lentas (sol, sombra)

    def __init__(self) -> None:
        self.issues = {name: IssueState() for name in RULES}
        self.prev: Optional[np.ndarray] = None
        self.background: Optional[np.ndarray] = None
        self.refs: Dict[str, Reference] = {}
        self.last_ts: Optional[float] = None
        self.mode: Optional[str] = None
        self.brightness_hist: Deque[Tuple[float, float]] = deque(maxlen=120)
        self.deltas: Deque[Tuple[float, float]] = deque(maxlen=8)

    def _flicker(self, ts: float) -> float:
        """Amplitude da oscilação de brilho, se ela vai e volta repetidamente."""
        values = np.array([b for t, b in self.brightness_hist if t >= ts - 30.0])
        if len(values) < 20:
            return 0.0
        centered = values - np.convolve(values, np.ones(9) / 9, mode="same")
        centered = centered[4:-4]
        active = centered[np.abs(centered) > 2.5]
        if len(active) < 8:
            return 0.0
        flips = int(np.sum(np.sign(active[1:]) != np.sign(active[:-1])))
        return round(float(np.std(centered)), 2) if flips >= 8 else 0.0

    def update(self, frame: np.ndarray, ts: float) -> Tuple[Features, List[Transition]]:
        small = cv2.resize(frame, (W, H), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
        dt = 0.5 if self.last_ts is None else max(ts - self.last_ts, 1e-3)
        self.last_ts = ts
        brightness = float(gray.mean())
        contrast = float(gray.std())
        # Imagem lisa (lente tampada) não diz se é dia ou noite: mantém o modo atual.
        if self.mode is None or contrast >= 12:
            self.mode = _mode(small)
        mode = self.mode
        ref = self.refs.get(mode)
        sharpness = float(cv2.Laplacian(gray, cv2.CV_32F).var())
        edges = float(np.mean(cv2.Canny(gray, 60, 140) > 0))
        self.brightness_hist.append((ts, brightness))
        flicker = self._flicker(ts)

        delta, max_delta, motion = 0.0, 255.0, 0.0
        if self.prev is not None:
            diff = cv2.absdiff(gray, self.prev)
            delta, max_delta = float(diff.mean()), float(diff.max())
        if self.background is None:
            self.background = gray.astype(np.float32)
        else:
            fg = cv2.absdiff(gray, cv2.convertScaleAbs(self.background))
            mask = (fg > 28).astype(np.uint8)
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
            ratio = float(mask.mean())
            # Mudança brusca de luz acende quase o quadro todo: não é movimento.
            motion = 0.0 if ratio > 0.75 else ratio * 100
            cv2.accumulateWeighted(gray.astype(np.float32), self.background, min(1.0, dt / 20.0))
        self.prev = gray

        gradient = _gradient(gray)
        ncc = 1.0 if ref is None else _ncc(gradient, ref.gradient)
        sharp_ratio = sharpness / ref.sharp if ref and ref.sharp > 1e-3 else 1.0

        cond = self._conditions(ref, brightness, contrast, edges, delta, max_delta, sharp_ratio,
                                ncc, flicker, ts)
        transitions = self._apply(cond, ts, {
            "brightness": round(brightness, 1), "contrast": round(contrast, 1),
            "sharp_ratio": round(sharp_ratio, 2), "ncc": round(ncc, 2),
            "flicker": flicker, "mode": mode,
        })
        self._learn(mode, gradient, sharpness, edges, contrast, dt, cond, ts)
        self._relearn_if_stale(mode, gradient, sharpness, edges, contrast, ts)

        return Features(ts, brightness, contrast, sharpness, edges, delta, max_delta, motion, ncc,
                        flicker, sharp_ratio, mode, cond), transitions

    def _conditions(self, ref: Optional[Reference], brightness: float, contrast: float,
                    edges: float, delta: float, max_delta: float, sharp_ratio: float, ncc: float,
                    flicker: float, ts: float) -> Dict[str, bool]:
        warm = ref is not None and ts - ref.started >= self.WARMUP_S
        # Local escuro por natureza (sem infravermelho) não é falha.
        black = (ref is not None and brightness < 12 and contrast < 6 and ref.contrast > 8)
        obstructed = False
        if warm and not black:
            edge_gone = ref.edges > 0.005 and edges < ref.edges * 0.25
            obstructed = edge_gone and contrast < max(ref.contrast * 0.5, 6)
        frozen = self._looks_frozen(delta, max_delta) and not black and not obstructed
        blur = moved = False
        if warm and not black and not obstructed and not frozen:
            blur = sharp_ratio < 0.3
            moved = not blur and ncc < 0.35
        return {"FROZEN": frozen, "BLACK": black, "OBSTRUCTED": obstructed, "BLUR": blur,
                "MOVED": moved, "FLICKER": flicker > 0}

    def _looks_frozen(self, delta: float, max_delta: float) -> bool:
        """Olha alguns quadros de uma vez: o codificador da câmera gera pequenos saltos
        periódicos mesmo com a imagem parada, e um quadro só não pode zerar a contagem."""
        self.deltas.append((delta, max_delta))
        if len(self.deltas) < self.deltas.maxlen:
            return False
        means, peaks = zip(*self.deltas)
        return float(np.median(means)) < 0.2 and float(np.percentile(peaks, 75)) <= 16.0

    def _apply(self, cond: Dict[str, bool], ts: float, data: dict) -> List[Transition]:
        out: List[Transition] = []
        for name, on in cond.items():
            state, rule = self.issues[name], RULES[name]
            if on:
                state.clear_since = None
                state.since = state.since if state.since is not None else ts
                if not state.active and ts - state.since >= rule.open_after:
                    state.active = True
                    out.append(Transition(name, "open", state.since, dict(data)))
            elif state.active:
                state.clear_since = state.clear_since if state.clear_since is not None else ts
                if ts - state.clear_since >= rule.close_after:
                    state.active, state.since, state.clear_since = False, None, None
                    out.append(Transition(name, "close", ts, dict(data)))
            else:
                state.since = None
        return out

    def _learn(self, mode: str, gradient: np.ndarray, sharpness: float, edges: float,
               contrast: float, dt: float, cond: Dict[str, bool], ts: float) -> None:
        if any(s.active for s in self.issues.values()) or any(cond.values()):
            return
        ref = self.refs.get(mode)
        if ref is None:
            self.refs[mode] = Reference(ts, gradient.copy(), sharpness, edges, contrast)
            return
        tau = 20.0 if ts - ref.started < self.WARMUP_S else self.REF_TAU_S
        alpha = min(dt / tau, 1.0)
        cv2.accumulateWeighted(gradient, ref.gradient, alpha)
        ref.sharp += alpha * (sharpness - ref.sharp)
        ref.edges += alpha * (edges - ref.edges)
        ref.contrast += alpha * (contrast - ref.contrast)

    def _relearn_if_stale(self, mode: str, gradient: np.ndarray, sharpness: float, edges: float,
                          contrast: float, ts: float) -> None:
        for name, after in RELEARN_AFTER_S.items():
            state = self.issues[name]
            if state.active and state.since is not None and ts - state.since >= after:
                self.refs[mode] = Reference(ts - self.WARMUP_S, gradient.copy(), sharpness, edges,
                                            contrast)
