"""Gera o feedback de coach com o Gemini (plano gratuito) a partir do relatório da partida."""
import json
import os
import time
from dataclasses import asdict

import requests

# Modelo gratuito. Se der erro de modelo, tente "gemini-2.0-flash" ou "gemini-2.5-flash-lite".
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SYSTEM = """Você é um coach de League of Legends experiente e direto.
Receberá métricas de UMA partida do jogador (KDA, farm, visão, diferenças contra o oponente de lane,
momento das mortes e problemas já detectados automaticamente).
Responda em português do Brasil, em Markdown, com:
1. **Resumo** da partida em 2 linhas.
2. **Onde você errou** — no máximo 3 pontos, priorizados por impacto, sempre citando os números.
3. **O que fez bem** — 1 ou 2 pontos.
4. **Foco para a próxima partida** — UMA meta concreta e mensurável (ex.: "70 CS aos 10 min").
Não invente eventos que não estejam nos dados. Se um dado estiver ausente, ignore-o.

Sobre os campos de macro:
- `teamfights`: lutas com 3+ abates. `absent`=você estava longe, vivo e fora da luta; `i_died_first`=primeira morte da luta;
  `objective_after`=objetivo tomado até 90s depois.
- `deaths`: `zone` (território inimigo/rio-centro/aliado), `enemies_involved`, `allies_nearby` (aliados a <3000 unidades),
  `followed_by_enemy_objective`.
- `objectives`: `present` = você estava a <4500 unidades; `i_was_dead` = morreu nos 45s anteriores.
Limites: a posição é amostrada 1x por minuto (interpolada), então é aproximada; não há dados de wards no mapa nem de
habilidades/flash. Leve a ROLE em conta: um top ausente de um dragão pode estar certo (split/TP); um jungler ou suporte, não.
Não critique posicionamento fino dentro da luta — você não tem esse dado."""


def coach_feedback(report) -> str:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("Defina GEMINI_API_KEY no .env (https://aistudio.google.com/apikey).")

    body = {
        "system_instruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [
            {"text": json.dumps(asdict(report), ensure_ascii=False, indent=2)}
        ]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 2000},
    }
    for attempt in range(3):
        r = requests.post(URL.format(model=MODEL), json=body, timeout=60,
                          headers={"x-goog-api-key": key})
        if r.status_code == 429 and attempt < 2:  # limite do plano gratuito
            time.sleep(15 * (attempt + 1))
            continue
        if not r.ok:
            raise RuntimeError(f"Gemini {r.status_code}: {r.text[:300]}")
        break

    cand = r.json().get("candidates", [{}])[0]
    parts = cand.get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts)
    return text or f"(Gemini não retornou texto; finishReason={cand.get('finishReason')})"
