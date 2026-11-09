"""Interface web local para o relatório do Coach de LoL.

Uso: python3 web.py (abra http://127.0.0.1:8000)
"""
import json
import os
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

from coach.analysis import analyze
from coach.feedback import coach_feedback
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

    report = analyze(riot.get_match(ids[0]), riot.get_timeline(ids[0]), puuid)
    feedback = None
    feedback_error = None
    if os.getenv("GEMINI_API_KEY"):
        try:
            feedback = coach_feedback(report)
        except Exception:
            feedback_error = "O parecer de IA não ficou disponível. As métricas da partida estão abaixo."
    else:
        feedback_error = "Configure GEMINI_API_KEY no .env para receber o parecer de IA."
    return {
        "riot_id": f"{name}#{tag}",
        "report": asdict(report),
        "feedback": feedback,
        "feedback_error": feedback_error,
    }


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
        if self.path != "/api/report":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("Requisição inválida.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Requisição inválida.")
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
    ThreadingHTTPServer((host, port), Handler).serve_forever()
