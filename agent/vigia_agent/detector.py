"""Detecção de pessoas e veículos com YOLOX-Nano (Apache-2.0) via ONNX, em CPU."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np

log = logging.getLogger("vigia.detector")

SIZE = 416
STRIDES = (8, 16, 32)
PERSON = 0
VEHICLES = {1: "bicicleta", 2: "carro", 3: "moto", 5: "ônibus", 7: "caminhão"}


@dataclass
class Detection:
    label: str               # person|vehicle
    score: float
    box: Tuple[float, float, float, float]  # x1,y1,x2,y2 normalizados 0..1

    @property
    def foot(self) -> Tuple[float, float]:
        return (self.box[0] + self.box[2]) / 2, self.box[3]


def _grids() -> Tuple[np.ndarray, np.ndarray]:
    grids, expanded = [], []
    for stride in STRIDES:
        n = SIZE // stride
        xv, yv = np.meshgrid(np.arange(n), np.arange(n))
        grid = np.stack((xv, yv), 2).reshape(-1, 2)
        grids.append(grid)
        expanded.append(np.full((grid.shape[0], 1), stride))
    return np.concatenate(grids), np.concatenate(expanded)


def _nms(boxes: np.ndarray, scores: np.ndarray, iou: float) -> List[int]:
    order = scores.argsort()[::-1]
    keep: List[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        xx1 = np.maximum(boxes[i, 0], boxes[order[1:], 0])
        yy1 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
        xx2 = np.minimum(boxes[i, 2], boxes[order[1:], 2])
        yy2 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        area_i = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
        area_o = (boxes[order[1:], 2] - boxes[order[1:], 0]) * (boxes[order[1:], 3] - boxes[order[1:], 1])
        order = order[1:][inter / (area_i + area_o - inter + 1e-9) <= iou]
    return keep


class Detector:
    def __init__(self, model_path: str, threshold: float = 0.45) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        self.session = ort.InferenceSession(model_path, options, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        self.threshold = threshold
        self.grid, self.strides = _grids()

    def detect(self, frame: np.ndarray) -> List[Detection]:
        h, w = frame.shape[:2]
        ratio = min(SIZE / h, SIZE / w)
        resized = cv2.resize(frame, (int(w * ratio), int(h * ratio)), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((SIZE, SIZE, 3), 114, dtype=np.uint8)
        canvas[: resized.shape[0], : resized.shape[1]] = resized
        tensor = canvas.transpose(2, 0, 1)[None].astype(np.float32)
        out = self.session.run(None, {self.input_name: tensor})[0][0]

        xy = (out[:, :2] + self.grid) * self.strides
        wh = np.exp(out[:, 2:4]) * self.strides
        boxes = np.concatenate((xy - wh / 2, xy + wh / 2), axis=1) / ratio
        objectness = out[:, 4]
        classes = out[:, 5:]
        cls_ids = classes.argmax(1)
        scores = objectness * classes[np.arange(len(cls_ids)), cls_ids]

        results: List[Detection] = []
        for cls, label in [(PERSON, "person")] + [(k, "vehicle") for k in VEHICLES]:
            mask = (cls_ids == cls) & (scores >= self.threshold)
            if not mask.any():
                continue
            b, s = boxes[mask], scores[mask]
            for i in _nms(b, s, 0.45):
                x1, y1, x2, y2 = b[i]
                results.append(Detection(label, float(s[i]), (
                    float(np.clip(x1 / w, 0, 1)), float(np.clip(y1 / h, 0, 1)),
                    float(np.clip(x2 / w, 0, 1)), float(np.clip(y2 / h, 0, 1)))))
        return results


def load_detector(model_path: str) -> Optional[Detector]:
    if not Path(model_path).exists():
        log.warning("Modelo %s não encontrado: detecção de pessoas desligada", model_path)
        return None
    return Detector(model_path)


def point_in_polygon(point: Tuple[float, float], polygon: Sequence[Sequence[float]]) -> bool:
    x, y = point
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1:
            inside = not inside
    return inside
