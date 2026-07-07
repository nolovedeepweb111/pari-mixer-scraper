from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, send_from_directory
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from pari_mixer_scraper.analysis import compute_team_stats, compute_tournament_hero_stats, generate_coach_text
from pari_mixer_scraper.collect import DEFAULT_LEAGUE_ID
from pari_mixer_scraper.mixercup_client import MixerCupClient, pair_substitution_events
from pari_mixer_scraper.models import (
    Base, Hero, Match, MatchDraftEntry, MatchPlayer, Player, SubstitutionEvent, Team,
    build_engine, configure_sqlite,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = os.environ.get("TOURNAMENT_DB", str(BASE_DIR / "tournament.db"))
LEAGUE_ID = int(os.environ.get("LEAGUE_ID", DEFAULT_LEAGUE_ID))

app = Flask(__name__, static_folder="static", static_url_path="")
engine = configure_sqlite(build_engine(DB_PATH))
Base.metadata.create_all(engine)

_collect_state = {"running": False, "log": [], "error": None, "new_matches": None, "started_at": None}
_collect_lock = threading.Lock()
# If a run has been "running" longer than this with no sign of life, treat
# it as dead (stuck thread, killed worker that never reset the flag, etc.)
# and allow a new attempt rather than blocking the site's data forever.
_STALE_RUN_SECONDS = 15 * 60


def _append_log(msg: str) -> None:
    _collect_state["log"].append(msg)
    _collect_state["log"] = _collect_state["log"][-200:]


def _run_collect() -> None:
    """Collects into a side file via a *separate process*, then atomically
    swaps it in.

    Writing SQLite from a background thread of this gunicorn worker hung
    indefinitely on Render no matter what we tried (isolated build file, no
    fsync, batched writes - every variant still wedged mid-write), while the
    identical collection code run as a standalone OS process works reliably.
    So instead of calling collect() in-thread, we spawn it as its own
    process (plain subprocess, not multiprocessing - the latter's
    resource-tracker fork hung here) pointed at a fresh <db>.build. When it
    exits cleanly we os.replace() the build file over the live one and
    dispose the app's pool so requests reopen the fresh data. The site keeps
    serving the previous data throughout. We deliberately do NO file I/O in
    this thread beyond spawning the child - even that was suspect on Render -
    and rebuild from scratch rather than seeding a copy (drafts/subs are all
    re-fetched, so nothing is lost)."""
    # Unique per run so we never touch a stale/half-written build file from
    # a prior run (and so this thread needs no os.remove before spawning).
    build_path = f"{DB_PATH}.build.{os.getpid()}.{int(time.monotonic() * 1000)}"
    try:
        _append_log("Collector thread started; spawning process...")
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "pari_mixer_scraper.collect",
             "--db", build_path, "--league-id", str(LEAGUE_ID)],
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                _append_log(line)
        proc.wait()
        if proc.returncode != 0:
            _collect_state["error"] = f"collector process exited with code {proc.returncode}"
            _append_log(_collect_state["error"])
            return

        _append_log("Swapping the freshly built database in...")
        engine.dispose()
        os.replace(build_path, DB_PATH)
        engine.dispose()
        _append_log("Database swap complete.")
    except Exception as e:
        _collect_state["error"] = str(e)
        _append_log(f"ERROR: {e}")
    finally:
        _collect_state["running"] = False


def _start_collect_background(force: bool = False) -> bool:
    """Returns False if a (non-stale) collection is already running."""
    with _collect_lock:
        started_at = _collect_state.get("started_at")
        is_stale = started_at is not None and (time.monotonic() - started_at) > _STALE_RUN_SECONDS
        if _collect_state["running"] and not (force or is_stale):
            return False
        if _collect_state["running"] and is_stale:
            _append_log("Previous run looked stuck (no progress for 15+ min) - starting a new one.")
        _collect_state.update({
            "running": True, "log": list(_collect_state["log"]) if is_stale else [],
            "error": None, "new_matches": None, "started_at": time.monotonic(),
        })
        threading.Thread(target=_run_collect, daemon=True).start()
    return True


def _auto_collect_if_empty() -> None:
    """Free hosting tiers (e.g. Render's free web service) reset the local
    filesystem on every cold start, wiping tournament.db. Rather than
    showing an empty site until someone notices and clicks "Обновить
    матчи", kick off a collection automatically whenever the database has
    no teams yet - self-healing after a reset, harmless no-op otherwise."""
    with Session(engine) as session:
        has_teams = session.execute(select(Team.team_id).limit(1)).first()
    if not has_teams:
        _start_collect_background()


