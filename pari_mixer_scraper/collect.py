from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from .mixercup_client import MixerCupClient
from .models import (
    Base, Hero, Match, MatchDraftEntry, MatchPlayer, Player, QueuedPlayer,
    SubstitutionEvent, Team,
    build_engine, configure_sqlite,
)
from .opendota_client import OpenDotaClient
from .roster_overrides import MANUAL_ROSTER_OVERRIDES
from .steam_client import SteamClient

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DEFAULT_LEAGUE_ID = 19924  # Pari Mixer Cup

ProgressFn = Callable[[str], None]

_HEROES_BUNDLE = Path(__file__).resolve().parent / "data" / "heroes.json"

# PARI Mixer Cup #1 (26) and #2 (27) run CONCURRENTLY - both have games in
# the same weeks and share dotabuff league 19924 - so a match's tournament
# can't be decided by time. It's decided by which mixer tournament's
# completed-games list contains the match (set in link_mixercup_data). We
# link every tournament we know about each cycle so both get their results.
# Extra ids can be forced via env MIXER_TOURNAMENT_IDS="26,27".


def sync_heroes(session: Session, client: OpenDotaClient | None = None, progress: ProgressFn | None = None) -> None:
    """Populates the hero list from a bundled snapshot shipped in the repo,
    not from OpenDota. The hero roster changes only a handful of times a
    year (a new hero release), but OpenDota's /heroes call was the single
    most frequent hang point for the collector on Render - and since it's
    the very first step, a hang there left a freshly redeployed (empty)
    database with no data at all until it cleared. Reading a static file
    is instant and can't hang. `client` is accepted for backward
    compatibility but no longer used; refresh the bundle by hand when a
    new hero ships."""
    progress = progress or (lambda msg: None)
    progress("  Loading hero list from bundled snapshot...")
    with open(_HEROES_BUNDLE, encoding="utf-8") as f:
        heroes = json.load(f)

    # Insert only the heroes we don't already have, in a single batched
    # write - not a per-row get()+add() loop. On a fresh (empty) database
    # that loop was 127 separate INSERTs each contending for SQLite's write
    # lock against the web server's concurrent reads on Render, and under
    # active polling it could stall indefinitely between them. The bundle
    # is static, so existing rows are already correct and need no update.
    existing_ids = {hid for (hid,) in session.execute(select(Hero.hero_id))}
    new_heroes = [
        Hero(hero_id=h["id"], name=h["name"], localized_name=h["localized_name"])
        for h in heroes if h["id"] not in existing_ids
    ]
    progress(f"  {len(heroes)} heroes loaded, {len(new_heroes)} new to insert...")
    if new_heroes:
        session.add_all(new_heroes)
        session.commit()
    progress("  Hero sync done.")


def fetch_pro_player_directory(client: OpenDotaClient) -> dict[int, dict]:
    """account_id -> {name, team_id} for known pro players, used to fill in
    a cleaner nickname than the personaname on an individual match."""
    directory: dict[int, dict] = {}
    for p in client.get_pro_players():
        account_id = p.get("account_id")
        if account_id is None:
            continue
        directory[account_id] = {
            "name": p.get("name") or p.get("personaname"),
            "team_id": p.get("team_id"),
        }
    return directory


def normalize_opendota_match(detail: dict) -> dict:
    players = []
    for p in detail.get("players", []):
        account_id = p.get("account_id")
        if account_id is None:
            continue
        is_radiant = p.get("isRadiant")
        if is_radiant is None:
            is_radiant = p.get("player_slot", 0) < 128
        players.append({
            "account_id": account_id,
            "hero_id": p.get("hero_id"),
            "is_radiant": is_radiant,
            "kills": p.get("kills"),
            "deaths": p.get("deaths"),
            "assists": p.get("assists"),
        })

    return {
        "match_id": detail["match_id"],
        "start_time": detail.get("start_time"),
        "duration": detail.get("duration"),
        "radiant_team_id": detail.get("radiant_team_id"),
        "dire_team_id": detail.get("dire_team_id"),
        "radiant_win": detail.get("radiant_win"),
        "players": players,
    }


def fetch_all_league_matches(
    league_id: int,
    od_client: OpenDotaClient,
    steam_client: SteamClient | None,
    progress: ProgressFn,
) -> list[dict]:
    """Combines Valve's own match history (via Steam Web API, when a key is
    configured) with OpenDota's league index, deduped by match_id. Steam is
    the authoritative source and is cheap (one call covers up to 100
    matches); OpenDota is used to fill in anything Steam's league_id tagging
    missed, at the cost of one detail request per match."""
    matches_by_id: dict[int, dict] = {}

    if steam_client is not None:
        try:
            for m in steam_client.iter_league_matches(league_id):
                matches_by_id[m["match_id"]] = m
            progress(f"Steam API: {len(matches_by_id)} matches for league_id={league_id}")
        except Exception as e:
            # Call out a dead key explicitly. Steam answers a revoked/expired
            # key with a bare 403 on EVERY endpoint, which reads exactly like
            # "Valve removed this API" - it is not, and mistaking one for the
            # other costs a lot: Steam's league history is the cheap way to
            # enumerate matches (one call per 100), and without it the mixer
            # seed has to pull every match from OpenDota one by one, which
            # this host's shared IP gets rate-limited for.
            if "403" in str(e):
                progress(
                    "Steam API returned 403 for league history. This almost always means "
                    "STEAM_API_KEY is invalid, revoked or expired - NOT that the endpoint is gone. "
                    "Issue a fresh key at https://steamcommunity.com/dev/apikey and set STEAM_API_KEY. "
                    "Falling back to the (much slower, rate-limited) per-match OpenDota path."
                )
            else:
                progress(f"Steam API fetch failed ({e}), continuing with OpenDota only")

    try:
        od_list = od_client.get_league_matches(league_id)
    except Exception as e:
        progress(f"OpenDota league-matches fetch failed: {e}")
        od_list = []

    missing_ids = [m["match_id"] for m in od_list if m["match_id"] not in matches_by_id]
    for match_id in missing_ids:
        detail = od_client.get_match(match_id)
        matches_by_id[match_id] = normalize_opendota_match(detail)

    return list(matches_by_id.values())


def resolve_team_names(
    od_client: OpenDotaClient,
    steam_client: SteamClient | None,
    team_ids: set[int],
    progress: ProgressFn,
    time_budget: int | None = None,
) -> dict[int, str | None]:
    """Small/amateur teams are often missing from OpenDota's team index, so
    try Valve's own GetTeamInfo (via Steam) first and fall back to OpenDota."""
    names: dict[int, str | None] = {}
    deadline = None if time_budget is None else time.monotonic() + time_budget
    for team_id in team_ids:
        if not team_id:
            continue
        if deadline is not None and time.monotonic() > deadline:
            progress("Team-name resolution: time budget spent; rest follows next run.")
            break

        name = None
        if steam_client is not None:
            try:
                info = steam_client.get_team_info(team_id)
                if info:
                    name = info.get("name")
            except Exception as e:
                progress(f"Steam GetTeamInfo failed for team_id={team_id}: {e}")

        if not name:
            try:
                info = od_client.get_team(team_id)
                name = info.get("name")
            except Exception:
                pass

        names[team_id] = name
    return names


