"""Configuração do servidor, lida das variáveis de ambiente."""
from __future__ import annotations

import os


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in {"1", "true", "sim", "yes", "on"}


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///./vigia.db")
    # A Railway entrega postgres:// ou postgresql://; o SQLAlchemy precisa do driver.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


class Settings:
    def __init__(self) -> None:
        self.env = os.getenv("VIGIA_ENV", "production")
        self.database_url = _database_url()
        self.secret_key = os.getenv("SECRET_KEY", "")
        self.owner_password = os.getenv("OWNER_PASSWORD", "")
        # Operador da empresa: vê tudo, revisa e aponta anomalias, mas não muda configurações.
        self.operator_password = os.getenv("OPERATOR_PASSWORD", "")
        # Com revisão ligada, alertas que não são críticos esperam o operador confirmar.
        self.operator_review = _bool("OPERATOR_REVIEW", False)
        self.review_timeout_s = int(os.getenv("REVIEW_TIMEOUT_MIN", "10")) * 60
        self.agent_token = os.getenv("AGENT_TOKEN", "")
        self.site_name = os.getenv("SITE_NAME", "Meu local")
        self.timezone = os.getenv("TIMEZONE", "America/Sao_Paulo")

        # WhatsApp
        self.wa_provider = os.getenv("WHATSAPP_PROVIDER", "console")  # console|baileys|meta|evolution|zapi
        self.wa_webhook_secret = os.getenv("WHATSAPP_WEBHOOK_SECRET", "")
        self.meta_token = os.getenv("META_TOKEN", "")
        self.meta_phone_id = os.getenv("META_PHONE_NUMBER_ID", "")
        self.meta_app_secret = os.getenv("META_APP_SECRET", "")
        self.meta_verify_token = os.getenv("META_VERIFY_TOKEN", "")
        self.meta_graph_version = os.getenv("META_GRAPH_VERSION", "v24.0")
        self.meta_template_lang = os.getenv("META_TEMPLATE_LANG", "pt_BR")
        self.evolution_url = os.getenv("EVOLUTION_URL", "").rstrip("/")
        self.evolution_key = os.getenv("EVOLUTION_API_KEY", "")
        self.evolution_instance = os.getenv("EVOLUTION_INSTANCE", "")
        self.baileys_url = os.getenv("BAILEYS_URL", "").rstrip("/")
        self.baileys_key = os.getenv("BAILEYS_KEY", "")
        self.zapi_instance = os.getenv("ZAPI_INSTANCE", "")
        self.zapi_token = os.getenv("ZAPI_TOKEN", "")
        self.zapi_client_token = os.getenv("ZAPI_CLIENT_TOKEN", "")

        # Descrição das imagens por IA (opcional)
        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.vision_model = os.getenv("VISION_MODEL", "claude-haiku-5-5")

        # Regras
        self.heartbeat_timeout_s = int(os.getenv("HEARTBEAT_TIMEOUT_S", "120"))
        self.digest_hour = int(os.getenv("DIGEST_HOUR", "8"))
        self.retention_metrics_days = int(os.getenv("RETENTION_METRICS_DAYS", "30"))
        self.retention_snapshots_days = int(os.getenv("RETENTION_SNAPSHOTS_DAYS", "15"))
        self.retention_events_days = int(os.getenv("RETENTION_EVENTS_DAYS", "90"))
        self.worker_enabled = _bool("WORKER_ENABLED", True)

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"

    def validate(self) -> None:
        """Recusa subir em produção sem os segredos obrigatórios."""
        if self.is_dev:
            self.secret_key = self.secret_key or "dev-secret-key-nao-usar-em-producao"
            self.owner_password = self.owner_password or "dev"
            self.agent_token = self.agent_token or "dev-agent"
            return
        missing = [
            name
            for name, value in {
                "SECRET_KEY": self.secret_key,
                "OWNER_PASSWORD": self.owner_password,
                "AGENT_TOKEN": self.agent_token,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError("Faltam variáveis obrigatórias: " + ", ".join(missing))
        if len(self.secret_key) < 32:
            raise RuntimeError("SECRET_KEY precisa ter pelo menos 32 caracteres")


settings = Settings()
