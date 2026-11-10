"""Portuguese narration through the online Edge speech service (no API key)."""
import asyncio

VOICES = {"pt-BR-AntonioNeural", "pt-BR-FranciscaNeural"}
MAX_TEXT = 20000


def synthesize(data: dict) -> bytes:
    text = data.get("text")
    voice = data.get("voice", "pt-BR-AntonioNeural")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ValueError(f"Informe um texto de até {MAX_TEXT} caracteres.")
    if not isinstance(voice, str) or voice not in VOICES:
        raise ValueError("Voz inválida.")
    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError("Instale as dependências e reinicie o servidor para ativar a fala.") from exc

    async def generate():
        audio = bytearray()
        async for chunk in edge_tts.Communicate(text.strip(), voice).stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
        if not audio:
            raise RuntimeError("O serviço de voz não retornou áudio.")
        return bytes(audio)

    try:
        return asyncio.run(asyncio.wait_for(generate(), timeout=90))
    except Exception as exc:
        raise RuntimeError("O serviço de voz está indisponível. Verifique a internet e tente novamente.") from exc
