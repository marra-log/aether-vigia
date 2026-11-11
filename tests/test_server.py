"""API, regras de alerta, comandos e provedores de WhatsApp."""
import base64
import hashlib
import hmac
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app import alerts, security, whatsapp
from app.config import settings
from app.db import (Operator, Alert, BaselineBucket, Camera, Event, KV, Metric, Recipient, SessionLocal,
                    init_db)
from app.main import app
from app.security import decrypt, mask_url
from app.whatsapp import ConsoleProvider, Outgoing, SendError, phones_match, set_provider

AGENT = {"Authorization": "Bearer dev-agent"}
JPEG = b"\xff\xd8\xff\xe0fakejpeg"


@pytest.fixture()
def provider():
    p = ConsoleProvider()
    set_provider(p)
    return p


@pytest.fixture()
def client(provider):
    settings.validate()
    init_db()
    with SessionLocal() as db:
        for model in (Alert, Event, Metric, BaselineBucket, Recipient, Operator, Camera, KV):
            db.execute(delete(model))
        db.commit()
    security._attempts.clear()
    c = TestClient(app)
    assert c.post("/api/login", json={"password": "dev"}).status_code == 200
    return c


def camera(client, name="Portão"):
    r = client.post("/api/cameras", json={"name": name, "url": "rtsp://admin:segredo@10.0.0.2:554/cam"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def recipient(client, phone="5531999998888", **kw):
    r = client.post("/api/recipients", json={"name": "Dono", "phone": phone, **kw})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def event(client, cam, type_, state="open", ts=None, snapshot=False):
    body = {"camera_id": cam, "type": type_, "state": state, "ts": ts or time.time()}
    if snapshot:
        body["snapshot_b64"] = base64.b64encode(JPEG).decode()
    r = client.post("/api/agent/events", json=body, headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def tick(now=None):
    with SessionLocal() as db:
        alerts.tick(db, now or time.time())
        db.commit()


def test_painel_exige_login():
    c = TestClient(app)
    with c:
        assert c.get("/api/overview").status_code == 401
        assert c.get("/api/cameras").status_code == 401
        assert c.get("/api/events/1/snapshot").status_code == 401


def test_agente_exige_token(client):
    assert client.get("/api/agent/config").status_code == 401
    assert client.get("/api/agent/config", headers={"Authorization": "Bearer errado"}).status_code == 401


def test_senha_errada_e_limite_de_tentativas(client):
    fresh = TestClient(app)
    codes = [fresh.post("/api/login", json={"password": "x"}, headers={"x-forwarded-for": "9.9.9.9"}).status_code
             for _ in range(9)]
    assert codes[:8] == [401] * 8 and codes[8] == 429


def test_url_da_camera_nao_vaza(client):
    cam = camera(client)
    listing = client.get("/api/cameras").text
    assert "segredo" not in listing and "•••@10.0.0.2:554" in listing
    with SessionLocal() as db:
        c = db.get(Camera, cam)
        assert "segredo" not in c.rtsp_enc
        assert decrypt(c.rtsp_enc).endswith("segredo@10.0.0.2:554/cam")
    assert client.get("/api/agent/config", headers=AGENT).json()["cameras"][0]["url"].startswith("rtsp://admin")


def test_rejeita_endereco_perigoso(client):
    r = client.post("/api/cameras", json={"name": "x", "url": "file:///etc/passwd"})
    assert r.status_code == 422


def test_alerta_critico_com_imagem(client, provider):
    cam = camera(client)
    recipient(client)
    eid = event(client, cam, "OBSTRUCTED", snapshot=True)
    tick()
    assert len(provider.sent) == 1
    msg = provider.sent[0]
    assert "tampada" in msg.text and "Portão" in msg.text and msg.image == JPEG
    assert msg.event_id == eid
    assert client.get(f"/api/events/{eid}/snapshot").content == JPEG


def test_queda_rapida_nao_incomoda(client, provider):
    cam = camera(client)
    recipient(client)
    event(client, cam, "OFFLINE")
    event(client, cam, "OFFLINE", state="close")
    tick(time.time() + 120)
    assert provider.sent == []


def test_queda_longa_avisa_e_depois_avisa_que_voltou(client, provider):
    cam = camera(client)
    recipient(client)
    t0 = time.time()
    event(client, cam, "OFFLINE", ts=t0)
    tick(t0 + 10)
    assert provider.sent == []
    tick(t0 + 70)
    assert len(provider.sent) == 1 and "fora do ar" in provider.sent[0].text
    event(client, cam, "OFFLINE", state="close", ts=t0 + 700)
    tick(t0 + 710)
    assert len(provider.sent) == 2 and "Normalizado" in provider.sent[1].text


def test_varias_cameras_viram_uma_mensagem(client, provider):
    cams = [camera(client, f"Cam {i}") for i in range(4)]
    recipient(client)
    t0 = time.time()
    for c in cams:
        event(client, c, "OFFLINE", ts=t0)
    tick(t0 + 5)
    tick(t0 + 70)
    assert len(provider.sent) == 1
    assert "juntas" in provider.sent[0].text
    for c in cams:
        event(client, c, "OFFLINE", state="close", ts=t0 + 300)
    tick(t0 + 310)
    tick(t0 + 315)
    assert len(provider.sent) == 2 and "Normalizado" in provider.sent[1].text


def test_uma_camera_de_quatro_nao_vira_queda_do_local(client, provider):
    cams = [camera(client, f"Cam {i}") for i in range(4)]
    recipient(client)
    t0 = time.time()
    event(client, cams[0], "OFFLINE", ts=t0)
    tick(t0 + 70)
    assert len(provider.sent) == 1 and "fora do ar" in provider.sent[0].text


def test_intervalo_entre_alertas_iguais(client, provider):
    cam = camera(client)
    recipient(client)
    for _ in range(3):
        event(client, cam, "FROZEN")
        tick()
        event(client, cam, "FROZEN", state="close")
        tick()
    alerts_sent = [m for m in provider.sent if m.kind == "alert"]
    assert len(alerts_sent) == 1


def test_horario_silencioso_segura_atencao_mas_nao_critico(client, provider):
    cam = camera(client)
    recipient(client, quiet_start="00:00", quiet_end="23:59")
    event(client, cam, "BLUR")
    tick()
    assert provider.sent == []
    event(client, cam, "BLACK")
    tick()
    assert len(provider.sent) == 1


def test_gravidade_minima(client, provider):
    cam = camera(client)
    recipient(client, min_severity="critical")
    event(client, cam, "BLUR")
    tick()
    assert provider.sent == []


def test_silenciar_nao_cala_invasao(client, provider):
    cam = camera(client)
    recipient(client)
    with SessionLocal() as db:
        alerts.handle_command(db, "silenciar 2h")
        db.commit()
    event(client, cam, "FROZEN")
    event(client, cam, "INTRUSION", snapshot=True)
    tick()
    assert len(provider.sent) == 1 and "área protegida" in provider.sent[0].text


def test_perda_do_agente(client, provider):
    camera(client)
    recipient(client)
    assert client.post("/api/agent/heartbeat", json={"cameras": []}, headers=AGENT).status_code == 200
    tick(time.time() + 300)
    assert len(provider.sent) == 1 and "Perdi contato" in provider.sent[0].text
    client.post("/api/agent/heartbeat", json={"cameras": []}, headers=AGENT)
    tick()
    tick()
    assert "Normalizado" in provider.sent[-1].text


def test_quedas_repetidas_viram_oscilacao(client, provider):
    cam = camera(client)
    recipient(client)
    t0 = time.time()
    for i in range(4):
        event(client, cam, "OFFLINE", ts=t0 - 600 + i * 100)
        event(client, cam, "OFFLINE", state="close", ts=t0 - 550 + i * 100)
    tick(t0)
    with SessionLocal() as db:
        ev = db.scalars(select(Event).where(Event.type == "UNSTABLE")).one()
        assert "4 vezes" in ev.detail


def test_oscilacao_por_reconexoes(client, provider):
    cam = camera(client)
    recipient(client)
    base = int(time.time() // 60) * 60 - 1200
    minutes = [{"ts": base + i * 60, "uptime": 0.9, "reconnects": 1 if i % 3 == 0 else 0} for i in range(12)]
    r = client.post("/api/agent/metrics", json={"camera_id": cam, "minutes": minutes}, headers=AGENT)
    assert r.status_code == 200, r.text
    tick()
    assert len(provider.sent) == 1 and "oscilando" in provider.sent[0].text


def test_reenvia_quando_provedor_falha(client):
    class Flaky(ConsoleProvider):
        fails = 2

        def send(self, msg):
            if self.fails:
                self.fails -= 1
                raise SendError("HTTP 500")
            return super().send(msg)

    p = Flaky()
    set_provider(p)
    cam = camera(client)
    recipient(client)
    event(client, cam, "BLACK")
    t0 = time.time()
    tick(t0)
    assert p.sent == []
    tick(t0 + 5)   # ainda dentro da espera
    assert p.fails == 1
    tick(t0 + 16)
    tick(t0 + 100)
    assert len(p.sent) == 1


def test_erro_definitivo_nao_repete(client):
    class Bad(ConsoleProvider):
        calls = 0

        def send(self, msg):
            Bad.calls += 1
            raise SendError("HTTP 400", retryable=False)

    set_provider(Bad())
    cam = camera(client)
    recipient(client)
    event(client, cam, "BLACK")
    t0 = time.time()
    tick(t0)
    tick(t0 + 5000)
    assert Bad.calls == 1


def test_comandos(client, provider):
    cam = camera(client)
    recipient(client)
    eid = event(client, cam, "BLUR")
    with SessionLocal() as db:
        assert "1 câmeras" in alerts.handle_command(db, "status") or "de 1" in alerts.handle_command(db, "status")
        assert "marcada como vista" in alerts.handle_command(db, f"ok {eid}")
        assert "Diga o número" in alerts.handle_command(db, "ok")
        assert "Não encontrei" in alerts.handle_command(db, "ok 99999")
        assert "pausados" in alerts.handle_command(db, "silenciar 90min")
        assert "Não entendi" in alerts.handle_command(db, "silenciar nunca")
        assert "reativados" in alerts.handle_command(db, "reativar")
        assert "Comandos" in alerts.handle_command(db, "oi")
        db.commit()


def test_alarme_falso_reduz_sensibilidade_so_de_comportamento(client):
    cam = camera(client)
    behavior = event(client, cam, "BEHAVIOR_SPIKE")
    blur = event(client, cam, "BLUR")
    client.post(f"/api/events/{blur}/feedback", json={"feedback": "false"})
    with SessionLocal() as db:
        assert db.get(Camera, cam).threshold_mult == 1.0
    client.post(f"/api/events/{behavior}/feedback", json={"feedback": "false"})
    with SessionLocal() as db:
        assert db.get(Camera, cam).threshold_mult == 1.15


def test_numero_brasileiro_com_e_sem_nono_digito():
    assert phones_match("5531999998888", "553199998888")
    assert not phones_match("5531999998888", "5531999998887")
    assert not phones_match("", "")


def _signed(body):
    return {"X-Hub-Signature-256": "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest(),
            "Content-Type": "application/json"}


def _meta_message(phone, text):
    return json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"from": phone, "text": {"body": text}}]}}]}]}).encode()


