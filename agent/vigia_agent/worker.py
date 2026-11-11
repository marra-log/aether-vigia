"""Uma câmera, uma thread: lê o vídeo, analisa e entrega ao enviador."""
from __future__ import annotations

import base64
import logging
import os
import threading
import time
from collections import deque
from typing import Deque, List, Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import cv2
import numpy as np

from .detector import Detection, Detector
from .rules import SecurityRules
from .health import SignalMonitor, Transition
from .uplink import Uplink

log = logging.getLogger("vigia.camera")

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
OFFLINE_AFTER_S = 30.0
OFFLINE_CLOSE_AFTER_S = 10.0
THUMB_EVERY_S = 300.0
DETECT_MIN_INTERVAL_S = 1.0
DETECT_MIN_MOTION = 0.15
STREAM_SOURCES = {"rtsp", "rtsps", "http", "https"}


def source_allowed(url: str, allow_files: bool) -> bool:
    if not url:
        return False
    scheme = urlsplit(url).scheme.lower()
    return scheme in STREAM_SOURCES or (allow_files and scheme in {"", "file"})


def encode_jpeg(frame: np.ndarray, width: int = 960, quality: int = 75) -> bytes:
    h, w = frame.shape[:2]
    if w > width:
        frame = cv2.resize(frame, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)
    ok, data = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return data.tobytes() if ok else b""


class MinuteAccumulator:
    def __init__(self) -> None:
        self.minute: Optional[int] = None
        self.reset()

    def reset(self) -> None:
        self.samples = 0
        self.expected = 0.0
        self.brightness: List[float] = []
        self.contrast: List[float] = []
        self.sharp: List[float] = []
        self.flicker = 0.0
        self.motion: List[float] = []
        self.grab_counts: List[float] = []
        self.gaps = 0
        self.reconnects = 0
        self.persons = 0
        self.vehicles = 0

    def build(self, sample_fps: float) -> Optional[dict]:
        if self.minute is None:
            return None
        expected = 60 * sample_fps
        motion = np.array(self.motion) if self.motion else np.zeros(1)
        fps = np.array(self.grab_counts) if self.grab_counts else np.zeros(1)
        mean = float(fps.mean())
        return {
            "ts": self.minute, "uptime": round(min(self.samples / expected, 1.0), 3),
            "fps": round(mean, 2), "fps_cv": round(float(fps.std() / mean) if mean > 0 else 0.0, 3),
            "gaps": self.gaps, "reconnects": self.reconnects,
            "brightness": round(float(np.mean(self.brightness)) if self.brightness else 0.0, 2),
            "contrast": round(float(np.mean(self.contrast)) if self.contrast else 0.0, 2),
            "sharp_ratio": round(float(np.mean(self.sharp)) if self.sharp else 1.0, 3),
            "flicker": self.flicker, "motion": round(float(motion.mean()), 3),
            "motion_p95": round(float(np.percentile(motion, 95)), 3),
            "active": round(float(np.mean(motion > 0.3)), 3),
            "persons": self.persons, "vehicles": self.vehicles,
        }


