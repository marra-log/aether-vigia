"""Sessão do dono, token do agente e criptografia das URLs das câmeras."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Cookie, Header, HTTPException, Request

from .config import settings

COOKIE = "vigia_session"
SESSION_DAYS = 30


def _key(purpose: str) -> bytes:
    return hashlib.sha256(f"{purpose}:{settings.secret_key}".encode()).digest()


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_key("rtsp")))


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode() if text else ""


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


def mask_url(url: str) -> str:
    """Esconde usuário e senha ao mostrar a URL no painel."""
    if not url:
        return ""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    if parts.username or parts.password:
        host = "•••@" + host
    return urlunsplit((parts.scheme, host, parts.path, parts.query, ""))


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1)
    except ValueError:
        return False
    return hmac.compare_digest(got.hex(), digest)


def create_session(role: str = "owner", operator_id: Optional[int] = None,
                   name: Optional[str] = None) -> str:
    payload = {"exp": int(time.time()) + SESSION_DAYS * 86400, "role": role}
    if operator_id is not None:
        payload.update({"oid": operator_id, "name": name})
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    sig = hmac.new(_key("session"), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def session_data(token: Optional[str]) -> Optional[dict]:
    """Conteúdo da sessão ({role, oid?, name?}), ou None se inválida ou vencida."""
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = hmac.new(_key("session"), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body))
    except ValueError:
        return None
    if data.get("exp", 0) <= time.time():
        return None
    data["role"] = data.get("role", "owner")
    return data if data["role"] in {"owner", "operator"} else None


def session_role(token: Optional[str]) -> Optional[str]:
    data = session_data(token)
    return data["role"] if data else None


def valid_session(token: Optional[str]) -> bool:
    return session_role(token) is not None


def current_user(vigia_session: Optional[str] = Cookie(default=None)) -> dict:
    """Quem está usando o painel. Operador removido ou desativado perde o acesso na hora."""
    data = session_data(vigia_session)
    if data is None:
        raise HTTPException(status_code=401, detail="Entre com sua senha")
    user = {"role": data["role"], "name": data.get("name"), "oid": data.get("oid")}
    if user["oid"] is not None:
        from .db import Operator, SessionLocal

        with SessionLocal() as db:
            operator = db.get(Operator, user["oid"])
            if operator is None or not operator.active:
                raise HTTPException(status_code=401, detail="Seu acesso foi encerrado")
            user["name"] = operator.name
    return user


def require_user(vigia_session: Optional[str] = Cookie(default=None)) -> str:
    return current_user(vigia_session)["role"]


def require_owner(vigia_session: Optional[str] = Cookie(default=None)) -> str:
    role = require_user(vigia_session)
    if role != "owner":
        raise HTTPException(status_code=403, detail="Só o dono pode alterar isso")
    return role


def require_agent(authorization: str = Header(default="")) -> None:
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(token, settings.agent_token):
        raise HTTPException(status_code=401, detail="Token do agente inválido")


_attempts: Dict[str, Deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    # O último endereço é o que o proxy da hospedagem acrescentou; os anteriores
    # vêm do próprio cliente e podem ser inventados.
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[-1].strip() or (request.client.host if request.client else "?")


def _prune(q: Deque[float], now: float) -> None:
    while q and q[0] < now - 600:
        q.popleft()


def check_login_rate(request: Request) -> None:
    """Depois de 8 senhas erradas em 10 minutos, bloqueia (40 no total, por segurança)."""
    now = time.time()
    per_ip, overall = _attempts[_client_ip(request)], _attempts["*"]
    _prune(per_ip, now)
    _prune(overall, now)
    if len(per_ip) >= 8 or len(overall) >= 40:
        raise HTTPException(status_code=429, detail="Muitas tentativas. Aguarde alguns minutos.")


def record_failed_login(request: Request) -> None:
    now = time.time()
    _attempts[_client_ip(request)].append(now)
    _attempts["*"].append(now)


def password_role(password: str) -> Optional[str]:
    if hmac.compare_digest(password.encode(), settings.owner_password.encode()):
        return "owner"
    if settings.operator_password and hmac.compare_digest(
            password.encode(), settings.operator_password.encode()):
        return "operator"
    return None