def test_webhook_meta_valida_assinatura(client, provider, monkeypatch):
    from app import main
    provider.name = "meta"
    camera(client)
    recipient(client)
    body = _meta_message("553199998888", "status")
    assert client.post("/webhook/whatsapp", content=body,
                       headers={"X-Hub-Signature-256": "sha256=errado"}).status_code == 403
    assert client.post("/webhook/whatsapp", content=body, headers=_signed(body)).status_code == 200
    assert any("Loja Teste" in m.text for m in provider.sent)


def test_webhook_ignora_numero_desconhecido(client, provider):
    provider.name = "meta"
    recipient(client)
    body = _meta_message("5511911112222", "silenciar 7d")
    client.post("/webhook/whatsapp", content=body, headers=_signed(body))
    assert provider.sent == []
    with SessionLocal() as db:
        assert db.get(KV, "mute") is None


def test_verificacao_do_webhook_meta(client):
    ok = client.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    assert client.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "42"}).status_code == 403


def test_webhook_com_segredo(client, provider):
    provider.name = "zapi"
    recipient(client)
    body = {"phone": "5531999998888", "text": {"message": "ajuda"}, "fromMe": False}
    assert client.post("/webhook/whatsapp/errado", json=body).status_code == 403
    assert client.post("/webhook/whatsapp/hook-secret", json=body).status_code == 200
    assert "Comandos" in provider.sent[-1].text


