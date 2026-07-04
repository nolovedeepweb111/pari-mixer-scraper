from __future__ import annotations

import re
import time

import requests

from .http_utils import call_with_timeout

BASE_URL = "https://api.mixer-cup.gg"

# SteamID64 -> SteamID32 (Dota account_id) offset.
STEAM_ID64_BASE = 76561197960265728

_AVATAR_STEAM_ID_RE = re.compile(r"/avatars/(\d+)\.")


def steam_account_id_from_avatar_url(url: str | None) -> int | None:
    """MixerCup's steamAvatar field is a signed URL to a copy of the
    player's Steam avatar, filed under their SteamID64 - e.g.
    '.../avatars/76561199130942974.jpg?...'. That's the only place the
    public API exposes a Steam identity for an arbitrary player, so we
    extract it here to link mixer-cup players to Dota account_ids exactly,
    instead of matching on nickname text (which can differ between a
    player's live Steam persona name and their mixer-cup registration)."""
    if not url:
        return None
    m = _AVATAR_STEAM_ID_RE.search(url)
    if not m:
        return None
    steam_id64 = int(m.group(1))
    return steam_id64 - STEAM_ID64_BASE

_ACTIVE_TOURNAMENT_QUERY = """
query ActiveTournament {
    activeTournament {
        id
        name
        status
    }
}
"""

_TEAMS_QUERY = """
query Teams($filters: TeamFilterInput!, $first: Int, $offset: Int) {
    teams(first: $first, offset: $offset, filters: $filters) {
        pageInfo { totalFiltered }
        items {
            id
            name
            number
            players { id nickname proName steamAvatar rating }
        }
    }
}
"""

_GAMES_QUERY = """
query Games($first: Int, $offset: Int, $filters: GameFilterInput) {
    games(first: $first, offset: $offset, filters: $filters) {
        pageInfo { total }
        items {
            id
            status
            matchId
            result
            team1 { id number name }
            team2 { id number name }
        }
    }
}
"""

_NEXT_GAME_QUERY = """
query Games($first: Int, $filters: GameFilterInput) {
    games(first: $first, filters: $filters) {
        items {
            id
            status
            plannedTime
            team1 { id name }
            team2 { id name }
        }
    }
}
"""


class MixerCupClient:
    """Client for mixer-cup.gg's public GraphQL API - used to pull the
    tournament's real team names and current rosters, which aren't
    registered anywhere in Steam/OpenDota for ad-hoc mixer teams."""

    def __init__(self, base_url: str = BASE_URL, session: requests.Session | None = None, min_interval: float = 0.3):
        self.base_url = base_url
        self.session = session or requests.Session()
        self.min_interval = min_interval
        self._last_request = 0.0

    def _post(self, query: str, variables: dict | None = None) -> dict:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

        resp = call_with_timeout(
            lambda: self.session.post(
                self.base_url,
                json={"query": query, "variables": variables or {}},
                headers={"Content-Type": "application/json"},
                timeout=30,
            ),
            timeout=35,
        )
        self._last_request = time.monotonic()
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"MixerCup GraphQL error: {body['errors']}")
        return body["data"]

    def get_active_tournament(self) -> dict | None:
        data = self._post(_ACTIVE_TOURNAMENT_QUERY)
        return data.get("activeTournament")

    def iter_teams(self, tournament_id: int, page_size: int = 50):
        offset = 0
        while True:
            data = self._post(_TEAMS_QUERY, {
                "filters": {"tournamentId": tournament_id},
                "first": page_size,
                "offset": offset,
            })
            result = data["teams"]
            items = result["items"]
            for team in items:
                for player in team["players"]:
                    player["account_id"] = steam_account_id_from_avatar_url(player.get("steamAvatar"))
            yield from items
            offset += len(items)
            if not items or offset >= result["pageInfo"]["totalFiltered"]:
                return

    def iter_completed_games(self, tournament_id: int, page_size: int = 100):
        offset = 0
        while True:
            data = self._post(_GAMES_QUERY, {
                "filters": {"tournamentId": tournament_id, "status": ["COMPLETE"]},
                "first": page_size,
                "offset": offset,
            })
            result = data["games"]
            items = result["items"]
            yield from items
            offset += len(items)
            if not items or offset >= result["pageInfo"]["total"]:
                return

    def get_next_opponent(self, tournament_id: int, team_uuid: str) -> dict | None:
        """Next not-yet-played game for this team, or None if there isn't
        one (bracket finished, or team has none scheduled yet)."""
        data = self._post(_NEXT_GAME_QUERY, {
            "filters": {
                "tournamentId": tournament_id,
                "teamId": team_uuid,
                "status": ["PENDING", "ACTIVE", "PAUSED", "ON_HOLD"],
            },
            "first": 50,
        })
        games = data["games"]["items"]
        if not games:
            return None

        def sort_key(g):
            return (g.get("plannedTime") is None, g.get("plannedTime") or "")

        games.sort(key=sort_key)
        game = games[0]
        opponent = game["team2"] if game["team1"]["id"] == team_uuid else game["team1"]
        return {
            "opponent_mixer_uuid": opponent["id"],
            "opponent_name": opponent.get("name") or f"Team {opponent['id']}",
            "planned_time": game.get("plannedTime"),
            "status": game.get("status"),
        }
