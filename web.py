"""Interface web local para o relatório do Coach de LoL.

Uso: python3 web.py (abra http://127.0.0.1:8000)
"""
import json
import os
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

from coach import meta
from coach.analysis import analyze
from coach.feedback import coach_feedback
from coach.speech import synthesize
from coach.riot import CLUSTERS, RiotClient, RiotError


PAGE = Path(__file__).parent / "static" / "index.html"
ENV_FILE = Path(__file__).parent / ".env"
MAX_BODY = 4096


def generate_report(data: dict) -> dict:
    # A chave de desenvolvimento pode ser renovada enquanto o servidor está aberto.
    load_dotenv(ENV_FILE, override=True)
    riot_id = data.get("riot_id", "")
    region = data.get("region", "br1")
    if not isinstance(riot_id, str) or "#" not in riot_id or len(riot_id) > 100:
        raise ValueError('Informe o Riot ID no formato "Nome#TAG".')
    name, tag = (part.strip() for part in riot_id.rsplit("#", 1))
    if not name or not tag:
        raise ValueError('Informe o Riot ID no formato "Nome#TAG".')
    if not isinstance(region, str) or region not in CLUSTERS:
        raise ValueError("Região inválida.")
    key = os.getenv("RIOT_API_KEY")
    if not key:
        raise RuntimeError("Configure RIOT_API_KEY no arquivo .env.")

    riot = RiotClient(key, region)
    puuid = riot.get_puuid(name, tag)
    ids = riot.get_match_ids(puuid, count=1)
    if not ids:
        raise ValueError("Nenhuma partida recente encontrada para este jogador.")

    report = analyze(riot.get_match(ids[0]), riot.get_timeline(ids[0]), puuid, meta.load())
    feedback = None
    feedback_error = None
    if os.getenv("GEMINI_API_KEY"):
        try:
            feedback = coach_feedback(report)
        except Exception as exc:
            print(f"[gemini] falha ao gerar o parecer: {exc}")
            feedback_error = "O parecer de IA não ficou disponível. As métricas da partida estão abaixo."
    else:
        feedback_error = "Configure GEMINI_API_KEY no .env para receber o parecer de IA."
    return {
        "riot_id": f"{name}#{tag}",
        "report": asdict(report),
        "feedback": feedback,
        "feedback_error": feedback_error,
    }


def meta_loop() -> None:
    """Mantém o meta atualizado em segundo plano enquanto o servidor roda."""
    interval = int(os.getenv("META_REFRESH_HOURS", "6")) * 3600
    while True:
        load_dotenv(ENV_FILE, override=True)
        key = os.getenv("RIOT_API_KEY")
        if key:
            try:
                client = RiotClient(key, os.getenv("META_REGION", "br1"),
                                    min_interval=float(os.getenv("META_MIN_INTERVAL", "1.25")))
                meta.refresh(client, int(os.getenv("META_MATCHES_PER_REFRESH", "60")))
            except Exception as exc:
                print(f"[meta] falha na atualização: {exc}")
        time.sleep(interval)


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/static/coach-explaining.png":
            body = (PAGE.parent / "coach-explaining.png").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/meta":
            self.send_json(200, meta.status())
            return
        if self.path != "/":
            self.send_error(404)
            return
        body = PAGE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path not in ("/api/report", "/api/speech"):
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > (128000 if self.path == "/api/speech" else MAX_BODY):
                raise ValueError("Requisição inválida.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Requisição inválida.")
            if self.path == "/api/speech":
                audio = synthesize(data)
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(audio)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(audio)
                return
            result = generate_report(data)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
            return
        except RiotError as exc:
            self.send_json(502, {"error": str(exc)})
            return
        except RuntimeError as exc:
            self.send_json(503, {"error": str(exc)})
            return
        except Exception:
            self.send_json(500, {"error": "Não foi possível gerar o relatório agora. Verifique o servidor e tente novamente."})
            return
        self.send_json(200, result)


if __name__ == "__main__":
    load_dotenv(ENV_FILE, override=True)
    host = "127.0.0.1"
    port = int(os.getenv("PORT", "8000"))
    print(f"Coach de LoL: http://{host}:{port}")
    if os.getenv("META_AUTO_REFRESH", "1") != "0":
        threading.Thread(target=meta_loop, daemon=True).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()
