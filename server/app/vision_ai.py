"""Descrição opcional da imagem do alerta em uma frase, usando o Claude.

Só roda se ANTHROPIC_API_KEY estiver definida. Se falhar, o alerta segue normalmente.
"""
from __future__ import annotations

import base64
import logging
from typing import Optional

import httpx

from .config import settings

log = logging.getLogger("vigia.vision")

PROMPT = (
    "Esta é uma imagem de câmera de segurança do próprio estabelecimento. O sistema "
    "registrou: {title}. Descreva em uma frase curta, em português do Brasil, o que se vê "
    "e que ajude o dono a decidir se precisa agir. Fale de ações e objetos, sem tentar "
    "identificar quem são as pessoas e sem deduzir idade, origem ou aparência física. "
    "Se a imagem não mostrar nada relevante, diga isso. Responda só com a frase."
)


def describe(image: bytes, title: str) -> Optional[str]:
    if not settings.anthropic_key:
        return None
    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={"x-api-key": settings.anthropic_key,
                     "anthropic-version": "2023-06-01"},
            json={
                "model": settings.vision_model,
                "max_tokens": 120,
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg",
                        "data": base64.b64encode(image).decode()}},
                    {"type": "text", "text": PROMPT.format(title=title)},
                ]}],
            },
        )
        r.raise_for_status()
        parts = [b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text"]
        return " ".join(parts).strip() or None
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.warning("Descrição da imagem indisponível: %s", exc)
        return None