def test_parse_evolution_ignora_proprias_mensagens():
    body = {"data": {"key": {"remoteJid": "5531999998888@s.whatsapp.net", "fromMe": True}, "message": {"conversation": "oi"}}}
    assert whatsapp.parse_incoming("evolution", body) == []
    body["data"]["key"]["fromMe"] = False
    got = whatsapp.parse_incoming("evolution", body)
    assert got[0].phone == "5531999998888" and got[0].text == "oi"


_REAL_CLIENT = httpx.Client


def _mock_client(handler):
    return lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)


def test_payload_meta_com_imagem(monkeypatch):
    settings.meta_token, settings.meta_phone_id = "tok", "123"
    calls = []

    def handler(req):
        calls.append(req)
        if req.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "media-1"})
        return httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})

    monkeypatch.setattr(whatsapp.httpx, "Client", _mock_client(handler))
    p = whatsapp.MetaProvider()
    mid = p.send(Outgoing(to="5531999998888", kind="alert", text="x", image=JPEG, event_id=7,
                          params=["Titulo", "Portão", "09/11 18:24", "linha1\nlinha2"]))
    assert mid == "wamid.1" and calls[0].url.path == "/v24.0/123/media"
    body = json.loads(calls[1].content)
    assert body["template"]["name"] == "vigia_alerta_imagem"
    comps = body["template"]["components"]
    assert comps[0]["parameters"][0]["image"]["id"] == "media-1"
    assert "\n" not in comps[1]["parameters"][3]["text"]
    assert comps[2]["parameters"][0]["payload"] == "ACK:7"