def upsert_team(session: Session, team_id: int | None, name: str | None) -> None:
    if team_id is None:
        return
    team = session.get(Team, team_id)
    if team is None:
        session.add(Team(team_id=team_id, name=name))
    elif name and not team.name:
        team.name = name


def upsert_player(
    session: Session,
    account_id: int | None,
    fallback_name: str | None,
    team_id: int | None,
    pro_directory: dict[int, dict],
) -> None:
    if account_id is None:
        return
    pro_info = pro_directory.get(account_id, {})
    resolved_name = pro_info.get("name") or fallback_name
    # team_id always comes from the match actually being processed, never
    # from OpenDota's pro_directory: that field is the player's real-world
    # pro team, which is unrelated to (and can silently override) which
    # mixer-cup team_id they played under in this tournament.

    player = session.get(Player, account_id)
    if player is None:
        session.add(Player(account_id=account_id, name=resolved_name, team_id=team_id))
    else:
        if resolved_name:
            player.name = resolved_name
        if team_id:
            player.team_id = team_id


def persist_match(
    session: Session,
    league_id: int,
    match: dict,
    pro_directory: dict[int, dict],
    team_names: dict[int, str | None],
) -> None:
    # Explicit flush()es at each dependency boundary (teams -> match ->
    # players -> match_players) instead of relying on SQLAlchemy's
    # automatic flush-order dependency sort: that sort is reliable against
    # local SQLite, but against Turso/libSQL (which - unlike local SQLite's
    # default - actually enforces foreign keys) statements ended up sent in
    # an order that tripped FOREIGN KEY constraint failures. Flushing at
    # each stage guarantees the referenced rows exist first regardless of
    # dialect-specific autoflush/ordering quirks.
    match_id = match["match_id"]
    radiant_team_id = match.get("radiant_team_id")
    dire_team_id = match.get("dire_team_id")
    upsert_team(session, radiant_team_id, team_names.get(radiant_team_id))
    upsert_team(session, dire_team_id, team_names.get(dire_team_id))
    session.flush()

    session.add(Match(
        match_id=match_id,
        league_id=league_id,
        start_time=match.get("start_time"),
        duration=match.get("duration"),
        radiant_team_id=radiant_team_id,
        dire_team_id=dire_team_id,
        radiant_win=match.get("radiant_win"),
    ))
    session.flush()

    player_rows = []
    for p in match["players"]:
        account_id = p["account_id"]
        is_radiant = bool(p["is_radiant"])
        team_id = radiant_team_id if is_radiant else dire_team_id
        upsert_player(session, account_id, None, team_id, pro_directory)
        player_rows.append((p, is_radiant, team_id))
    session.flush()

    for p, is_radiant, team_id in player_rows:
        session.add(MatchPlayer(
            match_id=match_id,
            account_id=p["account_id"],
            hero_id=p["hero_id"],
            team_id=team_id,
            is_radiant=is_radiant,
            kills=p.get("kills"),
            deaths=p.get("deaths"),
            assists=p.get("assists"),
        ))


def persist_draft_entries(session: Session, match_id: int, detail: dict) -> bool:
    """Store a match's picks/bans out of an OpenDota/Steam match detail.
    False means the detail carried no draft (not captain's mode, or OpenDota
    hasn't parsed it) - the caller decides whether that's worth reporting."""
    picks_bans = detail.get("picks_bans") or []
    if not picks_bans:
        return False
    match = session.get(Match, match_id)
    if match is None:
        return False
    for pb in picks_bans:
        team_id = match.radiant_team_id if pb.get("team") == 0 else match.dire_team_id
        session.add(MatchDraftEntry(
            match_id=match_id,
            order_num=pb.get("order", 0),
            hero_id=pb.get("hero_id"),
            team_id=team_id,
            is_pick=bool(pb.get("is_pick")),
        ))
    return True


def sync_draft_data(
    session: Session,
    client: OpenDotaClient,
    steam_client: SteamClient | None,
    progress: ProgressFn,
    time_budget: int | None = None,
) -> None:
    """Backfills picks/bans for matches that don't have any draft rows yet.
    Steam's GetMatchDetails is the primary source (generous rate limits,
    same picks_bans structure - OpenDota sources its copy from there);
    OpenDota is the per-match fallback. Costs one call per match, but only
    once - matches already covered are skipped on later runs.

    Newly seeded matches already carry their draft (seed_matches_from_mixer
    stores it from the same detail), so this only covers matches that predate
    that - which, because every build is seeded from the live DB, would
    otherwise stay draftless forever and leave their teams' analysis empty."""
    has_draft = {row[0] for row in session.execute(select(MatchDraftEntry.match_id))}
    # Newest first: the running cup's games are what people have open, and the
    # per-run cap means the tail waits for a later cycle.
    all_match_ids = [
        row[0] for row in session.execute(
            select(Match.match_id).order_by(Match.start_time.desc().nulls_last())
        )
    ]
    missing = [m for m in all_match_ids if m not in has_draft]
    if not missing:
        return

    # Valve's GetMatchDetails 500s for private-lobby games (which is what
    # this mixer tournament is played in) even though GetMatchHistory lists
    # them fine. After a few straight failures, stop trying Steam for the
    # rest of the run instead of burning a doomed call + log line per match.
    steam_failures = 0

    def fetch_detail(match_id: int) -> dict:
        nonlocal steam_failures
        if steam_client is not None and steam_failures < 3:
            try:
                detail = steam_client.get_match_details(match_id)
                steam_failures = 0
                return detail
            except Exception as e:
                steam_failures += 1
                if steam_failures == 3:
                    progress(f"Steam GetMatchDetails keeps failing ({e}); using OpenDota only for this run.")
        return client.get_match(match_id)

    progress(f"Fetching picks/bans for {len(missing)} match(es)...")
    deadline = None if time_budget is None else time.monotonic() + time_budget
    fetched_empty = 0
    consecutive_failures = 0
    for i, match_id in enumerate(missing, start=1):
        if deadline is not None and time.monotonic() > deadline:
            progress(f"Draft backfill: time budget spent; {len(missing) - i + 1} match(es) left for the next run")
            break
        try:
            detail = fetch_detail(match_id)
            consecutive_failures = 0
        except Exception as e:
            progress(f"Could not fetch picks/bans for match {match_id}: {e}")
            consecutive_failures += 1
            if consecutive_failures >= 5:
                # Both sources failing repeatedly (e.g. OpenDota rate-limits
                # this IP for the day) - grinding through the rest would waste
                # minutes to fetch nothing. Stop; the next cycle backfills
                # what's left (already-fetched drafts are kept via the seeded
                # build).
                progress("Draft sources look rate-limited; deferring remaining drafts to a later run.")
                break
            continue

        if not persist_draft_entries(session, match_id, detail):
            fetched_empty += 1
            continue
        session.commit()
        if i % 10 == 0 or i == len(missing):
            progress(f"  picks/bans {i}/{len(missing)}")

    if fetched_empty:
        progress(f"{fetched_empty} match(es) had no draft data (not captain's mode, or not parsed by OpenDota)")