def _collect_scheduler_loop(interval_seconds: int) -> None:
    while True:
        time.sleep(interval_seconds)
        try:
            _start_collect_background()
        except Exception as e:
            # Never let an unexpected error here kill the loop silently -
            # Python threads don't auto-restart, so one uncaught exception
            # would permanently stop future scheduled collections with no
            # visible sign anything was wrong.
            _append_log(f"Periodic collect trigger failed: {e}")


def _start_periodic_collect() -> None:
    """Keeps match data fresh with no manual trigger. Only useful while the
    process is actually running - on free hosting tiers the service spins
    down after ~15 min idle, so this alone won't refresh an asleep deploy.
    Pair with an external ping (e.g. the GitHub Actions workflow in
    .github/workflows/) that hits /api/collect on a schedule - that both
    wakes the service and triggers a collect."""
    interval_seconds = int(os.environ.get("COLLECT_INTERVAL_SECONDS", 10 * 60))
    if interval_seconds <= 0:
        return
    threading.Thread(target=_collect_scheduler_loop, args=(interval_seconds,), daemon=True).start()


_auto_collect_if_empty()
_start_periodic_collect()


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


_mixer_client = MixerCupClient()
_mixer_tournament_id_cache: int | None = None


def _resolve_mixer_tournament_id() -> int | None:
    global _mixer_tournament_id_cache
    if _mixer_tournament_id_cache is not None:
        return _mixer_tournament_id_cache
    env_id = os.environ.get("MIXER_TOURNAMENT_ID")
    if env_id:
        _mixer_tournament_id_cache = int(env_id)
        return _mixer_tournament_id_cache
    try:
        active = _mixer_client.get_active_tournament()
    except Exception:
        return None
    if active:
        _mixer_tournament_id_cache = active["id"]
    return _mixer_tournament_id_cache


def _get_next_opponent(mixer_uuid: str) -> dict | None:
    tournament_id = _resolve_mixer_tournament_id()
    if tournament_id is None:
        return None
    try:
        opponent = _mixer_client.get_next_opponent(tournament_id, mixer_uuid)
    except Exception:
        return None
    if opponent is None:
        return None

    with Session(engine) as session:
        opponent["opponent_team_id"] = session.execute(
            select(Team.team_id).where(Team.mixer_uuid == opponent["opponent_mixer_uuid"])
        ).scalar_one_or_none()
    return opponent


def _team_total_mmr(session: Session, team_id: int) -> float | None:
    rows = session.execute(
        select(Player.mmr).where(Player.team_id == team_id, Player.roster_confirmed.is_(True))
    ).scalars().all()
    total = sum(m for m in rows if m is not None)
    return total or None


def _get_substitution_history(session: Session, team_id: int) -> list[dict]:
    """Reads from our own SubstitutionEvent table (synced during collect(),
    see sync_substitution_history) rather than querying mixer-cup.gg live -
    their own substitution history has been observed to disappear
    periodically, so this is the durable copy."""
    events = session.execute(
        select(SubstitutionEvent)
        .where(SubstitutionEvent.team_id == team_id)
        .order_by(SubstitutionEvent.occurred_at)
    ).scalars().all()
    raw = [
        {
            "type": e.event_type, "nickname": e.nickname, "rating": e.rating,
            "queue_position": e.queue_position, "occurred_at": e.occurred_at,
        }
        for e in events
    ]
    swaps = pair_substitution_events(raw)

    # MixerCup doesn't expose the team's historical total rating, only its
    # current one - so reconstruct it by walking the swaps backward from
    # today's total, undoing each swap's rating_diff in turn. This assumes
    # the other roster slots' ratings stayed constant between swaps, which
    # isn't strictly true (players' ratings drift with every game) but is
    # the best available approximation without historical snapshots.
    running_total = _team_total_mmr(session, team_id)
    for swap in reversed(swaps):
        swap["team_rating_after"] = running_total
        if running_total is None:
            swap["team_rating_before"] = None
            continue
        delta = swap["rating_diff"] or 0
        running_total = running_total - delta
        swap["team_rating_before"] = running_total

    return swaps


def _roster_filter(session: Session, team_id: int):
    """MixerCup-confirmed roster for this team, if we have one; otherwise
    fall back to everyone who has ever played under this team_id (covers
    teams we haven't been able to link to mixer-cup.gg yet)."""
    confirmed = set(session.execute(
        select(Player.account_id).where(Player.team_id == team_id, Player.roster_confirmed.is_(True))
    ).scalars())
    if confirmed:
        return Player.account_id.in_(confirmed)
    return MatchPlayer.team_id == team_id


