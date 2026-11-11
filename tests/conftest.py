import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "agent")]
os.environ.update({
    "VIGIA_ENV": "dev", "DATABASE_URL": os.getenv("TEST_DATABASE_URL", "sqlite:///:memory:"),
    "WORKER_ENABLED": "0",
    "WHATSAPP_PROVIDER": "console", "META_APP_SECRET": "app-secret", "META_VERIFY_TOKEN": "verify-me",
    "WHATSAPP_WEBHOOK_SECRET": "hook-secret", "SITE_NAME": "Loja Teste",
})
