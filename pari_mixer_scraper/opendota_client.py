from __future__ import annotations

import time

import requests

BASE_URL = "https://api.opendota.com/api"


class OpenDotaClient:
    """Thin wrapper around the public OpenDota API with basic rate limiting
    and retry-on-429 handling (free tier: 60 req/min, 50k req/month)."""

    def __init__(self, base_url: str = BASE_URL, min_interval: float = 1.2, session: requests.Session | None = None):
        self.base_url = base_url
        self.min_interval = min_interval
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "pari-mixer-scraper/1.0"})
        self._last_request = 0.0

    def _get(self, path: str, params: dict | None = None):
        for attempt in range(5):
            elapsed = time.monotonic() - self._last_request
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)

            resp = self.session.get(f"{self.base_url}{path}", params=params, timeout=30)
            self._last_request = time.monotonic()

            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue

            resp.raise_for_status()
            return resp.json()

        resp.raise_for_status()

    def get_league(self, league_id: int):
        return self._get(f"/leagues/{league_id}")

    def get_league_matches(self, league_id: int):
        return self._get(f"/leagues/{league_id}/matches")

    def get_match(self, match_id: int):
        return self._get(f"/matches/{match_id}")

    def get_heroes(self):
        return self._get("/heroes")

    def get_pro_players(self):
        return self._get("/proPlayers")

    def get_team(self, team_id: int):
        return self._get(f"/teams/{team_id}")

    def get_player(self, account_id: int):
        return self._get(f"/players/{account_id}")
