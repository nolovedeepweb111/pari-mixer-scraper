from __future__ import annotations

import time

import requests

BASE_URL = "https://api.steampowered.com/IDOTA2Match_570"

# Sentinel Valve uses for a player who has hidden their match history.
ANONYMOUS_ACCOUNT_ID = 4294967295


class SteamClient:
    """Wrapper around Valve's official Dota 2 Web API (GetMatchHistory), the
    same data source the in-game client and Dota Plus use. Requires a free
    Steam Web API key: https://steamcommunity.com/dev/apikey
    """

    def __init__(self, api_key: str, session: requests.Session | None = None, min_interval: float = 1.0):
        self.api_key = api_key
        self.session = session or requests.Session()
        self.min_interval = min_interval
        self._last_request = 0.0

    def _get(self, method: str, version: str, params: dict) -> dict:
        query = {**params, "key": self.api_key, "format": "json"}
        url = f"{BASE_URL}/{method}/{version}/"

        for attempt in range(5):
            elapsed = time.monotonic() - self._last_request
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)

            resp = self.session.get(url, params=query, timeout=30)
            self._last_request = time.monotonic()

            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue

            resp.raise_for_status()
            return resp.json()["result"]

        resp.raise_for_status()

    def iter_league_matches(self, league_id: int, matches_per_page: int = 100):
        """Yields normalized match dicts for a league, paging backwards
        through match history via start_at_match_id until exhausted."""
        start_at_match_id = None
        while True:
            params = {
                "league_id": league_id,
                "matches_requested": matches_per_page,
                "tournament_games_only": 1,
            }
            if start_at_match_id is not None:
                params["start_at_match_id"] = start_at_match_id

            result = self._get("GetMatchHistory", "v1", params)
            matches = result.get("matches", [])
            if not matches:
                return

            for m in matches:
                yield normalize_match(m)

            if result.get("results_remaining", 0) <= 0:
                return
            start_at_match_id = matches[-1]["match_id"] - 1

    def get_team_info(self, team_id: int) -> dict | None:
        result = self._get("GetTeamInfo", "v1", {"team_id": team_id})
        teams = result.get("teams", [])
        return teams[0] if teams else None


def normalize_match(m: dict) -> dict:
    """Convert a GetMatchHistory match entry into the common shape shared
    with OpenDota-sourced matches (see opendota-side normalize in collect.py)."""
    players = []
    for p in m.get("players", []):
        account_id = p.get("account_id")
        if account_id is None or account_id == ANONYMOUS_ACCOUNT_ID:
            continue
        players.append({
            "account_id": account_id,
            "hero_id": p.get("hero_id"),
            "is_radiant": p.get("player_slot", 0) < 128,
            "kills": None,
            "deaths": None,
            "assists": None,
        })

    return {
        "match_id": m["match_id"],
        "start_time": m.get("start_time"),
        "duration": None,
        "radiant_team_id": m.get("radiant_team_id") or None,
        "dire_team_id": m.get("dire_team_id") or None,
        "radiant_win": None,
        "players": players,
    }
