"""Envio e recebimento pelo WhatsApp, com provedores intercambiáveis.

baileys    Conector próprio por QR Code (pasta whatsapp/). Não oficial.
meta       API oficial (Cloud API). Exige modelos aprovados: veja docs/whatsapp.md.
evolution  Evolution API própria, via QR Code. Não oficial.
zapi       Z-API, via QR Code. Não oficial.
console    Só registra no log. Serve para desenvolver e testar.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx

from .config import settings

log = logging.getLogger("vigia.whatsapp")
TIMEOUT = httpx.Timeout(25.0, connect=10.0)


class SendError(Exception):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Outgoing:
    to: str
    kind: str                         # alert|resolved|digest|test
    text: str                         # versão completa, para provedores de texto livre
    params: List[str] = field(default_factory=list)  # variáveis do modelo Meta
    image: Optional[bytes] = None
    event_id: Optional[int] = None


@dataclass
class Incoming:
    phone: str
    text: str


def digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _br_key(phone: str) -> str:
    """Celulares do Brasil podem chegar com ou sem o 9 depois do DDD."""
    p = digits(phone)
    if p.startswith("55") and len(p) == 13 and p[4] == "9":
        return p[:4] + p[5:]
    return p


def phones_match(a: str, b: str) -> bool:
    return _br_key(a) == _br_key(b) and bool(_br_key(a))


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    retryable = resp.status_code == 429 or resp.status_code >= 500
    raise SendError(f"HTTP {resp.status_code}: {resp.text[:300]}", retryable=retryable)


class ConsoleProvider:
    name = "console"
    configured = True

    def __init__(self) -> None:
        self.sent: List[Outgoing] = []

    def send(self, msg: Outgoing) -> str:
        self.sent.append(msg)
        log.info("WhatsApp (simulado) para %s:\n%s", msg.to, msg.text)
        return f"console-{len(self.sent)}"

    def reply(self, to: str, text: str) -> None:
        self.send(Outgoing(to=to, kind="reply", text=text))


class MetaProvider:
    name = "meta"
    TEMPLATES = {
        "alert_image": "vigia_alerta_imagem",
        "alert": "vigia_alerta",
        "resolved": "vigia_resolvido",
        "digest": "vigia_resumo",
        "test": "vigia_teste",
    }

    @property
    def configured(self) -> bool:
        return bool(settings.meta_token and settings.meta_phone_id)

    @property
    def _base(self) -> str:
        return f"https://graph.facebook.com/{settings.meta_graph_version}/{settings.meta_phone_id}"

    @property
    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {settings.meta_token}"}

    def _upload(self, client: httpx.Client, image: bytes) -> str:
        r = client.post(f"{self._base}/media", headers=self._headers,
                        data={"messaging_product": "whatsapp", "type": "image/jpeg"},
                        files={"file": ("alerta.jpg", image, "image/jpeg")})
        _raise_for(r)
        return r.json()["id"]

    def send(self, msg: Outgoing) -> str:
        name_key = "alert_image" if msg.kind == "alert" and msg.image else msg.kind
        template = self.TEMPLATES[name_key]
        components: list = []
        with httpx.Client(timeout=TIMEOUT) as client:
            if name_key == "alert_image":
                media = self._upload(client, msg.image)  # type: ignore[arg-type]
                components.append({"type": "header", "parameters": [
                    {"type": "image", "image": {"id": media}}]})
            components.append({"type": "body", "parameters": [
                {"type": "text", "text": _template_text(p)} for p in msg.params]})
            if msg.kind == "alert" and msg.event_id:
                components.append({"type": "button", "sub_type": "quick_reply", "index": "0",
                                   "parameters": [{"type": "payload", "payload": f"ACK:{msg.event_id}"}]})
                components.append({"type": "button", "sub_type": "quick_reply", "index": "1",
                                   "parameters": [{"type": "payload", "payload": "MUTE:1h"}]})
            r = client.post(f"{self._base}/messages", headers=self._headers, json={
                "messaging_product": "whatsapp", "to": digits(msg.to), "type": "template",
                "template": {"name": template,
                             "language": {"code": settings.meta_template_lang},
                             "components": components},
            })
        _raise_for(r)
        return r.json()["messages"][0]["id"]

    def reply(self, to: str, text: str) -> None:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.post(f"{self._base}/messages", headers=self._headers, json={
                "messaging_product": "whatsapp", "to": digits(to), "type": "text",
                "text": {"body": text, "preview_url": False},
            })
        _raise_for(r)


def _template_text(value: str) -> str:
    """A Meta recusa quebra de linha, tabulação e 4+ espaços em variáveis."""
    return re.sub(r"\s{2,}|\n|\t", " ", value).strip()[:1000] or "-"


class EvolutionProvider:
    name = "evolution"

    @property
    def configured(self) -> bool:
        return bool(settings.evolution_url and settings.evolution_key and settings.evolution_instance)

    def _post(self, path: str, body: dict) -> dict:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.post(f"{settings.evolution_url}{path}/{settings.evolution_instance}",
                            headers={"apikey": settings.evolution_key}, json=body)
        _raise_for(r)
        return r.json()

    def send(self, msg: Outgoing) -> str:
        if msg.image:
            data = self._post("/message/sendMedia", {
                "number": digits(msg.to), "mediatype": "image", "mimetype": "image/jpeg",
                "caption": msg.text, "fileName": "alerta.jpg",
                "media": base64.b64encode(msg.image).decode(),
            })
        else:
            data = self._post("/message/sendText", {"number": digits(msg.to), "text": msg.text})
        return str(data.get("key", {}).get("id", ""))

    def reply(self, to: str, text: str) -> None:
        self._post("/message/sendText", {"number": digits(to), "text": text})


class BaileysProvider:
    """Fala com o conector da pasta whatsapp/, que mantém a sessão do QR Code."""
    name = "baileys"

    @property
    def configured(self) -> bool:
        return bool(settings.baileys_url and settings.baileys_key)

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        try:
            with httpx.Client(timeout=TIMEOUT, trust_env=False) as client:
                r = client.request(method, f"{settings.baileys_url}{path}", json=body,
                                   headers={"Authorization": f"Bearer {settings.baileys_key}"})
        except httpx.HTTPError as exc:
            raise SendError(f"Conector de WhatsApp inacessível: {exc}") from exc
        if r.status_code == 503:   # celular desconectado: tenta de novo depois
            raise SendError("WhatsApp desconectado: leia o QR Code no painel")
        _raise_for(r)
        return r.json()

    def send(self, msg: Outgoing) -> str:
        body = {"number": digits(msg.to), "text": msg.text}
        if msg.image:
            body["image_b64"] = base64.b64encode(msg.image).decode()
        return str(self._call("POST", "/send", body).get("id", ""))

    def reply(self, to: str, text: str) -> None:
        self._call("POST", "/send", {"number": digits(to), "text": text})

    def status(self) -> dict:
        return self._call("GET", "/status")

    def logout(self) -> None:
        self._call("POST", "/logout")


class ZapiProvider:
    name = "zapi"

    @property
    def configured(self) -> bool:
        return bool(settings.zapi_instance and settings.zapi_token)

    def _post(self, path: str, body: dict) -> dict:
        headers = {"Client-Token": settings.zapi_client_token} if settings.zapi_client_token else {}
        url = f"https://api.z-api.io/instances/{settings.zapi_instance}/token/{settings.zapi_token}/{path}"
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.post(url, headers=headers, json=body)
        _raise_for(r)
        return r.json()

    def send(self, msg: Outgoing) -> str:
        if msg.image:
            b64 = base64.b64encode(msg.image).decode()
            data = self._post("send-image", {"phone": digits(msg.to), "caption": msg.text,
                                             "image": f"data:image/jpeg;base64,{b64}"})
        else:
            data = self._post("send-text", {"phone": digits(msg.to), "message": msg.text})
        return str(data.get("zaapId") or data.get("messageId") or "")

    def reply(self, to: str, text: str) -> None:
        self._post("send-text", {"phone": digits(to), "message": text})


_provider = None


def get_provider():
    global _provider
    if _provider is None:
        cls = {"baileys": BaileysProvider, "meta": MetaProvider, "evolution": EvolutionProvider,
               "zapi": ZapiProvider}.get(settings.wa_provider, ConsoleProvider)
        _provider = cls()
    return _provider


def set_provider(provider) -> None:
    global _provider
    _provider = provider


def verify_meta_signature(body: bytes, header: str) -> bool:
    if not settings.meta_app_secret or not header.startswith("sha256="):
        return False
    expected = hmac.new(settings.meta_app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[7:], expected)


def parse_incoming(provider: str, data: dict) -> List[Incoming]:
    """Traduz o webhook de cada provedor para (telefone, texto)."""
    out: List[Incoming] = []
    if provider == "meta":
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                for m in change.get("value", {}).get("messages", []) or []:
                    text = (m.get("text", {}).get("body")
                            or m.get("button", {}).get("payload")
                            or m.get("interactive", {}).get("button_reply", {}).get("id")
                            or "")
                    if text:
                        out.append(Incoming(m.get("from", ""), text))
    elif provider == "evolution":
        payload = data.get("data", {}) or {}
        key = payload.get("key", {}) or {}
        if not key.get("fromMe") and not str(key.get("remoteJid", "")).endswith("@g.us"):
            m = payload.get("message", {}) or {}
            text = m.get("conversation") or m.get("extendedTextMessage", {}).get("text") or ""
            if text:
                out.append(Incoming(str(key.get("remoteJid", "")).split("@")[0], text))
    elif provider == "baileys":
        if data.get("phone") and data.get("text"):
            out.append(Incoming(str(data["phone"]), str(data["text"])))
    elif provider == "zapi":
        if not data.get("fromMe") and not data.get("isGroup"):
            text = (data.get("text", {}) or {}).get("message") or (
                data.get("buttonsResponseMessage", {}) or {}).get("buttonId") or ""
            if text:
                out.append(Incoming(str(data.get("phone", "")), text))
    return out