@app.get("/api/teams")
def api_teams():
    with Session(engine) as session:
        teams = session.execute(select(Team.team_id, Team.name).order_by(Team.name)).all()

        result = []
        for team_id, name in teams:
            player_filter = _roster_filter(session, team_id)
            # MatchPlayer has one row per match a player appeared in, so a
            # plain join+sum would count their mmr once per match played.
            # distinct() collapses that back down to one row per player,
            # matching how the team-detail endpoint computes it.
            rows = session.execute(
                select(Player.account_id, Player.mmr)
                .join(MatchPlayer, MatchPlayer.account_id == Player.account_id)
                .where(MatchPlayer.team_id == team_id, player_filter)
                .distinct()
            ).all()
            # Teams with only a single player are almost always admin/test
            # teams from a stray match rather than a real tournament squad.
            if len(rows) > 1:
                total_mmr = sum(mmr for _, mmr in rows if mmr is not None) or None
                result.append({
                    "team_id": team_id,
                    "name": name or f"Team {team_id}",
                    "player_count": len(rows),
                    "total_mmr": total_mmr,
                })

    result.sort(key=lambda t: t["total_mmr"] if t["total_mmr"] is not None else -1, reverse=True)
    return jsonify(result)


def _hero_icon_slug(internal_name: str) -> str:
    """'npc_dota_hero_antimage' -> 'antimage', matching the filenames Valve
    serves hero icons under on the Steam CDN."""
    prefix = "npc_dota_hero_"
    return internal_name[len(prefix):] if internal_name.startswith(prefix) else internal_name


def _recent_drafts(session: Session, team_id: int, limit: int = 23) -> list[dict]:
    """Full draft (both teams' picks and bans, in actual draft order) for
    this team's last few matches - not just this team's own bans, since
    what the *opponent* banned against them is the more useful signal."""
    matches = session.execute(
        select(Match.match_id, Match.radiant_team_id, Match.dire_team_id, Match.radiant_win)
        .where((Match.radiant_team_id == team_id) | (Match.dire_team_id == team_id))
        .order_by(Match.start_time.desc())
    ).all()

    drafts = []
    for match_id, radiant_team_id, dire_team_id, radiant_win in matches:
        if len(drafts) >= limit:
            break
        opponent_team_id = dire_team_id if radiant_team_id == team_id else radiant_team_id
        team_won = None
        if radiant_win is not None:
            team_won = radiant_win if radiant_team_id == team_id else not radiant_win

        rows = session.execute(
            select(
                MatchDraftEntry.order_num, MatchDraftEntry.is_pick, MatchDraftEntry.team_id,
                Hero.localized_name, Hero.name,
            )
            .join(Hero, Hero.hero_id == MatchDraftEntry.hero_id)
            .where(MatchDraftEntry.match_id == match_id)
            .order_by(MatchDraftEntry.order_num)
        ).all()
        if not rows:
            continue

        opponent = session.get(Team, opponent_team_id) if opponent_team_id else None

        def side(rows, this_team_id):
            return [
                {
                    "order": order_num,
                    "is_pick": is_pick,
                    "hero": hero_name,
                    "hero_icon": _hero_icon_slug(internal_name),
                }
                for order_num, is_pick, tid, hero_name, internal_name in rows
                if tid == this_team_id
            ]

        drafts.append({
            "match_id": match_id,
            "team_won": team_won,
            "team_entries": side(rows, team_id),
            "opponent_name": opponent.name if opponent and opponent.name else f"Team {opponent_team_id}",
            "opponent_entries": side(rows, opponent_team_id),
        })
    return drafts


