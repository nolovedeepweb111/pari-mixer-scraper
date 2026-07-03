from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from .mixercup_client import MixerCupClient
from .models import (
    Base, Hero, Match, MatchDraftEntry, MatchPlayer, Player, Team,
    build_database_url, configure_sqlite,
)
from .opendota_client import OpenDotaClient
from .roster_overrides import MANUAL_ROSTER_OVERRIDES
from .steam_client import SteamClient

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DEFAULT_LEAGUE_ID = 19924  # Pari Mixer Cup

ProgressFn = Callable[[str], None]


def sync_heroes(session: Session, client: OpenDotaClient) -> None:
    for h in client.get_heroes():
        hero = session.get(Hero, h["id"])
        if hero is None:
            session.add(Hero(hero_id=h["id"], name=h["name"], localized_name=h["localized_name"]))
        else:
            hero.name = h["name"]
            hero.localized_name = h["localized_name"]
    session.commit()


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
) -> dict[int, str | None]:
    """Small/amateur teams are often missing from OpenDota's team index, so
    try Valve's own GetTeamInfo (via Steam) first and fall back to OpenDota."""
    names: dict[int, str | None] = {}
    for team_id in team_ids:
        if not team_id:
            continue

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
    match_id = match["match_id"]
    radiant_team_id = match.get("radiant_team_id")
    dire_team_id = match.get("dire_team_id")
    upsert_team(session, radiant_team_id, team_names.get(radiant_team_id))
    upsert_team(session, dire_team_id, team_names.get(dire_team_id))

    session.add(Match(
        match_id=match_id,
        league_id=league_id,
        start_time=match.get("start_time"),
        duration=match.get("duration"),
        radiant_team_id=radiant_team_id,
        dire_team_id=dire_team_id,
        radiant_win=match.get("radiant_win"),
    ))

    for p in match["players"]:
        account_id = p["account_id"]
        is_radiant = bool(p["is_radiant"])
        team_id = radiant_team_id if is_radiant else dire_team_id

        upsert_player(session, account_id, None, team_id, pro_directory)

        session.add(MatchPlayer(
            match_id=match_id,
            account_id=account_id,
            hero_id=p["hero_id"],
            team_id=team_id,
            is_radiant=is_radiant,
            kills=p.get("kills"),
            deaths=p.get("deaths"),
            assists=p.get("assists"),
        ))


def sync_draft_data(session: Session, client: OpenDotaClient, progress: ProgressFn) -> None:
    """Backfills picks/bans for matches that don't have any draft rows yet.
    Steam's GetMatchHistory (our primary, cheap source) doesn't include the
    draft, so this costs one OpenDota call per match - but only once, since
    matches already covered are skipped on later runs."""
    has_draft = {row[0] for row in session.execute(select(MatchDraftEntry.match_id))}
    all_match_ids = [row[0] for row in session.execute(select(Match.match_id))]
    missing = [m for m in all_match_ids if m not in has_draft]
    if not missing:
        return

    progress(f"Fetching picks/bans for {len(missing)} match(es)...")
    fetched_empty = 0
    for i, match_id in enumerate(missing, start=1):
        try:
            detail = client.get_match(match_id)
        except Exception as e:
            progress(f"Could not fetch picks/bans for match {match_id}: {e}")
            continue

        picks_bans = detail.get("picks_bans") or []
        if not picks_bans:
            fetched_empty += 1
            continue

        match = session.get(Match, match_id)
        for pb in picks_bans:
            team_id = match.radiant_team_id if pb.get("team") == 0 else match.dire_team_id
            session.add(MatchDraftEntry(
                match_id=match_id,
                order_num=pb.get("order", 0),
                hero_id=pb.get("hero_id"),
                team_id=team_id,
                is_pick=bool(pb.get("is_pick")),
            ))
        session.commit()
        if i % 10 == 0 or i == len(missing):
            progress(f"  picks/bans {i}/{len(missing)}")

    if fetched_empty:
        progress(f"{fetched_empty} match(es) had no draft data (not captain's mode, or not parsed by OpenDota)")


def enrich_missing_player_names(session: Session, client: OpenDotaClient, progress: ProgressFn) -> None:
    """Steam's GetMatchHistory doesn't include nicknames, only account_id.
    Fill those in from OpenDota's player profile endpoint (best-effort)."""
    missing = session.execute(select(Player).where(Player.name.is_(None))).scalars().all()
    if not missing:
        return
    progress(f"Resolving nicknames for {len(missing)} player(s) without a known name...")
    for player in missing:
        try:
            info = client.get_player(player.account_id)
            name = (info.get("profile") or {}).get("personaname")
            if name:
                player.name = name
        except Exception:
            continue
    session.commit()


def enrich_missing_team_names(
    session: Session,
    od_client: OpenDotaClient,
    steam_client: SteamClient | None,
    progress: ProgressFn,
) -> None:
    missing = session.execute(select(Team).where(Team.name.is_(None))).scalars().all()
    if not missing:
        return
    progress(f"Resolving names for {len(missing)} team(s) without a known name...")
    team_ids = {t.team_id for t in missing}
    names = resolve_team_names(od_client, steam_client, team_ids, progress)
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
        player = by_account_id.get(account_id)
        if player is None:
            continue
        player.roster_confirmed = True
        if nickname:
            player.name = nickname
        if mp.get("rating") is not None:
            player.mmr = mp["rating"]


