"""Processo do agente: mantém uma thread por câmera, conversa com o servidor."""
from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Dict
from zoneinfo import ZoneInfo

from .detector import load_detector
from .uplink import Uplink
from .worker import CameraWorker

VERSION = "1.0.0"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vigia.agent")


def main() -> None:
    parser = argparse.ArgumentParser(description="Agente Aether Vigia")
    parser.add_argument("--server", default=os.getenv("VIGIA_SERVER", "http://localhost:8000"))
    parser.add_argument("--token", default=os.getenv("AGENT_TOKEN", ""))
    parser.add_argument("--model", default=os.getenv("VIGIA_MODEL", "/app/models/yolox_nano.onnx"))
    parser.add_argument("--replay", action="store_true",
                        help="testa com arquivos de vídeo, usando o tempo do próprio vídeo")
    parser.add_argument("--replay-start", type=float, default=None,
                        help="horário (epoch) em que o vídeo teria começado")
    args = parser.parse_args()
    if not args.token:
        raise SystemExit("Defina AGENT_TOKEN")

    uplink = Uplink(args.server, args.token)
    detector = load_detector(args.model)
    allow_files = args.replay or os.getenv("VIGIA_ALLOW_FILES") == "1"
    workers: Dict[int, CameraWorker] = {}
    tz = ZoneInfo("America/Sao_Paulo")
    last_config, last_beat = 0.0, 0.0
    log.info("Agente iniciado: servidor %s", args.server)

    while True:
        now = time.time()
        if now - last_config >= 30 or last_config == 0:
            config = uplink.get_config()
            if config:
                last_config = now
                tz = ZoneInfo(config.get("timezone", "America/Sao_Paulo"))
                wanted = {c["id"]: c for c in config["cameras"]}
                for cam_id in list(workers):
                    if cam_id not in wanted or workers[cam_id].cfg.get("url") != wanted[cam_id].get("url"):
                        log.info("Parando câmera %s", cam_id)
                        workers.pop(cam_id).stop()
                for cam_id, cfg in wanted.items():
                    if args.replay and args.replay_start:
                        cfg = {**cfg, "replay_start": args.replay_start}
                    if cam_id in workers:
                        workers[cam_id].update_config(cfg)
                    else:
                        log.info("Iniciando câmera %s (%s)", cam_id, cfg["name"])
                        workers[cam_id] = CameraWorker(cfg, uplink, detector, tz, args.replay, allow_files)
                        workers[cam_id].start()
        if now - last_beat >= 15:
            last_beat = now
            uplink.heartbeat({"agent_id": os.uname().nodename, "version": VERSION, "cameras": [
                {"id": w.cam_id, "state": w.state} for w in workers.values()]})
        uplink.flush()
        if args.replay and workers and all(w.state == "finished" for w in workers.values()):
            uplink.flush()
            log.info("Replay concluído")
            return
        time.sleep(0.5)


if __name__ == "__main__":
    main()