def test_payload_evolution_e_zapi(monkeypatch):
    settings.evolution_url, settings.evolution_key, settings.evolution_instance = "https://evo", "k", "loja"
    settings.zapi_instance, settings.zapi_token, settings.zapi_client_token = "i", "t", "c"
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"key": {"id": "e1"}, "zaapId": "z1"})

    monkeypatch.setattr(whatsapp.httpx, "Client", _mock_client(handler))
    msg = Outgoing(to="+55 31 99999-8888", kind="alert", text="legenda", image=JPEG)
    assert whatsapp.EvolutionProvider().send(msg) == "e1"
    evo = json.loads(calls[0].content)
    assert calls[0].url.path == "/message/sendMedia/loja" and calls[0].headers["apikey"] == "k"
    assert evo["number"] == "5531999998888" and evo["mediatype"] == "image" and evo["caption"] == "legenda"
    assert whatsapp.ZapiProvider().send(msg) == "z1"
    z = json.loads(calls[1].content)
    assert calls[1].url.path == "/instances/i/token/t/send-image" and calls[1].headers["client-token"] == "c"
    assert z["image"].startswith("data:image/jpeg;base64,")


def test_visao_geral_e_serie(client):
    cam = camera(client)
    base = int(time.time() // 60) * 60 - 600
    minutes = [{"ts": base + i * 60, "uptime": 1.0, "fps": 15, "motion": 1.5, "sharp_ratio": 1.0} for i in range(8)]
    client.post("/api/agent/metrics", json={"camera_id": cam, "minutes": minutes}, headers=AGENT)
    client.post("/api/agent/metrics", json={"camera_id": cam, "minutes": minutes}, headers=AGENT)  # idempotente
    over = client.get("/api/overview").json()
    assert over["cameras"][0]["health"] > 90
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Metric)) == 8
    series = client.get(f"/api/cameras/{cam}/series?hours=24").json()
    assert [p for p in series["series"] if p.get("fps")]


