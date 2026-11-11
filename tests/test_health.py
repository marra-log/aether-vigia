"""Injeta cada defeito em imagem sintética e confere abertura e fechamento."""
import numpy as np
import cv2
import pytest

from vigia_agent.health import SignalMonitor

rng = np.random.default_rng(3)


def scene(t, color=True, shift=0, gain=1.0, walker=True):
    """Cena com estrutura, ruído de sensor, relógio e, se pedido, alguém andando."""
    img = np.full((360, 640, 3), (120, 130, 125), np.uint8)
    cv2.rectangle(img, (40, 80), (250, 300), (60, 90, 150), -1)
    cv2.rectangle(img, (320, 150), (600, 340), (170, 160, 60), -1)
    for x in range(0, 640, 40):
        cv2.line(img, (x, 0), (x, 70), (30, 30, 30), 2)
    cv2.circle(img, (450, 90), 45, (220, 220, 230), -1)
    if walker:
        x = int(40 + (t * 25) % 560)
        cv2.rectangle(img, (x, 200), (x + 30, 300), (20, 20, 200), -1)
    if shift:
        img = cv2.warpAffine(img, np.float32([[1, 0, shift], [0, 1, shift // 2]]), (640, 360))
    img = np.clip(img.astype(np.float32) * gain + rng.normal(0, 2.5, img.shape), 0, 255).astype(np.uint8)
    cv2.putText(img, f"{int(t):08d}", (420, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    if not color:
        img = cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    return img


def run(mon, make, start, seconds, fps=2):
    events = []
    t = start
    while t < start + seconds:
        _, trs = mon.update(make(t), t)
        events += [(x.type, x.state) for x in trs]
        t += 1 / fps
    return events, t


def warmed():
    mon = SignalMonitor()
    events, t = run(mon, scene, 0, 60)
    assert events == []
    return mon, t


def test_cena_normal_nao_gera_alarme():
    mon = SignalMonitor()
    events, _ = run(mon, lambda t: scene(t, gain=0.85 + 0.25 * t / 1800), 0, 1800)
    assert events == []


def test_imagem_travada():
    mon, t = warmed()
    frozen = scene(t)
    events, t = run(mon, lambda _: frozen, t, 45)
    assert ("FROZEN", "open") in events
    events, t = run(mon, scene, t, 20)
    assert ("FROZEN", "close") in events


def test_imagem_travada_com_saltos_do_codificador():
    mon, t = warmed()
    base = scene(t)
    blip = lambda x: np.clip(base.astype(np.int16) + (rng.integers(-5, 6, base.shape) if int(x * 2) % 8 == 0 else 0), 0, 255).astype(np.uint8)
    events, t = run(mon, blip, t, 50)
    assert ("FROZEN", "open") in events


def test_tampada_com_imagem_lisa_nao_vira_travada():
    mon, t = warmed()
    flat = np.full((360, 640, 3), 70, np.uint8)
    events, t = run(mon, lambda _: flat, t, 15)
    assert events == [("OBSTRUCTED", "open")]


def test_imagem_preta():
    mon, t = warmed()
    events, t = run(mon, lambda x: rng.integers(0, 4, (360, 640, 3), dtype=np.uint8), t, 20)
    assert events == [("BLACK", "open")]
    events, t = run(mon, scene, t, 15)
    assert ("BLACK", "close") in events


def test_camera_tampada():
    mon, t = warmed()
    cover = lambda x: np.clip(np.full((360, 640, 3), 70, np.float32) + rng.normal(0, 2, (360, 640, 3)), 0, 255).astype(np.uint8)
    events, t = run(mon, cover, t, 15)
    assert events == [("OBSTRUCTED", "open")]


def test_desfoque():
    mon, t = warmed()
    events, t = run(mon, lambda x: cv2.GaussianBlur(scene(x), (0, 0), 7), t, 80)
    assert events == [("BLUR", "open")]


def test_camera_movida():
    mon, t = warmed()
    events, t = run(mon, lambda x: scene(x, shift=120), t, 40)
    assert events == [("MOVED", "open")]


def test_piscando():
    mon, t = warmed()
    events, t = run(mon, lambda x: scene(x, gain=0.75 if int(x * 2) % 2 else 1.2), t, 60)
    assert ("FLICKER", "open") in events


def test_transicao_para_infravermelho_nao_gera_alarme():
    mon, t = warmed()
    night = lambda x: cv2.GaussianBlur(scene(x, color=False, gain=0.5), (0, 0), 3)
    events, t = run(mon, night, t, 300)
    assert events == []
    # voltar ao dia usa a referência do dia, já aprendida
    events, t = run(mon, scene, t, 120)
    assert events == []


def test_oscilacao_curta_nao_abre_ocorrencia():
    mon, t = warmed()
    frozen = scene(t)
    events, t = run(mon, lambda _: frozen, t, 10)
    assert events == []
