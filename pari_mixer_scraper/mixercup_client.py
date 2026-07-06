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

_TOURNAMENT_EVENTS_QUERY = """
query TournamentEvents($filters: TournamentEventFilterInput, $first: Int, $offset: Int) {
    tournamentEvents(filters: $filters, first: $first, offset: $offset, sort: [CREATED_AT]) {
        items {
            id
            type
            createdAt
            user { nickname rating }
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

    def iter_substitution_events(self, tournament_id: int, team_uuid: str, page_size: int = 100):
        """Raw PLAYER_IN/PLAYER_OFF events for this team, oldest first.
        mixer-cup.gg's own substitution history has been observed to
        disappear periodically, so the caller is expected to persist these
        (see collect.sync_substitution_history) rather than display them
        live - event id is mixer-cup.gg's own UUID, stable enough to
        dedupe against on repeat syncs."""
        offset = 0
        while True:
            data = self._post(_TOURNAMENT_EVENTS_QUERY, {
                "filters": {
                    "tournamentId": tournament_id,
                    "teamId": team_uuid,
                    "type": ["PLAYER_IN", "PLAYER_OFF"],
                },
                "first": page_size,
                "offset": offset,
            })
            items = data["tournamentEvents"]["items"]
            for e in items:
                user = e.get("user") or {}
                yield {
                    "event_id": e["id"],
                    "type": e["type"],
                    "nickname": user.get("nickname"),
                    "rating": user.get("rating"),
                    "occurred_at": e["createdAt"],
                }
            offset += len(items)
            if not items:
                return


def pair_substitution_events(events: list[dict]) -> list[dict]:
    """This tournament's format allows swapping a player mid-run; MixerCup
    logs every swap as a PLAYER_OFF event immediately followed by a
    PLAYER_IN event. Takes events sorted oldest-first (type/nickname/
    rating/occurred_at, as stored in SubstitutionEvent) and returns them
    paired up as {out, out_rating, in, in_rating, rating_diff, at} -
    unpaired events (e.g. an OFF with no matching IN yet) are returned with
    the other side set to None. rating_diff is in_rating - out_rating
    (positive means the team traded up in rating) when both are known."""
    def rating_diff(out_rating, in_rating):
        if out_rating is None or in_rating is None:
            return None
        return round(in_rating - out_rating)

    swaps = []
    pending_off = None
    for e in events:
        if e["type"] == "PLAYER_OFF":
            if pending_off is not None:
                swaps.append({
                    "out": pending_off["nickname"], "out_rating": pending_off["rating"],
                    "in": None, "in_rating": None, "rating_diff": None,
                    "at": pending_off["occurred_at"],
                })
            pending_off = e
        else:  # PLAYER_IN
            if pending_off is not None:
                swaps.append({
                    "out": pending_off["nickname"], "out_rating": pending_off["rating"],
                    "in": e["nickname"], "in_rating": e["rating"],
                    "rating_diff": rating_diff(pending_off["rating"], e["rating"]),
                    "at": e["occurred_at"],
                })
                pending_off = None
            else:
                swaps.append({
                    "out": None, "out_rating": None,
                    "in": e["nickname"], "in_rating": e["rating"], "rating_diff": None,
                    "at": e["occurred_at"],
                })
    if pending_off is not None:
        swaps.append({
            "out": pending_off["nickname"], "out_rating": pending_off["rating"],
            "in": None, "in_rating": None, "rating_diff": None,
            "at": pending_off["occurred_at"],
        })
    return swaps