def test_mascara_de_url():
    assert mask_url("rtsp://a:b@1.2.3.4:554/x?y=1") == "rtsp://•••@1.2.3.4:554/x?y=1"


# Operador da empresa ------------------------------------------------------------------

@pytest.fixture()
def operator(client, monkeypatch):
    monkeypatch.setattr(settings, "operator_password", "op-123")
    op = TestClient(app)
    r = op.post("/api/login", json={"password": "op-123"})
    assert r.status_code == 200 and r.json()["role"] == "operator"
    return op


def test_operador_ve_mas_nao_configura(client, operator):
    cam = camera(client)
    assert operator.get("/api/me").json()["role"] == "operator"
    assert operator.get("/api/overview").status_code == 200
    assert operator.get(f"/api/cameras/{cam}/series").status_code == 200
    assert operator.post("/api/cameras", json={"name": "x", "url": "rtsp://1.2.3.4/a"}).status_code == 403
    assert operator.delete(f"/api/cameras/{cam}").status_code == 403
    assert operator.get("/api/recipients").status_code == 403
    assert operator.post("/api/mute", json={"hours": 1}).status_code == 403


def test_operador_confirma_antes_do_dono_receber(client, operator, provider, monkeypatch):
    monkeypatch.setattr(settings, "operator_review", True)
    cam = camera(client)
    recipient(client)
    eid = event(client, cam, "BEHAVIOR_SPIKE", snapshot=True)
    tick()
    assert provider.sent == []
    queue = operator.get("/api/events?status=review").json()["events"]
    assert [e["id"] for e in queue] == [eid] and queue[0]["awaiting_review"]
    assert operator.get("/api/overview").json()["review_count"] == 1
    operator.post(f"/api/events/{eid}/feedback", json={"feedback": "real"})
    tick()
    assert len(provider.sent) == 1
    assert "Confirmado pela equipe de monitoramento" in provider.sent[0].text
    assert operator.get("/api/events?status=review").json()["events"] == []


def test_operador_descarta_e_dono_nao_e_incomodado(client, operator, provider, monkeypatch):
    monkeypatch.setattr(settings, "operator_review", True)
    cam = camera(client)
    recipient(client)
    eid = event(client, cam, "BEHAVIOR_SPIKE")
    operator.post(f"/api/events/{eid}/feedback", json={"feedback": "false"})
    tick(time.time() + 3600)
    assert provider.sent == []
    with SessionLocal() as db:
        assert db.get(Camera, cam).threshold_mult == 1.15


def test_sem_revisao_a_tempo_o_alerta_segue(client, provider, monkeypatch):
    monkeypatch.setattr(settings, "operator_review", True)
    cam = camera(client)
    recipient(client)
    event(client, cam, "BLUR")
    tick(time.time() + 60)
    assert provider.sent == []
    tick(time.time() + settings.review_timeout_s + 5)
    assert len(provider.sent) == 1 and "Ainda não revisado" in provider.sent[0].text


def test_critico_nao_espera_revisao(client, provider, monkeypatch):
    monkeypatch.setattr(settings, "operator_review", True)
    cam = camera(client)
    recipient(client)
    event(client, cam, "OBSTRUCTED")
    tick()
    assert len(provider.sent) == 1