def enrich_missing_player_names(session: Session, client: OpenDotaClient, progress: ProgressFn,
                                time_budget: int | None = None) -> None:
    """Steam's GetMatchHistory doesn't include nicknames, only account_id.
    Fill those in from OpenDota's player profile endpoint (best-effort)."""
    missing = session.execute(select(Player).where(Player.name.is_(None))).scalars().all()
    if not missing:
        return
    progress(f"Resolving nicknames for {len(missing)} player(s) without a known name...")
    # Bounded like the other OpenDota phases: this is the last thing standing
    # between the draft backfill and the final publish, so letting it run long
    # risks the whole run being killed as wedged - taking this run's drafts,
    # which are only published at that final step, down with it.
    deadline = None if time_budget is None else time.monotonic() + time_budget
    consecutive_failures = 0
    for player in missing:
        if deadline is not None and time.monotonic() > deadline:
            progress("Nickname enrichment: time budget spent; rest follows next run.")
            break
        try:
            info = client.get_player(player.account_id)
            consecutive_failures = 0
            name = (info.get("profile") or {}).get("personaname")
            if name:
                player.name = name
        except Exception:
            consecutive_failures += 1
            if consecutive_failures >= 5:
                # Same rationale as sync_draft_data: a run of failures means
                # OpenDota is rate-limiting the IP - defer to a later cycle.
                progress("OpenDota looks rate-limited; deferring remaining nicknames to a later run.")
                break
            continue
    session.commit()


def enrich_missing_team_names(
    session: Session,
    od_client: OpenDotaClient,
    steam_client: SteamClient | None,
    progress: ProgressFn,
    time_budget: int | None = None,
) -> None:
    missing = session.execute(select(Team).where(Team.name.is_(None))).scalars().all()
    if not missing:
        return
    progress(f"Resolving names for {len(missing)} team(s) without a known name...")
    team_ids = {t.team_id for t in missing}
    names = resolve_team_names(od_client, steam_client, team_ids, progress,
                               time_budget=time_budget)
    for team in missing:
        name = names.get(team.team_id)
        if name:
            team.name = name
    session.commit()


def _lineup_account_ids(session: Session, match_id: int, is_radiant: bool) -> set[int]:
    rows = session.execute(
        select(MatchPlayer.account_id)
        .where(MatchPlayer.match_id == match_id, MatchPlayer.is_radiant == is_radiant)
    ).all()
    return {account_id for (account_id,) in rows}


def _apply_confirmed_roster(session: Session, steam_team_id: int, mixer_team: dict) -> None:
    """Resets roster_confirmed for everyone who has ever played under this
    Steam team_id (regular roster + one-off substitutes), then confirms and
    renames exactly the accounts that match MixerCup's current roster by
    Steam account_id (derived from steamAvatar - see
    mixercup_client.steam_account_id_from_avatar_url). This is an exact
    numeric match, unlike nickname text, which can differ between a
    player's live Steam persona name and their mixer-cup registration."""
    roster = session.execute(select(Player).where(Player.team_id == steam_team_id)).scalars().all()
    for player in roster:
        player.roster_confirmed = False

    by_account_id = {p.account_id: p for p in roster}
    for mp in mixer_team.get("players", []):
        account_id = mp.get("account_id")
        nickname = mp.get("nickname")
        if account_id is None:
            continue
        roles = ",".join(mp["preferredRoles"]) if mp.get("preferredRoles") else None
        player = by_account_id.get(account_id)
        if player is None:
            # A freshly substituted-in player has no Player row yet (rows
            # normally come from match data, and they haven't played for
            # this team yet). Create it from the mixer roster so the site
            # shows the substitution immediately, not after their first game.
            player = session.get(Player, account_id)
            if player is not None:
                # Exists under another team (played elsewhere earlier in the
                # tournament) - move them to their current team.
                player.team_id = steam_team_id
            else:
                player = Player(account_id=account_id, team_id=steam_team_id)
                session.add(player)
        player.roster_confirmed = True
        if nickname:
            player.name = nickname
        if mp.get("rating") is not None:
            player.mmr = mp["rating"]
        if roles:
            player.preferred_roles = roles


def _team_mixer_uuid(session: Session, team_id: int | None) -> str | None:
    """The mixer_uuid stored on a steam team row, used to orient a match's
    sides when lineup overlap is inconclusive."""
    if team_id is None:
        return None
    team_row = session.get(Team, team_id)
    return team_row.mixer_uuid if team_row is not None else None


