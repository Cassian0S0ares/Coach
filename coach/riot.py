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
    def __init__(self, api_key: str, platform: str = "br1", min_interval: float = 0.0):
        if platform not in CLUSTERS:
            raise RiotError(f"Região desconhecida: {platform}")
        self.platform = platform
        self.cluster = CLUSTERS[platform]
        self.min_interval = min_interval  # pausa mínima entre chamadas (respeita chave de desenvolvimento)
        self._last_call = 0.0
        self.session = requests.Session()
        self.session.headers["X-Riot-Token"] = api_key

    def _get(self, path: str, params: dict | None = None, host: str | None = None):
        url = f"https://{host or self.cluster}.api.riotgames.com{path}"
        for _ in range(3):
            wait = self._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            r = self.session.get(url, params=params, timeout=15)
            if r.status_code == 429:  # rate limit
                time.sleep(int(r.headers.get("Retry-After", "2")))
                continue
            if r.status_code == 401:
                raise RiotError("A Riot não recebeu a chave da API (HTTP 401). Verifique RIOT_API_KEY no .env.")
            if r.status_code == 403:
                raise RiotError("A Riot recusou o acesso (HTTP 403). Confira a chave e as permissões no portal de desenvolvedores.")
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

    # --- League-V4 / Summoner-V4 (usam o host da plataforma, ex.: br1) ---
    def get_top_league(self, tier: str = "challenger", queue: str = "RANKED_SOLO_5x5") -> list[dict]:
        """Entradas da liga de elite (challenger/grandmaster/master), ordenadas por PDL."""
        data = self._get(f"/lol/league/v4/{tier}leagues/by-queue/{queue}", host=self.platform)
        return sorted(data.get("entries", []), key=lambda e: e.get("leaguePoints", 0), reverse=True)

    def get_puuid_by_summoner(self, summoner_id: str) -> str:
        return self._get(f"/lol/summoner/v4/summoners/{summoner_id}", host=self.platform)["puuid"]

    def get_match_ids_since(self, puuid: str, count: int, queue: int = 420, start_time: int | None = None) -> list[str]:
        params = {"start": 0, "count": count, "queue": queue}
        if start_time:
            params["startTime"] = start_time
        return self._get(f"/lol/match/v5/matches/by-puuid/{puuid}/ids", params)