class CameraWorker(threading.Thread):
    def __init__(self, cfg: dict, uplink: Uplink, detector: Optional[Detector], tz: ZoneInfo,
                 replay: bool = False, allow_files: bool = False) -> None:
        super().__init__(daemon=True, name=f"cam-{cfg['id']}")
        self.cfg = cfg
        self.cam_id = cfg["id"]
        self.uplink = uplink
        self.detector = detector
        self.replay = replay
        self.allow_files = allow_files
        self.stop_flag = threading.Event()
        self.state = "unknown"
        self.monitor = SignalMonitor()
        self.rules = SecurityRules(cfg, tz)
        self.accumulator = MinuteAccumulator()
        self.latest_frame: Optional[np.ndarray] = None
        self.last_thumb = 0.0
        self.last_detect = 0.0
        self.offline_open = False
        self.last_frame_clock: Optional[float] = None
        self.started_clock = time.time()
        self.grabs_this_second = 0
        self.grab_second: Optional[int] = None
        self.frame_index = 0

    def stop(self) -> None:
        self.stop_flag.set()

    def update_config(self, cfg: dict) -> None:
        self.cfg = cfg
        self.rules.configure(cfg)

    # Tempo -----------------------------------------------------------------------
    def _replay_ts(self, fps: float) -> float:
        return self.cfg.get("replay_start", time.time()) + self.frame_index / fps

    # Saída -----------------------------------------------------------------------
    def _emit(self, transition: Transition, frame: Optional[np.ndarray]) -> None:
        body = {"camera_id": self.cam_id, "type": transition.type, "state": transition.state,
                "ts": transition.ts, "data": transition.data}
        if transition.state == "open" and frame is not None:
            jpeg = encode_jpeg(frame)
            if jpeg:
                body["snapshot_b64"] = base64.b64encode(jpeg).decode()
        self.uplink.send_event(body)

    def _flush_minute(self, minute: int) -> None:
        acc = self.accumulator
        if acc.minute is not None and acc.minute != minute:
            row = acc.build(float(self.cfg.get("sample_fps", 2.0)))
            if row:
                self.uplink.send_metric(self.cam_id, row, lambda: self.latest_frame)
            acc.reset()
        acc.minute = minute

    # Análise ---------------------------------------------------------------------
    def process(self, frame: np.ndarray, ts: float) -> None:
        self.latest_frame = frame
        self._flush_minute(int(ts // 60) * 60)
        features, transitions = self.monitor.update(frame, ts)
        acc = self.accumulator
        acc.samples += 1
        acc.brightness.append(features.brightness)
        acc.contrast.append(features.contrast)
        acc.sharp.append(min(features.sharp_ratio, 3.0))
        acc.motion.append(features.motion)
        acc.flicker = max(acc.flicker, features.flicker)
        for t in transitions:
            self._emit(t, frame)

        healthy = not any(s.active for s in self.monitor.issues.values())
        elapsed = ts - self.last_detect
        person_around = self.rules.presence_since is not None
        run_detector = (self.detector is not None and healthy and (
            (features.motion >= DETECT_MIN_MOTION and elapsed >= DETECT_MIN_INTERVAL_S)
            or (person_around and elapsed >= 5.0)))   # pessoa parada não gera movimento
        if run_detector:
            self.last_detect = ts
            detections: List[Detection] = self.detector.detect(frame)
            acc.persons = max(acc.persons, sum(d.label == "person" for d in detections))
            acc.vehicles = max(acc.vehicles, sum(d.label == "vehicle" for d in detections))
            for t in self.rules.update(detections, ts):
                self._emit(t, frame)
        elif elapsed >= 5.0:
            self.last_detect = ts
            for t in self.rules.update([], ts):
                self._emit(t, frame)

        if ts - self.last_thumb >= THUMB_EVERY_S or self.last_thumb == 0:
            self.last_thumb = ts
            jpeg = encode_jpeg(frame, width=640, quality=60)
            if jpeg:
                self.uplink.send_thumb(self.cam_id, jpeg)

    def _mark_down(self, ts: float) -> None:
        if not self.offline_open:
            self.offline_open = True
            self.state = "offline"
            self.uplink.send_event({"camera_id": self.cam_id, "type": "OFFLINE", "state": "open",
                                    "ts": ts, "data": {}})

    def _mark_up(self, ts: float) -> None:
        self.state = "online"
        if self.offline_open:
            self.offline_open = False
            self.uplink.send_event({"camera_id": self.cam_id, "type": "OFFLINE", "state": "close",
                                    "ts": ts, "data": {}})

    # Laço principal --------------------------------------------------------------
    def run(self) -> None:
        url = self.cfg.get("url", "")
        if not source_allowed(url, self.allow_files):
            log.error("Câmera %s: endereço não permitido", self.cam_id)
            self.state = "offline"
            return
        if self.replay:
            self._run_replay(url)
        else:
            self._run_live(url)

    def _open(self, url: str) -> cv2.VideoCapture:
        return cv2.VideoCapture(url, cv2.CAP_FFMPEG, [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000])

    def _run_replay(self, url: str) -> None:
        cap = self._open(url)
        fps = cap.get(cv2.CAP_PROP_FPS) or 10.0
        step = max(int(round(fps / float(self.cfg.get("sample_fps", 2.0)))), 1)
        self.state = "online"
        while not self.stop_flag.is_set():
            if not cap.grab():
                break
            if self.frame_index % step == 0:
                ok, frame = cap.retrieve()
                if ok:
                    self.accumulator.grab_counts.append(fps)
                    self.process(frame, self._replay_ts(fps))
            self.frame_index += 1
        last = self._replay_ts(fps)
        self._flush_minute(int(last // 60) * 60 + 60)
        cap.release()
        self.state = "finished"

    def _check_down(self) -> None:
        """Sem imagem: registra o minuto zerado e, se durar, avisa que caiu."""
        now = time.time()
        self._flush_minute(int(now // 60) * 60)
        reference = self.last_frame_clock or self.started_clock
        if now - reference >= OFFLINE_AFTER_S:
            self._mark_down(reference)

    def _run_live(self, url: str) -> None:
        backoff = 2.0
        self.started_clock = time.time()
        ever_connected = False
        while not self.stop_flag.is_set():
            cap = self._open(url)
            if not cap.isOpened():
                cap.release()
                self._check_down()
                self.stop_flag.wait(backoff)
                backoff = min(backoff * 1.6, 30.0)
                continue
            if ever_connected:
                self.accumulator.reconnects += 1
            ever_connected = True
            backoff = 2.0
            self._read_loop(cap)
            cap.release()
            self._check_down()
            self.stop_flag.wait(1.0)

    def _read_loop(self, cap: cv2.VideoCapture) -> None:
        interval = 1.0 / float(self.cfg.get("sample_fps", 2.0))
        next_sample = 0.0
        last_grab = time.monotonic()
        recent_intervals: Deque[float] = deque(maxlen=100)
        while not self.stop_flag.is_set():
            t0 = time.monotonic()
            if not cap.grab():
                return
            now_mono = time.monotonic()
            gap = now_mono - last_grab
            last_grab = now_mono
            recent_intervals.append(gap)
            typical = float(np.median(recent_intervals))
            if len(recent_intervals) > 30 and gap > max(typical * 5, 1.0):
                self.accumulator.gaps += 1
            second = int(time.time())
            if self.grab_second is None:
                self.grab_second = second
            if second != self.grab_second:
                self.accumulator.grab_counts.append(self.grabs_this_second)
                self.grabs_this_second = 0
                self.grab_second = second
            self.grabs_this_second += 1
            if t0 >= next_sample:
                next_sample = t0 + interval
                ok, frame = cap.retrieve()
                if ok and frame is not None:
                    ts = time.time()
                    self.last_frame_clock = ts
                    self._mark_up(ts)
                    self.process(frame, ts)
