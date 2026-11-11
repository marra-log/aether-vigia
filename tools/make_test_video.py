"""Gera um vídeo sintético com falhas conhecidas, para testar o agente de ponta a ponta.

Uso: python tools/make_test_video.py saida.mp4 [--pessoa foto.png]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

W, H, FPS = 640, 360, 6
TIMELINE = [("normal", 45), ("frozen", 45), ("normal", 25), ("black", 18), ("normal", 25),
            ("covered", 18), ("normal", 25), ("blur", 80), ("normal", 45), ("flicker", 50),
            ("normal", 45), ("moved", 40), ("normal", 45), ("person", 25), ("normal", 60)]
rng = np.random.default_rng(11)


def scene(t: float, shift: int = 0, gain: float = 1.0, walker: bool = True) -> np.ndarray:
    img = np.full((H, W, 3), (118, 128, 122), np.uint8)
    cv2.rectangle(img, (30, 90), (240, 320), (60, 90, 150), -1)
    cv2.rectangle(img, (310, 160), (610, 345), (160, 150, 70), -1)
    for x in range(0, W, 36):
        cv2.line(img, (x, 0), (x, 70), (35, 35, 35), 2)
    cv2.circle(img, (470, 95), 42, (215, 215, 225), -1)
    if walker:
        x = int(30 + (t * 30) % 560)
        cv2.rectangle(img, (x, 210), (x + 26, 300), (25, 25, 190), -1)
    if shift:
        img = cv2.warpAffine(img, np.float32([[1, 0, shift], [0, 1, shift // 2]]), (W, H))
    noisy = img.astype(np.float32) * gain + rng.normal(0, 2.5, img.shape)
    img = np.clip(noisy, 0, 255).astype(np.uint8)
    cv2.putText(img, f"CAM 01  {int(t):06d}", (380, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    return img


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--pessoa", type=Path, help="foto colada na cena na etapa 'person'")
    args = parser.parse_args()
    person = None
    if args.pessoa:
        person = cv2.resize(cv2.imread(str(args.pessoa)), (230, 230))

    writer = cv2.VideoWriter(str(args.out), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    t, marks = 0.0, []
    for name, seconds in TIMELINE:
        marks.append((name, t))
        frozen_frame = scene(t)
        for i in range(int(seconds * FPS)):
            now = t + i / FPS
            if name == "frozen":
                frame = frozen_frame
            elif name == "black":
                frame = rng.integers(0, 4, (H, W, 3), dtype=np.uint8)
            elif name == "covered":
                frame = np.clip(rng.normal(70, 2, (H, W, 3)), 0, 255).astype(np.uint8)
            elif name == "blur":
                frame = cv2.GaussianBlur(scene(now), (0, 0), 7)
            elif name == "flicker":
                frame = scene(now, gain=0.75 if int(now * 2) % 2 else 1.2)
            elif name == "moved":
                frame = scene(now, shift=120)
            elif name == "person" and person is not None:
                frame = scene(now, walker=False)
                x = 60 + int(i * 2)
                frame[100:330, x:x + 230] = person
            else:
                frame = scene(now)
            writer.write(frame)
        t += seconds
    writer.release()
    print({"duracao_s": t, "etapas": marks})


if __name__ == "__main__":
    main()