def test_operador_aponta_anomalia(client, operator, provider):
    cam = camera(client)
    recipient(client)
    client.post(f"/api/agent/cameras/{cam}/thumb", content=JPEG, headers=AGENT)
    r = operator.post(f"/api/cameras/{cam}/flag", json={"note": "Portão aberto fora do horário", "severity": "critical"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "operador" and r.json()["severity"] == "critical"
    tick()
    msg = provider.sent[0]
    assert "Observação do operador: Portão aberto fora do horário" in msg.text and msg.image == JPEG
    assert operator.post(f"/api/cameras/{cam}/flag", json={"note": "x"}).status_code == 422


# Conector por QR Code (Baileys) -------------------------------------------------------

def _baileys(monkeypatch, handler):
    monkeypatch.setattr(settings, "baileys_url", "http://wa.internal:3000")
    monkeypatch.setattr(settings, "baileys_key", "chave-do-conector-123")
    monkeypatch.setattr(whatsapp.httpx, "Client", _mock_client(handler))
    p = whatsapp.BaileysProvider()
    set_provider(p)
    return p


def test_baileys_envia_imagem_e_texto(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"id": "ABC"})

    p = _baileys(monkeypatch, handler)
    assert p.send(Outgoing(to="+55 31 99999-8888", kind="alert", text="legenda", image=JPEG)) == "ABC"
    body = json.loads(calls[0].content)
    assert calls[0].url.path == "/send" and calls[0].headers["authorization"] == "Bearer chave-do-conector-123"
    assert body["number"] == "5531999998888" and body["text"] == "legenda"
    assert base64.b64decode(body["image_b64"]) == JPEG
    p.reply("5531999998888", "ok")
    assert "image_b64" not in json.loads(calls[1].content)


def test_baileys_desconectado_tenta_de_novo(monkeypatch):
    p = _baileys(monkeypatch, lambda req: httpx.Response(503, json={"error": "x"}))
    with pytest.raises(SendError) as err:
        p.send(Outgoing(to="5531999998888", kind="alert", text="x"))
    assert err.value.retryable
    p = _baileys(monkeypatch, lambda req: httpx.Response(422, json={"error": "sem WhatsApp"}))
    with pytest.raises(SendError) as err:
        p.send(Outgoing(to="5531999998888", kind="alert", text="x"))
    assert not err.value.retryable


def test_painel_mostra_qr_e_desconecta(client, monkeypatch):
    assert client.get("/api/whatsapp/connection").json() == {"supported": False}
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json={"status": "qr", "qr": "data:image/png;base64,AAA", "number": None, "ok": True})

    _baileys(monkeypatch, handler)
    data = client.get("/api/whatsapp/connection").json()
    assert data["status"] == "qr" and data["qr"].startswith("data:image/png")
    assert client.post("/api/whatsapp/disconnect").json() == {"ok": True}
    assert ("POST", "/logout") in seen


def test_qr_so_para_o_dono(client, operator, monkeypatch):
    assert operator.get("/api/whatsapp/connection").status_code == 403
    assert operator.post("/api/whatsapp/disconnect").status_code == 403


def test_conector_fora_do_ar_nao_quebra_o_painel(client, monkeypatch):
    def handler(req):
        raise httpx.ConnectError("sem rota")

    _baileys(monkeypatch, handler)
    assert client.get("/api/whatsapp/connection").json()["status"] == "unreachable"


def test_comando_recebido_pelo_conector(client, provider):
    provider.name = "baileys"
    recipient(client)
    body = {"phone": "553199998888", "text": "status"}
    assert client.post("/webhook/whatsapp/hook-secret", json=body).status_code == 200
    assert "Loja Teste" in provider.sent[-1].text


# Operadores com login próprio e áreas protegidas ---------------------------------------

def new_operator(client, name="Ana Souza", username="ana.souza", password="senha-forte-1"):
    r = client.post("/api/operators", json={"name": name, "username": username, "password": password})
    assert r.status_code == 200, r.text
    op = TestClient(app)
    r2 = op.post("/api/login", json={"username": username.upper(), "password": password})
    assert r2.status_code == 200 and r2.json()["role"] == "operator"
    return r.json()["id"], op


