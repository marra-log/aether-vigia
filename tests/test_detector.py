"""Roda o modelo real em uma foto real e confere as regras de segurança."""
from pathlib import Path
from zoneinfo import ZoneInfo

import cv2
import numpy as np
import pytest
from skimage import data

from vigia_agent.detector import Detection, Detector, point_in_polygon
from vigia_agent.rules import SecurityRules, in_schedule

MODEL = Path(__file__).resolve().parents[1] / "agent" / "models" / "yolox_nano.onnx"
TZ = ZoneInfo("America/Sao_Paulo")
NIGHT_TS = 1762736400.0  # 2025-11-09 22:00 em São Paulo


def test_detecta_pessoa_em_foto_real():
    frame = cv2.cvtColor(data.astronaut(), cv2.COLOR_RGB2BGR)
    found = Detector(str(MODEL)).detect(frame)
    people = [d for d in found if d.label == "person"]
    assert people and max(d.score for d in people) > 0.5
    x1, y1, x2, y2 = people[0].box
    assert 0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1


def test_nao_detecta_pessoa_em_imagem_sem_pessoas():
    frame = cv2.cvtColor(data.coffee(), cv2.COLOR_RGB2BGR)
    assert not [d for d in Detector(str(MODEL)).detect(frame) if d.label == "person"]


def test_janela_noturna_atravessa_meia_noite():
    days = [0, 1, 2, 3, 4, 5, 6]
    assert in_schedule("22:00", "06:00", days, NIGHT_TS + 3 * 3600, TZ)
    assert not in_schedule("22:00", "06:00", days, NIGHT_TS + 12 * 3600, TZ)
    assert not in_schedule(None, None, days, NIGHT_TS, TZ)


def test_poligono():
    square = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]
    assert point_in_polygon((0.5, 0.5), square)
    assert not point_in_polygon((0.9, 0.5), square)


def person(x=0.5, y=0.5):
    return Detection("person", 0.8, (x - 0.05, y - 0.2, x + 0.05, y))


CFG = {"armed_start": "22:00", "armed_end": "06:00", "armed_days": list(range(7)), "zones": [],
       "loiter_seconds": 60}


def test_invasao_exige_confirmacao():
    rules = SecurityRules(CFG, TZ)
    assert rules.update([person()], NIGHT_TS) == []
    opened = rules.update([person()], NIGHT_TS + 1)
    assert [(t.type, t.state) for t in opened] == [("INTRUSION", "open")]


def test_deteccao_isolada_nao_dispara():
    rules = SecurityRules(CFG, TZ)
    out = rules.update([person()], NIGHT_TS)
    out += rules.update([], NIGHT_TS + 1)
    out += rules.update([], NIGHT_TS + 2)
    assert out == []


def test_fora_do_horario_nao_e_invasao():
    assert SecurityRules(CFG, TZ).update([person()], NIGHT_TS + 12 * 3600) == []


def test_fora_da_zona_nao_e_invasao():
    cfg = {**CFG, "zones": [[[0.0, 0.0], [0.3, 0.0], [0.3, 0.3], [0.0, 0.3]]]}
    rules = SecurityRules(cfg, TZ)
    assert rules.update([person()], NIGHT_TS) == [] and rules.update([person()], NIGHT_TS + 1) == []


def test_permanencia():
    rules = SecurityRules({**CFG, "armed_start": None, "armed_end": None}, TZ)
    types = []
    for i in range(0, 80, 5):
        types += [t.type for t in rules.update([person()], NIGHT_TS + i)]
    assert types == ["LOITERING"]
