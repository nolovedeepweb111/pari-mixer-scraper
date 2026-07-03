from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, send_from_directory
from sqlalchemy import case, create_engine, func, select
from sqlalchemy.orm import Session

from pari_mixer_scraper.analysis import compute_team_stats, generate_coach_text
from pari_mixer_scraper.collect import DEFAULT_LEAGUE_ID, collect
from pari_mixer_scraper.models import Base, Hero, Match, MatchDraftEntry, MatchPlayer, Player, Team, configure_sqlite

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = os.environ.get("TOURNAMENT_DB", str(BASE_DIR / "tournament.db"))
LEAGUE_ID = int(os.environ.get("LEAGUE_ID", DEFAULT_LEAGUE_ID))

app = Flask(__name__, static_folder="static", static_url_path="")
engine = configure_sqlite(create_engine(f"sqlite:///{DB_PATH}"))
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
    try:
        new_count = collect(LEAGUE_ID, DB_PATH, progress=_append_log, engine=engine)
        _collect_state["new_matches"] = new_count
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
        _start_collect_background()


def _start_periodic_collect() -> None:
    """Keeps match data fresh without anyone having to click "Обновить
    матчи". Only useful while the process is actually running - on free
    hosting tiers the service spins down after ~15 min idle, so this alone
    won't refresh an asleep deploy. Pair with an external ping (e.g. the
    GitHub Actions workflow in .github/workflows/) that hits /api/collect
    on a schedule - that both wakes the service and triggers a collect."""
    interval_seconds = int(os.environ.get("COLLECT_INTERVAL_SECONDS", 20 * 60))
    if interval_seconds <= 0:
        return
    threading.Thread(target=_collect_scheduler_loop, args=(interval_seconds,), daemon=True).start()


_auto_collect_if_empty()
_start_periodic_collect()


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


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


def _recent_drafts(session: Session, team_id: int, limit: int = 5) -> list[dict]:
    """Full draft (both teams' picks and bans, in actual draft order) for
    this team's last few matches - not just this team's own bans, since
    what the *opponent* banned against them is the more useful signal."""
    matches = session.execute(
        select(Match.match_id, Match.radiant_team_id, Match.dire_team_id)
        .where((Match.radiant_team_id == team_id) | (Match.dire_team_id == team_id))
        .order_by(Match.start_time.desc())
    ).all()

    drafts = []
    for match_id, radiant_team_id, dire_team_id in matches:
        if len(drafts) >= limit:
            break
        opponent_team_id = dire_team_id if radiant_team_id == team_id else radiant_team_id

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

    return jsonify({
        "team_id": team_id,
        "name": team.name or f"Team {team_id}",
        "total_mmr": total_mmr,
        "players": sorted(players.values(), key=lambda p: p["name"]),
        "recent_drafts": recent_drafts,
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