def test_operador_com_login_proprio_fica_registrado(client, provider, monkeypatch):
    monkeypatch.setattr(settings, "operator_review", True)
    cam = camera(client)
    recipient(client)
    _, ana = new_operator(client)
    assert ana.get("/api/me").json()["name"] == "Ana Souza"
    eid = event(client, cam, "BEHAVIOR_SPIKE")
    ana.post(f"/api/events/{eid}/feedback", json={"feedback": "real"})
    tick()
    assert "Confirmado por Ana Souza, da equipe de monitoramento" in provider.sent[0].text
    listed = client.get("/api/events").json()["events"][0]
    assert listed["reviewed_by"] == "operator" and listed["reviewed_name"] == "Ana Souza"
    ana.post(f"/api/cameras/{cam}/flag", json={"note": "Caminhão na contramão"})
    tick()
    assert "Observação do operador Ana Souza: Caminhão na contramão" in provider.sent[-1].text


def test_senha_do_operador_e_guardada_com_hash(client):
    oid, _ = new_operator(client)
    with SessionLocal() as db:
        stored = db.get(Operator, oid).password_hash
    assert "senha-forte-1" not in stored and stored.startswith("scrypt$")
    assert "password" not in client.get("/api/operators").text


def test_login_de_operador_errado_e_usuario_duplicado(client):
    new_operator(client)
    fresh = TestClient(app)
    assert fresh.post("/api/login", json={"username": "ana.souza", "password": "errada"}).status_code == 401
    assert fresh.post("/api/login", json={"username": "ninguem", "password": "senha-forte-1"}).status_code == 401
    # a senha do dono não entra pelo campo de operador
    assert fresh.post("/api/login", json={"username": "ana.souza", "password": "dev"}).status_code == 401
    dup = client.post("/api/operators", json={"name": "Outra", "username": "Ana.Souza", "password": "senha-forte-2"})
    assert dup.status_code == 409
    assert client.post("/api/operators", json={"name": "X Y", "username": "ab", "password": "senha-forte-2"}).status_code == 422
    assert client.post("/api/operators", json={"name": "X Y", "username": "abc", "password": "curta"}).status_code == 422


def test_operador_removido_perde_acesso_na_hora(client):
    oid, ana = new_operator(client)
    assert ana.get("/api/overview").status_code == 200
    assert ana.get("/api/operators").status_code == 403
    assert ana.post("/api/operators", json={"name": "Eu", "username": "eu.mesmo", "password": "senha-forte-9"}).status_code == 403
    client.delete(f"/api/operators/{oid}")
    assert ana.get("/api/overview").status_code == 401


def test_area_protegida_e_dias(client):
    cam = camera(client)
    base = {"name": "Portão", "armed_start": "22:00", "armed_end": "06:00"}
    zone = [[0.1, 0.2], [0.9, 0.2], [0.5, 0.95]]
    r = client.put(f"/api/cameras/{cam}", json={**base, "zones": [zone], "armed_days": [5, 6, 5], "loiter_seconds": 300})
    assert r.status_code == 200, r.text
    cfg = client.get("/api/agent/config", headers=AGENT).json()["cameras"][0]
    assert cfg["zones"] == [zone] and cfg["armed_days"] == [5, 6] and cfg["loiter_seconds"] == 300
    assert cfg["url"].startswith("rtsp://admin")   # salvar sem endereço mantém o atual
    for bad in ([[[0.1, 0.2], [0.9, 0.2]]], [[[0.1, 0.2], [1.4, 0.2], [0.5, 0.9]]], [[[0.1], [0.2], [0.3]]]):
        assert client.put(f"/api/cameras/{cam}", json={**base, "zones": bad}).status_code == 422
    assert client.put(f"/api/cameras/{cam}", json={**base, "armed_days": []}).status_code == 422
    assert client.put(f"/api/cameras/{cam}", json={**base, "armed_days": [7]}).status_code == 422