def link_mixercup_data(
    session: Session,
    mixer_client: MixerCupClient,
    tournament_id: int,
    progress: ProgressFn,
    apply_rosters: bool = True,
) -> None:
    """Uses mixer-cup.gg's own GraphQL API to attach real team names and
    current roster nicknames to the matches we already collected from
    Steam/OpenDota. MixerCup's `Games.matchId` field is the Dota match_id,
    so completed games there give a direct match_id -> (team1, team2) link;
    which mixer team is which side (radiant/dire) is then determined by
    comparing Steam account_ids against who actually played in that match.

    apply_rosters=False is used for PAST tournaments: it still sets each
    match's result (radiant_win) and team names/uuids - so old match pages
    show W/L - but does NOT touch confirmed rosters, player.team_id or team
    merging. Those must be driven only by the ACTIVE tournament, otherwise a
    player who competed in both tournaments would have their team flip-flop
    every cycle."""
    try:
        teams = list(mixer_client.iter_teams(tournament_id))
        games = list(mixer_client.iter_completed_games(tournament_id))
    except Exception as e:
        progress(f"MixerCup API fetch failed, skipping team/roster linking: {e}")
        return

    if not teams or not games:
        progress("MixerCup: no teams/games returned for this tournament_id, skipping linking")
        return
    progress(f"MixerCup: {len(teams)} teams, {len(games)} completed games with a linked match_id")

    teams_by_id = {t["id"]: t for t in teams}
    linked = ambiguous = skipped = 0

    for g in games:
        match_id_raw = g.get("matchId")
        if not match_id_raw:
            continue
        try:
            match_id = int(match_id_raw)
        except (TypeError, ValueError):
            continue

        match = session.get(Match, match_id)
        if match is None:
            continue

        team1 = teams_by_id.get((g.get("team1") or {}).get("id"))
        team2 = teams_by_id.get((g.get("team2") or {}).get("id"))
        if not team1 or not team2:
            skipped += 1
            continue

        radiant_ids = _lineup_account_ids(session, match_id, is_radiant=True)
        dire_ids = _lineup_account_ids(session, match_id, is_radiant=False)
        team1_ids = {p["account_id"] for p in team1["players"] if p.get("account_id") is not None}
        team2_ids = {p["account_id"] for p in team2["players"] if p.get("account_id") is not None}

        score_a = len(team1_ids & radiant_ids) + len(team2_ids & dire_ids)
        score_b = len(team1_ids & dire_ids) + len(team2_ids & radiant_ids)
        if score_a != score_b:
            radiant_team, dire_team = (team1, team2) if score_a > score_b else (team2, team1)
        else:
            # Lineup overlap couldn't orient the sides - rosters without
            # account_ids, or an old match whose players we no longer refetch.
            # Fall back to the mixer_uuid already stored on each steam team, so
            # old games still get their result even with no fresh lineup.
            radiant_uuid = _team_mixer_uuid(session, match.radiant_team_id)
            dire_uuid = _team_mixer_uuid(session, match.dire_team_id)
            t1_id, t2_id = team1["id"], team2["id"]
            if radiant_uuid == t1_id or dire_uuid == t2_id:
                radiant_team, dire_team = team1, team2
            elif radiant_uuid == t2_id or dire_uuid == t1_id:
                radiant_team, dire_team = team2, team1
            else:
                ambiguous += 1
                continue

        # Stamp the match with the tournament it actually belongs to (the
        # one whose completed-games list it appears in), regardless of its
        # shared dotabuff league_id.
        match.mixer_tournament_id = tournament_id

        result = g.get("result")
        if result in ("WIN1", "WIN2"):
            team1_won = result == "WIN1"
            match.radiant_win = team1_won if radiant_team is team1 else not team1_won

        for steam_team_id, mixer_team in ((match.radiant_team_id, radiant_team), (match.dire_team_id, dire_team)):
            if steam_team_id is None:
                continue
            team_row = session.get(Team, steam_team_id)
            if team_row is not None:
                # Name/uuid: set when THIS tournament owns the team. Active
                # linking always owns it; past linking only when the team
                # isn't already claimed by a different (e.g. still-active)
                # tournament - otherwise a steam_team_id reused across the two
                # concurrent cups would get renamed to the other cup's name
                # (how "Team B3SHA" became "Team yuusha").
                owns = apply_rosters or team_row.tournament_id in (None, tournament_id)
                if owns:
                    if mixer_team.get("name"):
                        team_row.name = mixer_team["name"]
                    if mixer_team.get("id"):
                        team_row.mixer_uuid = mixer_team["id"]
                # Active reclaims the team; past claims only if unclaimed, so
                # a team known only from an old cup still gets a tournament.
                if apply_rosters:
                    team_row.tournament_id = tournament_id
                elif team_row.tournament_id is None:
                    team_row.tournament_id = tournament_id
            # Rosters (which player is on the team now) are driven ONLY by the
            # active tournament - a player competing in both cups must not have
            # their team flip-flop each cycle.
            if apply_rosters:
                _apply_confirmed_roster(session, steam_team_id, mixer_team)
        linked += 1

    if apply_rosters:
        _merge_duplicate_steam_teams(session, teams_by_id, progress)

    session.commit()
    progress(f"MixerCup linking (tournament {tournament_id}): {linked} matches linked, {ambiguous} ambiguous, {skipped} skipped")


# Synthetic Team ids for mixer teams that have no Steam team_id yet (no
# match played, or the league id isn't known yet). Far above real Steam team
# ids (~8 digits); once real matches link a Steam id to the same mixer_uuid,
# _merge_duplicate_steam_teams folds the synthetic row into the real one.
_SYNTHETIC_TEAM_ID_BASE = 2_000_000_000


def sync_mixer_teams(
    session: Session,
    mixer_client: MixerCupClient,
    tournament_id: int,
    progress: ProgressFn,
) -> None:
    """Makes sure every team of the ACTIVE mixer tournament exists in the
    DB with its current name, roster and tournament marker - even before a
    single match is played (a fresh tournament has no league_id and no
    matches yet, but its teams and rosters are already public). Teams
    without a known Steam id get a synthetic one, replaced automatically by
    the real id once matches start linking."""
    try:
        mixer_teams = list(mixer_client.iter_teams(tournament_id))
    except Exception as e:
        progress(f"MixerCup team sync failed: {e}")
        return

    max_synth = session.execute(
        select(func.max(Team.team_id)).where(Team.team_id >= _SYNTHETIC_TEAM_ID_BASE)
    ).scalar() or _SYNTHETIC_TEAM_ID_BASE

    created = updated = 0
    for mt in mixer_teams:
        if not mt.get("id"):
            continue
        team_row = session.execute(
            select(Team).where(Team.mixer_uuid == mt["id"])
        ).scalars().first()
        if team_row is None:
            max_synth += 1
            team_row = Team(
                team_id=max_synth,
                name=mt.get("name"),
                mixer_uuid=mt["id"],
                tournament_id=tournament_id,
            )
            session.add(team_row)
            session.flush()
            created += 1
        else:
            if mt.get("name"):
                team_row.name = mt["name"]
            team_row.tournament_id = tournament_id
            updated += 1
        _apply_confirmed_roster(session, team_row.team_id, mt)

    session.commit()
    progress(f"MixerCup team sync: {created} new team(s), {updated} updated for tournament {tournament_id}")


def _merge_duplicate_steam_teams(session: Session, teams_by_id: dict, progress: ProgressFn) -> None:
    """A captain re-creating their in-game team mid-tournament gives the
    same mixer-cup team a second Steam team_id: older matches sit under the
    old id, newer ones under the new. Left as-is, the site shows the team
    twice, and the older copy loses its confirmed roster (its players'
    team_id now points at the new id), falling back to 'everyone who ever
    played a match under it' - e.g. 10 players and an inflated MMR total.

    Merge each such group under the id the team most recently played with:
    re-point matches, match players, draft entries, player rows and
    substitution events, drop the leftover Team rows, and re-apply the
    confirmed roster under the canonical id."""
    dup_uuids = [
        u for (u,) in session.execute(
            select(Team.mixer_uuid)
            .where(Team.mixer_uuid.is_not(None))
            .group_by(Team.mixer_uuid)
            .having(func.count() > 1)
        )
    ]
    for mixer_uuid in dup_uuids:
        team_ids = [
            t for (t,) in session.execute(
                select(Team.team_id).where(Team.mixer_uuid == mixer_uuid)
            )
        ]
        canonical, newest = None, -1
        for tid in team_ids:
            last = session.execute(
                select(func.max(Match.start_time)).where(
                    (Match.radiant_team_id == tid) | (Match.dire_team_id == tid)
                )
            ).scalar()
            if last is not None and last > newest:
                canonical, newest = tid, last
        if canonical is None:
            continue
        others = [t for t in team_ids if t != canonical]

        for old_id in others:
            session.execute(update(Match).where(Match.radiant_team_id == old_id)
                            .values(radiant_team_id=canonical))
            session.execute(update(Match).where(Match.dire_team_id == old_id)
                            .values(dire_team_id=canonical))
            session.execute(update(MatchPlayer).where(MatchPlayer.team_id == old_id)
                            .values(team_id=canonical))
            session.execute(update(MatchDraftEntry).where(MatchDraftEntry.team_id == old_id)
                            .values(team_id=canonical))
            session.execute(update(Player).where(Player.team_id == old_id)
                            .values(team_id=canonical))
            session.execute(update(SubstitutionEvent).where(SubstitutionEvent.team_id == old_id)
                            .values(team_id=canonical))
            old_row = session.get(Team, old_id)
            if old_row is not None:
                session.delete(old_row)
        session.flush()

        mixer_team = teams_by_id.get(mixer_uuid)
        if mixer_team is not None:
            _apply_confirmed_roster(session, canonical, mixer_team)

        progress(f"Merged Steam team id(s) {others} into {canonical} (same mixer team)")


