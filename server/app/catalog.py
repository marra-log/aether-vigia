"""Tipos de ocorrência: gravidade, texto ao dono e regras de envio."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EventType:
    severity: str
    title: str
    detail: str
    advice: str
    hold_s: int = 0            # espera antes de avisar (evita alarme por piscada)
    cooldown_s: int = 900      # intervalo mínimo entre alertas iguais da mesma câmera
    notify_resolved: bool = True
    kind: str = "sinal"        # sinal|seguranca|comportamento


CATALOG = {
    "OFFLINE": EventType("critical", "Câmera fora do ar",
        "Parei de receber imagem desta câmera.",
        "Confira energia e cabo de rede da câmera.", hold_s=60, cooldown_s=1800),
    "FROZEN": EventType("critical", "Imagem travada",
        "A câmera continua conectada, mas a imagem parou de mudar.",
        "Reinicie a câmera ou o gravador."),
    "BLACK": EventType("critical", "Imagem preta",
        "O vídeo chega totalmente escuro.",
        "Verifique o infravermelho, a lente e o cabo de vídeo."),
    "OBSTRUCTED": EventType("critical", "Câmera tampada ou bloqueada",
        "A imagem perdeu quase todo o detalhe de repente.",
        "Veja se algo cobriu a lente ou se houve sabotagem.", kind="seguranca"),
    "MOVED": EventType("critical", "Câmera foi movida",
        "O enquadramento mudou em relação ao que costuma ser.",
        "Confira se alguém mexeu na câmera.", kind="seguranca"),
    "BLUR": EventType("warning", "Imagem desfocada",
        "A nitidez caiu bem abaixo do normal desta câmera.",
        "Limpe a lente e confira o foco.", cooldown_s=21600),
    "FLICKER": EventType("warning", "Imagem piscando",
        "O brilho da imagem está oscilando sem parar.",
        "Pode ser fonte de energia fraca ou iluminação ao redor.", cooldown_s=7200),
    "UNSTABLE": EventType("warning", "Sinal oscilando",
        "A conexão desta câmera está caindo ou falhando repetidamente.",
        "Costuma ser cabo, fonte ou Wi-Fi fraco.", cooldown_s=7200),
    "INTRUSION": EventType("critical", "Pessoa em área protegida",
        "Uma pessoa foi detectada na área e no horário que você protegeu.",
        "Olhe a imagem e acione quem for preciso.", cooldown_s=180,
        notify_resolved=False, kind="seguranca"),
    "LOITERING": EventType("warning", "Pessoa parada por muito tempo",
        "Uma pessoa está no campo de visão há mais tempo que o limite.",
        "Olhe a imagem para ver se é normal.", cooldown_s=900,
        notify_resolved=False, kind="seguranca"),
    "BEHAVIOR_SPIKE": EventType("warning", "Movimento fora do normal",
        "Há muito mais movimento do que esta câmera costuma ter neste horário.",
        "Olhe a imagem para ver o que está acontecendo.", cooldown_s=1800,
        notify_resolved=False, kind="comportamento"),
    "BEHAVIOR_QUIET": EventType("info", "Movimento abaixo do normal",
        "O local está bem mais parado do que costuma estar neste horário.",
        "Confira se está tudo funcionando normalmente.", cooldown_s=14400,
        notify_resolved=False, kind="comportamento"),
    "MANUAL": EventType("warning", "Anomalia apontada pelo operador",
        "O operador viu algo fora do normal nesta câmera.",
        "Veja a observação e a imagem.", cooldown_s=0, notify_resolved=False, kind="operador"),
    "SITE_DOWN": EventType("critical", "Várias câmeras caíram juntas",
        "As câmeras pararam ao mesmo tempo.",
        "Provável queda de energia, internet ou gravador.", cooldown_s=1800),
    "AGENT_OFFLINE": EventType("critical", "Perdi contato com o local",
        "O equipamento que analisa as câmeras parou de responder.",
        "Provável queda de internet ou de energia no local.", cooldown_s=1800),
}

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
SEVERITY_ICON = {"info": "🔵", "warning": "🟠", "critical": "🔴"}
SEVERITY_LABEL = {"info": "Informação", "warning": "Atenção", "critical": "Crítico"}
