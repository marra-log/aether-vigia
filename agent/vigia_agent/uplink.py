"""Envia dados ao servidor. Se a internet cair, guarda e reenvia quando voltar."""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import httpx
import numpy as np

log = logging.getLogger("vigia.uplink")
QUEUE_LIMIT = 5000   # minutos e ocorrências guardados durante queda de internet


class Uplink:
    def __init__(self, base_url: str, token: str, transport: Optional[httpx.BaseTransport] = None) -> None:
        self.client = httpx.Client(base_url=base_url.rstrip("/"),
                                   headers={"Authorization": f"Bearer {token}"},
                                   timeout=httpx.Timeout(20.0, connect=8.0), transport=transport)
        self.events: Deque[dict] = deque(maxlen=QUEUE_LIMIT)
        self.metrics: Deque[Tuple[int, dict, Callable[[], Optional[np.ndarray]]]] = deque(maxlen=QUEUE_LIMIT)
        self.thumbs: Dict[int, bytes] = {}
        self.lock = threading.Lock()

    def get_config(self) -> Optional[dict]:
        try:
            r = self.client.get("/api/agent/config")
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            log.warning("Não consegui buscar a configuração: %s", exc)
            return None

    def heartbeat(self, payload: dict) -> bool:
        try:
            self.client.post("/api/agent/heartbeat", json=payload).raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.warning("Sem contato com o servidor: %s", exc)
            return False

    def send_event(self, body: dict) -> None:
        with self.lock:
            self.events.append(body)

    def send_metric(self, camera_id: int, row: dict, frame_getter: Callable[[], Any]) -> None:
        with self.lock:
            self.metrics.append((camera_id, row, frame_getter))

    def send_thumb(self, camera_id: int, jpeg: bytes) -> None:
        with self.lock:
            self.thumbs[camera_id] = jpeg

    def flush(self) -> None:
        """Ocorrências primeiro: são o que o dono mais precisa receber."""
        from .worker import encode_jpeg

        try:
            while self.events:
                body = self.events[0]
                r = self.client.post("/api/agent/events", json=body)
                if r.status_code >= 500:
                    r.raise_for_status()
                if r.status_code >= 400:
                    log.error("Ocorrência recusada (%s): %s", r.status_code, r.text[:200])
                with self.lock:
                    self.events.popleft()

            while self.metrics:
                with self.lock:
                    camera_id = self.metrics[0][0]
                    batch: List[Tuple[int, dict, Any]] = []
                    while self.metrics and self.metrics[0][0] == camera_id and len(batch) < 200:
                        batch.append(self.metrics.popleft())
                try:
                    r = self.client.post("/api/agent/metrics", json={
                        "camera_id": camera_id, "minutes": [b[1] for b in batch]})
                    if r.status_code in {404, 422}:   # câmera apagada: não trava a fila
                        log.error("Minutos recusados (%s): %s", r.status_code, r.text[:200])
                        continue
                    r.raise_for_status()
                except httpx.HTTPError:
                    with self.lock:
                        self.metrics.extendleft(reversed(batch))
                    raise
                for event_id in r.json().get("snapshot_for", []):
                    frame = batch[-1][2]()
                    if frame is not None:
                        self.client.post(f"/api/agent/events/{event_id}/snapshot",
                                         content=encode_jpeg(frame),
                                         headers={"Content-Type": "image/jpeg"})

            with self.lock:
                thumbs, self.thumbs = self.thumbs, {}
            for camera_id, jpeg in thumbs.items():
                self.client.post(f"/api/agent/cameras/{camera_id}/thumb", content=jpeg,
                                 headers={"Content-Type": "image/jpeg"})
        except httpx.HTTPError as exc:
            log.warning("Envio adiado: %s", exc)
