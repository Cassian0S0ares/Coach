"""Cliente mínimo da API da Riot (Account-V1 e Match-V5)."""
import time
from urllib.parse import quote

import requests

# plataforma -> cluster regional usado pelo Match-V5 / Account-V1
CLUSTERS = {
    "br1": "americas", "na1": "americas", "la1": "americas", "la2": "americas",
    "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe",
    "kr": "asia", "jp1": "asia",
    "oc1": "sea", "ph2": "sea", "sg2": "sea", "th2": "sea", "tw2": "sea", "vn2": "sea",
}


class RiotError(Exception):
    pass


class RiotClient:
    def __init__(self, api_key: str, platform: str = "br1"):
        if platform not in CLUSTERS:
            raise RiotError(f"Região desconhecida: {platform}")
        self.cluster = CLUSTERS[platform]
        self.session = requests.Session()
        self.session.headers["X-Riot-Token"] = api_key

    def _get(self, path: str, params: dict | None = None):
        url = f"https://{self.cluster}.api.riotgames.com{path}"
        for _ in range(3):
            r = self.session.get(url, params=params, timeout=15)
            if r.status_code == 429:  # rate limit
                time.sleep(int(r.headers.get("Retry-After", "2")))
                continue
            if r.status_code in (401, 403):
                raise RiotError("API key inválida ou expirada (chaves de dev expiram a cada 24h).")
            if r.status_code == 404:
                raise RiotError(f"Não encontrado: {path}")
            r.raise_for_status()
            return r.json()
        raise RiotError("Rate limit da Riot excedido, tente novamente em instantes.")

    def get_puuid(self, game_name: str, tag_line: str) -> str:
        data = self._get(
            f"/riot/account/v1/accounts/by-riot-id/{quote(game_name)}/{quote(tag_line)}"
        )
        return data["puuid"]

    def get_match_ids(self, puuid: str, count: int = 1, queue: int | None = None) -> list[str]:
        params = {"start": 0, "count": count}
        if queue:
            params["queue"] = queue  # 420 = ranqueada solo/duo, 440 = flex
        return self._get(f"/lol/match/v5/matches/by-puuid/{puuid}/ids", params)

    def get_match(self, match_id: str) -> dict:
        return self._get(f"/lol/match/v5/matches/{match_id}")

    def get_timeline(self, match_id: str) -> dict:
        return self._get(f"/lol/match/v5/matches/{match_id}/timeline")