# Raw view of the backup the GitHub Action commits to the data-backup
# branch (see .github/workflows/keep-alive.yml). Public repo, so no auth.
_DEFAULT_BACKUP_URL = (
    "https://raw.githubusercontent.com/nolovedeepweb111/pari-mixer-scraper/data-backup/backup.json"
)


def restore_state_backup(session: Session, progress: ProgressFn) -> None:
    """Re-imports data that only ever existed in our own database after
    Render's ephemeral disk wipes it on redeploy: substitution events
    (mixer-cup deletes its own history periodically, and each event's
    queue_position was captured from our snapshot at sync time - neither is
    re-fetchable) and the substitute-queue snapshot itself. Runs before
    sync_substitution_history so that events re-fetched from mixer-cup can
    look up queue positions in the restored snapshot. Purely additive:
    existing rows are never overwritten, except filling a NULL
    queue_position from the backup."""
    import requests

    from .http_utils import call_with_timeout

    url = os.environ.get("BACKUP_RESTORE_URL", _DEFAULT_BACKUP_URL)
    if not url:
        return
    try:
        resp = call_with_timeout(lambda: requests.get(url, timeout=20), timeout=25)
        if resp.status_code == 404:
            return  # no backup committed yet
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        progress(f"State backup fetch failed ({e}); continuing without restore.")
        return

    added_events = filled_positions = added_queue = 0
    added_teams = added_players = 0

    # Teams first (players and substitution events reference them). Insert
    # missing rows; for existing ones only fill NULLs - live/freshly-linked
    # data always wins over the backup.
    existing_teams = {t.team_id: t for t in session.execute(select(Team)).scalars()}
    for bt in data.get("teams", []):
        if bt.get("team_id") is None:
            continue
        row = existing_teams.get(bt["team_id"])
        if row is None:
            session.add(Team(
                team_id=bt["team_id"],
                name=bt.get("name"),
                mixer_uuid=bt.get("mixer_uuid"),
                tournament_id=bt.get("tournament_id"),
            ))
            added_teams += 1
        else:
            if row.name is None and bt.get("name"):
                row.name = bt["name"]
            if row.mixer_uuid is None and bt.get("mixer_uuid"):
                row.mixer_uuid = bt["mixer_uuid"]
            if row.tournament_id is None and bt.get("tournament_id") is not None:
                row.tournament_id = bt["tournament_id"]
    session.flush()

    existing_players = {p.account_id: p for p in session.execute(select(Player)).scalars()}
    for bp in data.get("players", []):
        if bp.get("account_id") is None:
            continue
        row = existing_players.get(bp["account_id"])
        if row is None:
            session.add(Player(
                account_id=bp["account_id"],
                name=bp.get("name"),
                team_id=bp.get("team_id"),
                roster_confirmed=bool(bp.get("roster_confirmed")),
                mmr=bp.get("mmr"),
                preferred_roles=bp.get("preferred_roles"),
            ))
            added_players += 1
        else:
            if row.name is None and bp.get("name"):
                row.name = bp["name"]
            if row.mmr is None and bp.get("mmr") is not None:
                row.mmr = bp["mmr"]
            if row.preferred_roles is None and bp.get("preferred_roles"):
                row.preferred_roles = bp["preferred_roles"]
    session.flush()

    existing_events = {e.event_id: e for e in session.execute(select(SubstitutionEvent)).scalars()}
    for ev in data.get("substitution_events", []):
        if not ev.get("event_id") or ev.get("team_id") is None:
            continue
        row = existing_events.get(ev["event_id"])
        if row is None:
            session.add(SubstitutionEvent(
                event_id=ev["event_id"],
                team_id=ev["team_id"],
                event_type=ev.get("event_type") or "",
                nickname=ev.get("nickname"),
                rating=ev.get("rating"),
                queue_position=ev.get("queue_position"),
                occurred_at=ev.get("occurred_at") or "",
            ))
            added_events += 1
        elif row.queue_position is None and ev.get("queue_position") is not None:
            row.queue_position = ev["queue_position"]
            filled_positions += 1

    existing_queue = {row[0] for row in session.execute(select(QueuedPlayer.player_uuid))}
    for qp in data.get("queued_players", []):
        if not qp.get("player_uuid") or qp["player_uuid"] in existing_queue:
            continue
        session.add(QueuedPlayer(
            player_uuid=qp["player_uuid"],
            nickname=qp.get("nickname"),
            rating=qp.get("rating"),
            queue_position=qp.get("queue_position"),
            updated_at=qp.get("updated_at") or "",
        ))
        added_queue += 1

    if added_events or filled_positions or added_queue or added_teams or added_players:
        session.commit()
        progress(
            f"State backup restored: +{added_teams} team(s), +{added_players} player(s), "
            f"+{added_events} substitution event(s), {filled_positions} queue position(s) "
            f"filled, +{added_queue} queued player(s)"
        )