def link_mixercup_data(
    session: Session,
    mixer_client: MixerCupClient,
    tournament_id: int,
    progress: ProgressFn,
) -> None:
    """Uses mixer-cup.gg's own GraphQL API to attach real team names and
    current roster nicknames to the matches we already collected from
    Steam/OpenDota. MixerCup's `Games.matchId` field is the Dota match_id,
    so completed games there give a direct match_id -> (team1, team2) link;
    which mixer team is which side (radiant/dire) is then determined by
    comparing Steam account_ids against who actually played in that match."""
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
        if score_a == score_b:
            ambiguous += 1
            continue

        radiant_team, dire_team = (team1, team2) if score_a > score_b else (team2, team1)

        result = g.get("result")
        if result in ("WIN1", "WIN2"):
            team1_won = result == "WIN1"
            match.radiant_win = team1_won if radiant_team is team1 else not team1_won

        for steam_team_id, mixer_team in ((match.radiant_team_id, radiant_team), (match.dire_team_id, dire_team)):
            if steam_team_id is None:
                continue
            team_row = session.get(Team, steam_team_id)
            if team_row is not None and mixer_team.get("name"):
                team_row.name = mixer_team["name"]
            _apply_confirmed_roster(session, steam_team_id, mixer_team)
        linked += 1

    session.commit()
    progress(f"MixerCup linking: {linked} matches linked, {ambiguous} ambiguous, {skipped} skipped")


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


def collect(league_id: int, db_path: str, progress: ProgressFn | None = None, engine=None) -> int:
    """Runs a full collection pass. Returns the number of newly stored matches.

    Pass an existing `engine` when calling from a process that already has
    one open on the same db_path (e.g. the Flask app) - two separate
    SQLAlchemy Engines/pools pointed at the same SQLite file from the same
    process is an easy way to end up with avoidable lock contention."""
    progress = progress or log.info
    progress("Starting collection...")

    if engine is None:
        engine = configure_sqlite(create_engine(build_database_url(db_path)))
    Base.metadata.create_all(engine)

    od_client = OpenDotaClient()
    steam_api_key = os.environ.get("STEAM_API_KEY")
    steam_client = SteamClient(steam_api_key) if steam_api_key else None
    if steam_client is None:
        progress("STEAM_API_KEY not set - fetching via OpenDota only (see README for how to add a Steam Web API key).")

    with Session(engine) as session:
        progress("Syncing hero list...")
        sync_heroes(session, od_client)

        progress(f"Fetching match list for league_id={league_id}...")
        matches = fetch_all_league_matches(league_id, od_client, steam_client, progress)
        if not matches:
            progress(
                f"No matches found for league_id={league_id} yet. Either the tournament "
                "hasn't been played, or matches aren't tagged with this league_id yet."
            )
            return 0

        existing_match_ids = {row[0] for row in session.execute(select(Match.match_id))}
        new_matches = [m for m in matches if m["match_id"] not in existing_match_ids]
        progress(f"{len(matches)} matches total, {len(new_matches)} new")

        # Saving matches with no team name yet (rather than looking them up
        # here via Steam/OpenDota, which is almost always a dead-end 404 for
        # these ad-hoc mixer teams) gets a usable site up fast: real names,
        # rosters and MMR all come from the MixerCup link-up right below,
        # which is one cheap GraphQL call - not a slow per-team lookup loop.
        if new_matches:
            for i, m in enumerate(new_matches, start=1):
                progress(f"[{i}/{len(new_matches)}] Saving match {m['match_id']}")
                persist_match(session, league_id, m, {}, {})
                session.commit()

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
            link_mixercup_data(session, mixer_client, mixer_tournament_id, progress)

        apply_manual_roster_overrides(session, progress)
        session.commit()
        progress("Core team/roster data ready - filling in the slower supplementary details now.")

        # Everything from here on is supplementary (nicknames for players
        # MixerCup didn't cover, fallback team names, draft history for the
        # "Последние драфты" tab) - the site already looks right without it.
        progress("Syncing pro player directory...")
        pro_directory = fetch_pro_player_directory(od_client)
        for player in session.execute(select(Player).where(Player.name.is_(None))).scalars():
            pro_info = pro_directory.get(player.account_id)
            if pro_info and pro_info.get("name"):
                player.name = pro_info["name"]
        session.commit()

        enrich_missing_player_names(session, od_client, progress)
        enrich_missing_team_names(session, od_client, steam_client, progress)
        sync_draft_data(session, od_client, progress)

    progress("Done.")
    return len(new_matches)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect player/hero data for a Dota 2 esports league into SQLite."
    )
    parser.add_argument(
        "--league-id", type=int, default=DEFAULT_LEAGUE_ID,
        help=f"OpenDota/Dotabuff league id (default: {DEFAULT_LEAGUE_ID}, Pari Mixer Cup)",
    )
    parser.add_argument("--db", default="tournament.db", help="Path to SQLite database file")
    args = parser.parse_args()
    collect(args.league_id, args.db)


if __name__ == "__main__":
    main()