@app.get("/api/teams/<int:team_id>")
def api_team_detail(team_id: int):
    with Session(engine) as session:
        team = session.get(Team, team_id)
        if team is None:
            return jsonify({"error": "not found"}), 404

        player_filter = _roster_filter(session, team_id)
        # decided = games with a known result (radiant_win is not null);
        # win rate is wins/decided, not wins/games, so a still-unresolved
        # match doesn't silently drag the rate down.
        decided = case((Match.radiant_win.is_not(None), 1), else_=0)
        won = case((MatchPlayer.is_radiant == Match.radiant_win, 1), else_=0)
        rows = session.execute(
            select(
                Player.account_id, Player.name, Player.mmr,
                Hero.hero_id, Hero.localized_name,
                func.count(), func.sum(decided), func.sum(won),
            )
            .join(MatchPlayer, MatchPlayer.account_id == Player.account_id)
            .join(Hero, Hero.hero_id == MatchPlayer.hero_id)
            .join(Match, Match.match_id == MatchPlayer.match_id)
            .where(MatchPlayer.team_id == team_id, player_filter)
            .group_by(Player.account_id, Hero.hero_id)
        ).all()

        recent_drafts = _recent_drafts(session, team_id)
        mixer_uuid = team.mixer_uuid

    players: dict[int, dict] = {}
    for account_id, name, mmr, hero_id, hero_name, games, decided_games, wins in rows:
        entry = players.setdefault(account_id, {
            "account_id": account_id,
            "name": name or f"account {account_id}",
            "mmr": mmr,
            "heroes": [],
        })
        win_rate = round(100 * wins / decided_games) if decided_games else None
        entry["heroes"].append({
            "hero_id": hero_id, "name": hero_name, "games": games, "win_rate": win_rate,
        })

    for entry in players.values():
        entry["heroes"].sort(key=lambda h: -h["games"])

    if len(players) <= 1:
        return jsonify({"error": "not found"}), 404

    total_mmr = sum(p["mmr"] for p in players.values() if p["mmr"] is not None) or None
    next_opponent = _get_next_opponent(mixer_uuid) if mixer_uuid else None

    return jsonify({
        "team_id": team_id,
        "name": team.name or f"Team {team_id}",
        "total_mmr": total_mmr,
        "players": sorted(players.values(), key=lambda p: p["name"]),
        "recent_drafts": recent_drafts,
        "next_opponent": next_opponent,
    })


@app.get("/api/teams/<int:team_id>/analysis")
def api_team_analysis(team_id: int):
    with Session(engine) as session:
        team = session.get(Team, team_id)
        if team is None:
            return jsonify({"error": "not found"}), 404

        team_name = team.name or f"Team {team_id}"
        stats = compute_team_stats(session, team_id)
        text = generate_coach_text(team_name, stats)

    return jsonify({
        "team_id": team_id,
        "name": team_name,
        "text": text,
        "games": stats["games"],
        "decided": stats["decided"],
        "wins": stats["wins"],
        "win_rate": stats["win_rate"],
        "top_picks": [{"hero": h, "count": c} for h, c in stats["top_picks"]],
        "signature_heroes": [
            {"hero": h, "wins": w, "games": g, "win_rate": wr} for h, w, g, wr in stats["signature_heroes"]
        ],
        "first_picks": [{"hero": h, "count": c} for h, c in stats["first_picks"]],
        "enemy_bans": [{"hero": h, "count": c} for h, c in stats["enemy_bans"]],
        "own_bans": [{"hero": h, "count": c} for h, c in stats["own_bans"]],
    })


@app.get("/api/teams/<int:team_id>/substitutions")
def api_team_substitutions(team_id: int):
    with Session(engine) as session:
        team = session.get(Team, team_id)
        if team is None:
            return jsonify({"error": "not found"}), 404
        swaps = _get_substitution_history(session, team_id)

    return jsonify({"team_id": team_id, "substitutions": swaps})


@app.get("/api/substitutions")
def api_all_substitutions():
    """Every substitution across the tournament, newest first, with the
    team it happened in - the per-team tab shows the same data scoped to
    one team; this powers the tournament-wide substitutions page."""
    with Session(engine) as session:
        teams = session.execute(
            select(Team.team_id, Team.name)
            .where(Team.team_id.in_(select(SubstitutionEvent.team_id).distinct()))
        ).all()

        all_swaps = []
        for team_id, team_name in teams:
            for swap in _get_substitution_history(session, team_id):
                swap["team_id"] = team_id
                swap["team_name"] = team_name or f"Team {team_id}"
                all_swaps.append(swap)

    all_swaps.sort(key=lambda s: s["at"], reverse=True)
    return jsonify({"substitutions": all_swaps})


@app.get("/api/tournament/heroes")
def api_tournament_heroes():
    with Session(engine) as session:
        stats = compute_tournament_hero_stats(session)
    return jsonify(stats)


@app.get("/api/collect/status")
def api_collect_status():
    return jsonify(_collect_state)


@app.post("/api/collect")
def api_collect_start():
    if not _start_collect_background():
        return jsonify({"error": "collection already running"}), 409
    return jsonify({"started": True})


if __name__ == "__main__":
    # debug=True enables Werkzeug's interactive debugger, which lets anyone
    # who can reach the server execute arbitrary Python on an unhandled
    # exception. Safe by default; opt in explicitly for local-only dev.
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="127.0.0.1", port=5000, debug=debug)