def sync_queue_snapshot(
    session: Session,
    mixer_client: MixerCupClient,
    tournament_id: int,
    progress: ProgressFn,
) -> None:
    """Refreshes our copy of the substitute queue (who's waiting, at what
    position). Rows are upserted, never deleted: a player vanishes from
    MixerCup's queue the instant they're picked into a team, so the last
    known position we recorded is precisely what sync_substitution_history
    needs to say where the incoming player stood in line."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    seen = 0
    try:
        for p in mixer_client.iter_queue_participants(tournament_id):
            if not p["player_uuid"]:
                continue
            row = session.get(QueuedPlayer, p["player_uuid"])
            if row is None:
                session.add(QueuedPlayer(
                    player_uuid=p["player_uuid"],
                    nickname=p["nickname"],
                    rating=p["rating"],
                    queue_position=p["queue_position"],
                    updated_at=now,
                ))
            else:
                row.nickname = p["nickname"] or row.nickname
                row.rating = p["rating"] if p["rating"] is not None else row.rating
                row.queue_position = p["queue_position"]
                row.updated_at = now
            seen += 1
    except Exception as e:
        progress(f"Queue snapshot fetch failed: {e}")
        return

    session.commit()
    progress(f"Queue snapshot: {seen} player(s) currently in line")


def sync_substitution_history(
    session: Session,
    mixer_client: MixerCupClient,
    tournament_id: int,
    progress: ProgressFn,
) -> None:
    """Saves every PLAYER_IN/PLAYER_OFF event for each team we've linked to
    mixer-cup.gg, permanently - their own substitution history has been
    observed to disappear periodically, so this is the durable copy.
    event_id dedupes cleanly against already-synced events, so this is
    cheap to re-run every collection pass."""
    known_event_ids = {row[0] for row in session.execute(select(SubstitutionEvent.event_id))}
    # Only the active tournament's teams - events are queried per (tournament,
    # team) pair, so old-tournament teams would just burn an empty API call
    # each. Their history is already stored (and in the state backup).
    teams = session.execute(
        select(Team).where(Team.mixer_uuid.is_not(None), Team.tournament_id == tournament_id)
    ).scalars().all()
    if not teams:
        return

    new_count = 0
    for team in teams:
        try:
            events = mixer_client.iter_substitution_events(tournament_id, team.mixer_uuid)
            for e in events:
                if e["event_id"] in known_event_ids:
                    continue
                # Where was the incoming player standing in the queue? Only
                # our own snapshot knows - MixerCup already dropped them
                # from the live queue the moment they were picked.
                queue_position = None
                if e["type"] == "PLAYER_IN" and e["player_uuid"]:
                    queued = session.get(QueuedPlayer, e["player_uuid"])
                    if queued is not None:
                        queue_position = queued.queue_position
                session.add(SubstitutionEvent(
                    event_id=e["event_id"],
                    team_id=team.team_id,
                    event_type=e["type"],
                    nickname=e["nickname"],
                    rating=e["rating"],
                    queue_position=queue_position,
                    occurred_at=e["occurred_at"],
                ))
                known_event_ids.add(e["event_id"])
                new_count += 1
        except Exception as e:
            progress(f"Substitution history fetch failed for team_id={team.team_id}: {e}")

    if new_count:
        session.commit()
        progress(f"Substitution history: {new_count} new event(s) saved")


def _purge_past_tournament_subs(session: Session, active_tournament_id: int, progress: ProgressFn) -> None:
    """Deletes substitution events tied to teams from any tournament other
    than the active one - the site only shows the current tournament's
    substitution history."""
    past_team_ids = [
        t for (t,) in session.execute(
            select(Team.team_id).where(
                Team.tournament_id.is_not(None),
                Team.tournament_id != active_tournament_id,
            )
        )
    ]
    if not past_team_ids:
        return
    deleted = session.execute(
        delete(SubstitutionEvent).where(SubstitutionEvent.team_id.in_(past_team_ids))
    ).rowcount
    if deleted:
        session.commit()
        progress(f"Removed {deleted} substitution event(s) from past tournaments")


def apply_manual_roster_overrides(session: Session, progress: ProgressFn) -> None:
    applied = 0
    for account_id, override in MANUAL_ROSTER_OVERRIDES.items():
        player = session.get(Player, account_id)
        if player is None:
            continue
        player.team_id = override["team_id"]
        player.roster_confirmed = True
        if override.get("mmr") is not None:
            player.mmr = override["mmr"]
        applied += 1
    if applied:
        session.commit()
        progress(f"Applied {applied} manual roster override(s)")


def _resolve_all_tournament_ids(session: Session, active_id: int | None) -> list[int]:
    """Every mixer tournament we should touch: the active one, any forced via
    env MIXER_TOURNAMENT_IDS, plus any already recorded on teams/matches - so
    a past cup keeps getting refreshed even after a redeploy wipes the disk."""
    ids: set[int] = set()
    if active_id is not None:
        ids.add(active_id)
    for raw in os.environ.get("MIXER_TOURNAMENT_IDS", "").replace(";", ",").split(","):
        try:
            ids.add(int(raw.strip()))
        except ValueError:
            pass
    ids.update(
        t for (t,) in session.execute(
            select(Team.tournament_id).where(Team.tournament_id.is_not(None)).distinct()
        )
    )
    ids.update(
        t for (t,) in session.execute(
            select(Match.mixer_tournament_id).where(Match.mixer_tournament_id.is_not(None)).distinct()
        )
    )
    return sorted(ids)


# Budget the two expensive, OpenDota-bound phases by WALL CLOCK, not by a
# match count. The count has to be guessed against an unknown per-call latency
# (~1.2s at best, far worse when the shared IP is being throttled), so it is
# either too small - a 60-match cap left ~300 matches needing five runs, and
# runs are rare: the free instance sleeps after ~15 min idle and the GitHub
# keep-alive is throttled to once every few HOURS, so the backfill dragged on
# for days - or too big, running past the caller's "this run is wedged"
# threshold, which used to stack a second collector on the box and take the
# site down with it. A deadline gets both right: grab as much as fits, promote
# what we got, continue next run (builds are seeded from the live DB, so
# progress accumulates).
SEED_TIME_BUDGET_SECONDS = int(os.environ.get("SEED_TIME_BUDGET_SECONDS", "300"))
DRAFT_TIME_BUDGET_SECONDS = int(os.environ.get("DRAFT_TIME_BUDGET_SECONDS", "180"))
# The cosmetic name lookups run after the draft backfill but before the final
# publish, so they get a small budget too - the whole run must stay under the
# caller's 15-min wedged threshold or this run's drafts never get published.
ENRICH_TIME_BUDGET_SECONDS = int(os.environ.get("ENRICH_TIME_BUDGET_SECONDS", "60"))


def seed_matches_from_mixer(
    session: Session,
    mixer_client: MixerCupClient,
    od_client: OpenDotaClient,
    league_id: int,
    tournament_ids: list[int],
    progress: ProgressFn,
    time_budget: int | None = None,
) -> int:
    """Backstop match discovery: take the match ids from mixer-cup's
    completed-games list and pull each one's details from OpenDota.

    This covers whatever Steam's league history didn't (OpenDota's own league
    index can't help - this league is tier 'excluded', so it returns nothing).
    It only fetches ids we don't already have, so with a WORKING STEAM_API_KEY
    it costs nothing: Steam has already supplied the list. With a dead key it
    becomes the only path, at one OpenDota call per match - slow, and prone to
    rate-limiting on a shared egress IP. If this is doing all the work, fix
    the Steam key rather than tuning the budgets."""
    wanted: set[int] = set()
    for tid in tournament_ids:
        try:
            for g in mixer_client.iter_completed_games(tid):
                raw = g.get("matchId")
                if not raw:
                    continue
                try:
                    match_id = int(raw)
                except (TypeError, ValueError):
                    continue
                # Real Dota match ids are ~10 digits (billions); mixer-cup
                # occasionally carries a small internal id for a game whose
                # Dota match was never linked - skip those, OpenDota 404s them.
                if match_id >= 1_000_000_000:
                    wanted.add(match_id)
        except Exception as e:
            progress(f"MixerCup completed-games fetch failed for tournament {tid}: {e}")

    existing = {row[0] for row in session.execute(select(Match.match_id))}
    # Newest first: if the cap defers some, the ones people are actually
    # looking at (this cup's latest games) land first.
    missing = sorted(wanted - existing, reverse=True)
    if not missing:
        progress(f"MixerCup match seed: all {len(wanted)} known matches already stored")
        return 0

    total_missing = len(missing)
    progress(f"MixerCup match seed: {total_missing} new match(es) to fetch from OpenDota")

    deadline = None if time_budget is None else time.monotonic() + time_budget
    added = 0
    consecutive_errors = 0
    for i, match_id in enumerate(missing, start=1):
        if deadline is not None and time.monotonic() > deadline:
            progress(f"MixerCup match seed: time budget spent; {total_missing - i + 1} match(es) left for the next run")
            break
        try:
            detail = od_client.get_match(match_id)
            consecutive_errors = 0
        except Exception as e:
            # A single 404 (unparsed/unknown match) is fine - skip it. But a
            # run of failures means OpenDota is rate-limiting this shared IP
            # (lasts hours), so stop and let a later cycle backfill the rest
            # rather than burn the whole run hammering a closed door.
            consecutive_errors += 1
            if consecutive_errors >= 8:
                progress(f"OpenDota failing repeatedly (last: {match_id}, {e}); deferring {len(missing) - i + 1} match(es) to a later run.")
                break
            continue
        if not detail or detail.get("match_id") is None:
            continue
        persist_match(session, league_id, normalize_opendota_match(detail), {}, {})
        # The detail we just paid for already carries picks_bans - store the
        # draft now rather than let sync_draft_data re-fetch the very same
        # match later. That halves the OpenDota calls per match, which is the
        # binding constraint on Render's shared (rate-limited) egress IP.
        persist_draft_entries(session, match_id, detail)
        session.commit()
        added += 1
        if i % 25 == 0:
            progress(f"MixerCup match seed: {added} fetched so far ({i}/{len(missing)})...")
    progress(f"MixerCup match seed: {added} match(es) added")
    return added


def _run_collection_pass(engine, league_ids: list[int], progress: ProgressFn,
                         on_core_ready=None) -> int:
    """Does the actual collection work against `engine`, returning the
    number of newly stored matches. The caller decides what `engine`
    points at - see collect().

    league_ids is a list so a database can span tournaments: keeping earlier
    tournaments' league ids in the list means their matches stay
    re-fetchable from Steam after Render wipes the disk on a redeploy.

    on_core_ready, if given, is called once the core team/roster/match data
    is committed but before the slow supplementary backfill (draft history,
    nickname/team-name enrichment). The app uses it to publish teams to the
    site fast, so the user isn't staring at an empty page during the slow
    part."""
    progress("Engine ready, building API clients...")

    od_client = OpenDotaClient()
    steam_api_key = os.environ.get("STEAM_API_KEY")
    steam_client = SteamClient(steam_api_key) if steam_api_key else None
    if steam_client is None:
        progress("STEAM_API_KEY not set - fetching via OpenDota only (see README for how to add a Steam Web API key).")

    progress("Opening a session...")
    with Session(engine) as session:
        progress("Session open. Restoring state backup if available...")
        restore_state_backup(session, progress)

        progress("Syncing hero list...")
        sync_heroes(session, od_client, progress)

        new_count = 0
        existing_match_ids = {row[0] for row in session.execute(select(Match.match_id))}
        for league_id in league_ids:
            progress(f"Fetching match list for league_id={league_id}...")
            matches = fetch_all_league_matches(league_id, od_client, steam_client, progress)
            new_matches = [m for m in matches if m["match_id"] not in existing_match_ids]
            progress(f"league {league_id}: {len(matches)} matches total, {len(new_matches)} new")

            # Saving matches with no team name yet (rather than looking them
            # up via Steam/OpenDota, which is almost always a dead-end 404
            # for these ad-hoc mixer teams) gets a usable site up fast: real
            # names, rosters and MMR all come from the MixerCup link-up right
            # below, which is one cheap GraphQL call.
            for i, m in enumerate(new_matches, start=1):
                progress(f"[{i}/{len(new_matches)}] Saving match {m['match_id']}")
                persist_match(session, league_id, m, {}, {})
                session.commit()
                existing_match_ids.add(m["match_id"])
                new_count += 1

        # Deliberately NO early return when there are no matches: a freshly
        # started tournament has teams and rosters on mixer-cup before a
        # single game is played (and before its league_id is even known) -
        # the site should show them right away.
        mixer_client = MixerCupClient()
        mixer_tournament_id = os.environ.get("MIXER_TOURNAMENT_ID")
        try:
            if mixer_tournament_id:
                mixer_tournament_id = int(mixer_tournament_id)
            else:
                active = mixer_client.get_active_tournament()
                mixer_tournament_id = active["id"] if active else None
                if active:
                    progress(f"MixerCup active tournament: {active['name']} (id={active['id']})")
        except Exception as e:
            progress(f"Could not resolve MixerCup tournament id, skipping team/roster linking: {e}")
            mixer_tournament_id = None

        if mixer_tournament_id is not None:
            # #1 and #2 run concurrently and share a dotabuff league, so both
            # must be touched. Gather the full id set (active + env override +
            # whatever teams/matches already carry) once, and use it both to
            # seed matches and to link every tournament for results.
            all_tournament_ids = _resolve_all_tournament_ids(session, mixer_tournament_id)

            # Discover + fetch matches from mixer-cup (Steam/OpenDota can no
            # longer enumerate this league - see seed_matches_from_mixer). Must
            # run BEFORE linking, which only updates matches that already exist.
            seed_matches_from_mixer(
                session, mixer_client, od_client,
                league_ids[0] if league_ids else DEFAULT_LEAGUE_ID,
                all_tournament_ids, progress,
                time_budget=SEED_TIME_BUDGET_SECONDS,
            )

            # Link the ACTIVE tournament fully (rosters, team identity), then
            # every OTHER tournament for results only.
            link_mixercup_data(session, mixer_client, mixer_tournament_id, progress)
            sync_mixer_teams(session, mixer_client, mixer_tournament_id, progress)
            sync_substitution_history(session, mixer_client, mixer_tournament_id, progress)
            sync_queue_snapshot(session, mixer_client, mixer_tournament_id, progress)

            for pid in all_tournament_ids:
                if pid == mixer_tournament_id:
                    continue
                link_mixercup_data(session, mixer_client, pid, progress, apply_rosters=False)

            # The user only wants the ACTIVE tournament's substitution history.
            _purge_past_tournament_subs(session, mixer_tournament_id, progress)

        apply_manual_roster_overrides(session, progress)
        session.commit()
        progress("Core team/roster data ready - filling in the slower supplementary details now.")

        if on_core_ready is not None:
            # Core data is committed, so the DB file is a consistent snapshot
            # the caller can safely publish now (before the slow backfill).
            on_core_ready()

        # Everything from here on is supplementary (nicknames for players
        # MixerCup didn't cover, fallback team names, draft history for the
        # "Последние драфты" tab) - the site already looks right without it.
        # Each step is isolated in its own try/except: they all lean on
        # OpenDota, which rate-limits Render's shared IP hard (429), and an
        # unwrapped failure here used to kill the whole run BEFORE the final
        # publish - core data collected, then thrown away. Whatever fails
        # today just gets backfilled by a later run (builds are seeded from
        # the live DB, so completed work is never redone).
        try:
            progress("Syncing pro player directory...")
            pro_directory = fetch_pro_player_directory(od_client)
            for player in session.execute(select(Player).where(Player.name.is_(None))).scalars():
                pro_info = pro_directory.get(player.account_id)
                if pro_info and pro_info.get("name"):
                    player.name = pro_info["name"]
            session.commit()
        except Exception as e:
            session.rollback()
            progress(f"Pro player directory failed ({e}); skipping this step.")

        # Drafts first: they drive the team analysis cards (first pick, bans),
        # whereas the enrichment steps only polish names. All three share the
        # same rate-limited OpenDota budget, and when it runs out the LAST
        # steps are the ones that get skipped - so the draft backfill must not
        # sit behind the cosmetic ones.
        for step_name, step in (
            ("draft sync", lambda: sync_draft_data(session, od_client, steam_client, progress,
                                                   time_budget=DRAFT_TIME_BUDGET_SECONDS)),
            ("player name enrichment", lambda: enrich_missing_player_names(
                session, od_client, progress, time_budget=ENRICH_TIME_BUDGET_SECONDS)),
            ("team name enrichment", lambda: enrich_missing_team_names(
                session, od_client, steam_client, progress, time_budget=ENRICH_TIME_BUDGET_SECONDS)),
        ):
            try:
                step()
            except Exception as e:
                session.rollback()
                progress(f"{step_name} failed ({e}); skipping this step.")

    progress("Done.")
    return new_count


def _atomic_publish(src: str, dst: str) -> None:
    """Copy src onto dst atomically: copy to a temp beside dst, then
    os.replace it in. A plain copyfile truncates dst in place, which a
    concurrent reader (the web app) could catch mid-write; os.replace swaps
    the inode so readers always see either the old or the new file whole."""
    tmp = f"{dst}.publishtmp.{os.getpid()}"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def collect(
    league_id: int, db_path: str, progress: ProgressFn | None = None,
    engine=None, promote_to: str | None = None,
) -> int:
    """Runs a full collection pass against db_path, building in place, and
    returns the number of newly stored matches.

    This always writes directly to db_path with its own engine. The Flask
    app does NOT call this in a background thread - writing SQLite from a
    background thread of the gunicorn worker hung indefinitely on Render,
    while this exact code run as a standalone process works fine. So the app
    spawns it as a separate process pointed at a build file (see
    app._run_collect). `engine` is accepted for backward compatibility and
    ignored.

    When promote_to is set, the build is SEEDED from the current live DB at
    promote_to first, so every run is incremental: drafts and substitution
    history already collected are carried over, not re-fetched (critical -
    substitution events deleted upstream by mixer-cup.gg exist ONLY in our
    live DB, and a from-scratch rebuild used to silently drop them; it also
    re-downloaded every match's draft each run, burning OpenDota's monthly
    quota and leaving the site draftless for minutes at a time).

    The result is then published to promote_to in TWO stages: the seeded
    build with fresh core team/roster/match data as soon as that's ready
    (teams visible within seconds, old drafts intact), then the full DB once
    the slow supplementary backfill (new drafts, name enrichment) finishes.
    Both publishes are atomic and guarded so a run that ended up with no
    matches never clobbers the live site with an empty DB."""
    progress = progress or log.info
    progress("Starting collection...")

    # league_id accepts a single id, a comma-separated string, or a list -
    # keeping earlier tournaments' ids in the list keeps their matches
    # re-fetchable from Steam after a disk wipe.
    if isinstance(league_id, str):
        league_ids = [int(x) for x in league_id.replace(";", ",").split(",") if x.strip()]
    elif isinstance(league_id, (list, tuple)):
        league_ids = [int(x) for x in league_id]
    else:
        league_ids = [int(league_id)]

    if promote_to and os.path.exists(promote_to):
        # Seed from the live DB. It only ever has readers (the web app) -
        # every writer works on a build file like this one - so with no
        # active writer this is a consistent snapshot.
        progress("Seeding build from the live database...")
        shutil.copyfile(promote_to, db_path)

    progress("Building a new database engine...")
    own_engine = configure_sqlite(build_engine(db_path))
    Base.metadata.create_all(own_engine)

    def publish_core() -> None:
        if promote_to:
            _atomic_publish(db_path, promote_to)
            progress("Core data published to the live site (teams visible now).")

    try:
        new_count = _run_collection_pass(own_engine, league_ids, progress, on_core_ready=publish_core)
        if promote_to:
            with Session(own_engine) as s:
                total_matches = s.execute(
                    select(func.count()).select_from(Match)
                ).scalar() or 0
                total_teams = s.execute(
                    select(func.count()).select_from(Team)
                ).scalar() or 0
    finally:
        own_engine.dispose()

    if promote_to:
        # Guard on the build's TOTAL contents, not just newly added matches:
        # a routine incremental run with nothing new still carries fresh
        # results/rosters/subs worth publishing, and a brand-new tournament
        # legitimately has teams before any match exists. Only a genuinely
        # empty build (fresh deploy + total upstream outage) must never
        # replace a live DB that has data.
        if total_matches > 0 or total_teams > 0:
            os.replace(db_path, promote_to)
            progress(f"Full data published ({total_matches} matches, {new_count} new).")
        else:
            try:
                os.remove(db_path)
            except OSError:
                pass
            progress("Build is empty; left the live DB unchanged.")
    return new_count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect player/hero data for a Dota 2 esports league into SQLite."
    )
    parser.add_argument(
        "--league-id", type=str, default=str(DEFAULT_LEAGUE_ID),
        help=f"League id(s), comma-separated for multiple tournaments "
             f"(default: {DEFAULT_LEAGUE_ID}, Pari Mixer Cup #1)",
    )
    parser.add_argument("--db", default="tournament.db", help="Path to SQLite database file")
    parser.add_argument(
        "--promote-to", default=None,
        help="After collecting into --db, publish it to this path (core data first, "
             "then the full DB) via atomic os.replace. The web app uses this so all "
             "file ops happen in this standalone process, where file I/O works, rather "
             "than in its gunicorn worker thread.",
    )
    args = parser.parse_args()
    collect(args.league_id, args.db, promote_to=args.promote_to)


if __name__ == "__main__":
    main()
