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

_TOURNAMENTS_QUERY = """
query Tournaments($first: Int) {
    tournaments(first: $first) {
        items {
            id
            name
            status
        }
    }
}
"""

_WEEKS_QUERY = """
query TournamentWeeks($tournamentId: Int!) {
    tournamentWeeks(tournamentId: $tournamentId) {
        id
        weekNumber
        status
        startTime
        endTime
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
            __WEEK_FIELD__
            players { id nickname proName steamAvatar rating preferredRoles }
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
            __WEEK_FIELD__
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

_PARTICIPANT_QUEUE_QUERY = """
query ParticipantList($tournamentId: Int!, $first: Int, $offset: Int, $filters: ParticipantFilterInput) {
    participantList(tournamentId: $tournamentId, first: $first, offset: $offset, filters: $filters) {
        pageInfo { total }
        items {
            queuePosition
            player { id nickname rating }
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
            user { id nickname rating }
        }
    }
}
"""


class MixerCupClient:
    """Client for mixer-cup.gg's public GraphQL API - used to pull the
    tournament's real team names and current rosters, which aren't
    registered anywhere in Steam/OpenDota for ad-hoc mixer teams."""

    def __init__(self, base_url: str = BASE_URL, session: requests.Session | None = None,
                 min_interval: float = 0.3, id_offset: int = 0, weeks: bool = False):
        self.base_url = base_url
        # Копий этой платформы теперь две, и нумерация турниров у каждой
        # своя, с единицы. Сдвиг разводит их в общем пространстве номеров:
        # наружу клиент отдаёт номер со сдвигом, в запросы подставляет
        # исходный. Так остальному коду второй источник не виден. См.
        # sources.py.
        self.id_offset = id_offset
        # Недели (еженедельные решафлы) есть только у той копии платформы, где
        # они заведены. У api.mixer-cup.gg схема старее: запрос с полями weekId
        # и weekNumber отвечает 400 и роняет сбор целиком - проверено. Поэтому
        # поля подставляются в запрос только для источников, где они есть.
        self.weeks = weeks
        self._week_numbers: dict[int, dict[str, int]] = {}
        self.session = session or requests.Session()
        self.min_interval = min_interval
        self._last_request = 0.0

    def _q(self, query: str, week_field: str) -> str:
        """Подставляет поле недели в запрос - или убирает его, если источник
        про недели не знает (см. self.weeks)."""
        return query.replace("__WEEK_FIELD__", week_field if self.weeks else "")

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

    def _local_id(self, tournament_id: int) -> int:
        return tournament_id - self.id_offset

    def _with_global_id(self, item: dict | None) -> dict | None:
        if not item or item.get("id") is None or not self.id_offset:
            return item
        return {**item, "id": item["id"] + self.id_offset}

    def get_active_tournament(self) -> dict | None:
        data = self._post(_ACTIVE_TOURNAMENT_QUERY)
        return self._with_global_id(data.get("activeTournament"))

    def list_tournaments(self, first: int = 20) -> list[dict]:
        """Every tournament mixer-cup knows, newest id first: {id, name,
        status}. status is ACTIVE / COMPLETE / REDUCTION (a cup still forming).

        This is the only non-circular way to learn that a PAST cup exists.
        Our own database can't tell us after the disk is wiped: teams all end
        up owned by the active cup (it reclaims every reused Steam id), and a
        freshly fetched match carries no tournament yet - that gets stamped BY
        linking the very cup we'd be trying to discover."""
        data = self._post(_TOURNAMENTS_QUERY, {"first": first})
        items = (data.get("tournaments") or {}).get("items") or []
        return sorted(
            (self._with_global_id(t) for t in items if t.get("id") is not None),
            key=lambda t: t["id"], reverse=True,
        )

    def list_weeks(self, tournament_id: int) -> list[dict]:
        """Недели турнира: {id, weekNumber, status, startTime, endTime}.

        Супермиксер WINLINE перетасовывает составы каждый понедельник, и у них
        это оформлено неделями: у команды есть weekId, у игры - weekNumber.
        Команда живёт одну неделю, то есть после решафла появляется новый
        набор команд, а не меняется состав прежних."""
        if not self.weeks:
            return []
        tournament_id = self._local_id(tournament_id)
        data = self._post(_WEEKS_QUERY, {"tournamentId": tournament_id})
        return data.get("tournamentWeeks") or []

    def _week_number_by_id(self, local_tournament_id: int) -> dict[str, int]:
        """weekId -> weekNumber. У команды в ответе только идентификатор недели,
        а работать удобнее с номером. Кэшируется на время жизни клиента: за один
        прогон сбора недели не меняются.

        Принимает номер турнира УЖЕ без сдвига источника - вызывается из мест,
        которые его сняли: list_weeks снял бы его второй раз, и запрос ушёл бы
        с отрицательным номером."""
        if not self.weeks:
            return {}
        if local_tournament_id not in self._week_numbers:
            try:
                data = self._post(_WEEKS_QUERY, {"tournamentId": local_tournament_id})
                self._week_numbers[local_tournament_id] = {
                    w["id"]: w["weekNumber"]
                    for w in (data.get("tournamentWeeks") or [])
                    if w.get("id") and w.get("weekNumber") is not None
                }
            except Exception:
                self._week_numbers[local_tournament_id] = {}
        return self._week_numbers[local_tournament_id]

    def iter_teams(self, tournament_id: int, page_size: int = 50):
        tournament_id = self._local_id(tournament_id)
        offset = 0
        while True:
            data = self._post(self._q(_TEAMS_QUERY, "weekId"), {
                "filters": {"tournamentId": tournament_id},
                "first": page_size,
                "offset": offset,
            })
            result = data["teams"]
            items = result["items"]
            # Неделя приходит идентификатором, а наружу удобнее отдавать номер:
            # хранить и сравнивать проще, и в базе он читаемее uuid.
            weeks = self._week_number_by_id(tournament_id) if self.weeks else {}
            for team in items:
                if weeks:
                    team["weekNumber"] = weeks.get(team.get("weekId"))
                for player in team["players"]:
                    player["account_id"] = steam_account_id_from_avatar_url(player.get("steamAvatar"))
            yield from items
            offset += len(items)
            if not items or offset >= result["pageInfo"]["totalFiltered"]:
                return

    def iter_completed_games(self, tournament_id: int, page_size: int = 100):
        tournament_id = self._local_id(tournament_id)
        offset = 0
        while True:
            data = self._post(self._q(_GAMES_QUERY, "weekNumber"), {
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
        tournament_id = self._local_id(tournament_id)
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
        tournament_id = self._local_id(tournament_id)
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
                    "player_uuid": user.get("id"),
                    "nickname": user.get("nickname"),
                    "rating": user.get("rating"),
                    "occurred_at": e["createdAt"],
                }
            offset += len(items)
            if not items:
                return

    def iter_queue_participants(self, tournament_id: int, page_size: int = 100):
        """Current substitute queue (participants with status BID), in queue
        order. A player disappears from here the moment they're picked into
        a team, so the caller snapshots this regularly (see
        collect.sync_queue_snapshot) to know later what position a player
        held right before being substituted in."""
        tournament_id = self._local_id(tournament_id)
        offset = 0
        while True:
            data = self._post(_PARTICIPANT_QUEUE_QUERY, {
                "tournamentId": tournament_id,
                "filters": {"status": ["BID"]},
                "first": page_size,
                "offset": offset,
            })
            result = data["participantList"]
            items = result["items"]
            for p in items:
                player = p.get("player") or {}
                yield {
                    "player_uuid": player.get("id"),
                    "nickname": player.get("nickname"),
                    "rating": player.get("rating"),
                    "queue_position": p.get("queuePosition"),
                }
            offset += len(items)
            # participantList's pageInfo.total is the size of the *current
            # page* (min(first, remaining)), unlike games/teams where it's
            # the overall filtered count - so a short page is the only
            # reliable end-of-list signal here.
            if len(items) < page_size:
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
                    "queue_position": None,
                    "at": pending_off["occurred_at"],
                })
            pending_off = e
        else:  # PLAYER_IN
            if pending_off is not None:
                swaps.append({
                    "out": pending_off["nickname"], "out_rating": pending_off["rating"],
                    "in": e["nickname"], "in_rating": e["rating"],
                    "rating_diff": rating_diff(pending_off["rating"], e["rating"]),
                    "queue_position": e.get("queue_position"),
                    "at": e["occurred_at"],
                })
                pending_off = None
            else:
                swaps.append({
                    "out": None, "out_rating": None,
                    "in": e["nickname"], "in_rating": e["rating"], "rating_diff": None,
                    "queue_position": e.get("queue_position"),
                    "at": e["occurred_at"],
                })
    if pending_off is not None:
        swaps.append({
            "out": pending_off["nickname"], "out_rating": pending_off["rating"],
            "in": None, "in_rating": None, "rating_diff": None,
            "queue_position": None,
            "at": pending_off["occurred_at"],
        })
    return swaps
