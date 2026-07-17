from __future__ import annotations

import hashlib
import hmac
import os
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, request, send_from_directory, session
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from pari_mixer_scraper.analysis import compute_team_stats, compute_tournament_hero_stats, generate_coach_text
from pari_mixer_scraper.collect import DEFAULT_LEAGUE_ID
from pari_mixer_scraper.mixercup_client import MixerCupClient, pair_substitution_events
from pari_mixer_scraper.models import (
    Base, Hero, Match, MatchDraftEntry, MatchPlayer, Player, QueuedPlayer,
    SubstitutionEvent, Team,
    build_engine, configure_sqlite,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = os.environ.get("TOURNAMENT_DB", str(BASE_DIR / "tournament.db"))
# Comma-separated so the database can span tournaments: keep earlier
# tournaments' league ids in the list and their matches stay re-fetchable
# from Steam after Render wipes the disk. The LAST id is the current
# tournament (used for tournament-wide stats).
LEAGUE_IDS = [
    int(x) for x in os.environ.get("LEAGUE_ID", str(DEFAULT_LEAGUE_ID)).replace(";", ",").split(",")
    if x.strip()
]
CURRENT_LEAGUE_ID = LEAGUE_IDS[-1]

# league_id -> display name, for tournament dividers on player pages.
# Override/extend via env LEAGUE_LABELS="19924:PARI Mixer Cup #1;NNNN:PARI Mixer Cup #2".
LEAGUE_LABELS: dict[int, str] = {19924: "PARI Mixer Cup #1"}
for _pair in os.environ.get("LEAGUE_LABELS", "").replace(",", ";").split(";"):
    if ":" in _pair:
        _lid, _label = _pair.split(":", 1)
        try:
            LEAGUE_LABELS[int(_lid.strip())] = _label.strip()
        except ValueError:
            pass

app = Flask(__name__, static_folder="static", static_url_path="")
# NullPool: the collector swaps a freshly built DB file in via os.replace,
# so a pooled connection would keep reading the old (now-unlinked) file.
# Opening a fresh connection per request always sees the current file.
engine = configure_sqlite(build_engine(DB_PATH, poolclass=NullPool))
Base.metadata.create_all(engine)

# ---------------------------------------------------------------------------
# Access control
#
# Site is private when ACCESS_KEYS is set (comma-separated keys the owner
# hands out, one per person). Each key can be activated on at most
# MAX_DEVICES_PER_KEY browsers - the first browsers to use it claim its
# slots, so a shared key is rejected on further devices.
#
# The repo is PUBLIC, and bindings must survive restarts (the /api/backup
# git branch), so bindings are stored keyed by HMAC(key) with AUTH_SECRET -
# the keys themselves never touch git. Device ids are random, non-secret.
# ---------------------------------------------------------------------------
_ACCESS_KEYS = [k.strip() for k in os.environ.get("ACCESS_KEYS", "").split(",") if k.strip()]
AUTH_ENABLED = bool(_ACCESS_KEYS)
# Stable across restarts without extra config: falls back to a hash of the
# key set (changing the keys logs everyone out, which is acceptable).
AUTH_SECRET = os.environ.get("AUTH_SECRET") or ("keyset:" + ",".join(sorted(_ACCESS_KEYS)))
MAX_DEVICES_PER_KEY = int(os.environ.get("MAX_DEVICES_PER_KEY", "2"))
# Protects the operational endpoints (collect/backup/archive) called by the
# GitHub Actions workflow. If empty, those stay open (so nothing breaks
# before the owner sets it up).
OPS_TOKEN = os.environ.get("OPS_TOKEN", "")

app.secret_key = hashlib.sha256(("session:" + AUTH_SECRET).encode()).digest()
app.permanent_session_lifetime = timedelta(days=60)


def _key_hash(key: str) -> str:
    return hmac.new(AUTH_SECRET.encode(), key.encode(), hashlib.sha256).hexdigest()


VALID_KEY_HASHES = {_key_hash(k) for k in _ACCESS_KEYS}
_device_bindings: dict[str, set[str]] = {}  # key_hash -> {device_id}
_bindings_lock = threading.Lock()


def _snapshot_bindings() -> dict[str, set[str]]:
    with _bindings_lock:
        return {kh: set(devs) for kh, devs in _device_bindings.items() if devs}


def _restore_access_bindings() -> None:
    """Load device bindings from the backup branch at startup so the
    anti-sharing state survives restarts/deploys. Only bindings for keys
    still valid are kept (removing a key from ACCESS_KEYS drops its
    bindings). Best-effort; runs in a thread so it never blocks boot."""
    if not AUTH_ENABLED:
        return
    import requests

    from pari_mixer_scraper.collect import _DEFAULT_BACKUP_URL
    url = os.environ.get("BACKUP_RESTORE_URL", _DEFAULT_BACKUP_URL)
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return
        data = r.json()
    except Exception:
        return
    bindings = data.get("access_bindings") or {}
    with _bindings_lock:
        for kh, devs in bindings.items():
            if kh in VALID_KEY_HASHES and isinstance(devs, list):
                _device_bindings.setdefault(kh, set()).update(
                    d for d in devs if isinstance(d, str)
                )


if AUTH_ENABLED:
    threading.Thread(target=_restore_access_bindings, daemon=True).start()

_collect_state = {"running": False, "log": [], "error": None, "new_matches": None,
                  "started_at": None, "pid": None}
_collect_lock = threading.Lock()
# If a run has been "running" longer than this with no sign of life, treat
# it as dead (stuck thread, killed worker that never reset the flag, etc.)
# and allow a new attempt rather than blocking the site's data forever.
# A run that is merely slow must NOT reach this: the collector budgets its
# expensive phases by wall clock (SEED_TIME_BUDGET_SECONDS and friends) to
# ~10 min total, so anything still alive this much later really is wedged.
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
    # a prior run.
    build_path = f"{DB_PATH}.build.{os.getpid()}.{int(time.monotonic() * 1000)}"
    try:
        _append_log("Spawning collector process...")
        # The gunicorn worker THREAD cannot be trusted with blocking file I/O
        # on Render - reading/copying files from it hung indefinitely (which
        # is what stalled every prior approach). So this thread does no file
        # I/O at all: it only os.posix_spawn()s the collector (posix_spawn,
        # not fork, which deadlocked from this multi-threaded worker) and
        # then waits on it with waitpid (a syscall, not file I/O). The child
        # process does ALL the file work - collecting into build_path and
        # then os.replace()-ing it over the live DB (--promote-to) - since
        # file I/O works fine in a standalone process, exactly as the CLI
        # does. The child's own stdout/stderr are inherited, so its progress
        # shows up in Render's log stream.
        child_env = dict(os.environ)
        child_env["PYTHONPATH"] = str(BASE_DIR) + os.pathsep + child_env.get("PYTHONPATH", "")
        pid = os.posix_spawn(
            sys.executable,
            [sys.executable, "-u", "-m", "pari_mixer_scraper.collect",
             "--db", build_path, "--promote-to", DB_PATH,
             "--league-id", ",".join(str(x) for x in LEAGUE_IDS)],
            child_env,
        )
        _collect_state["pid"] = pid
        _append_log(f"Spawned collector pid {pid}; waiting for it to finish...")

        # Hard deadline: a full from-scratch rebuild takes ~7-10 min on this
        # tier; anything past this is a wedged child. Kill it and free the
        # run slot instead of sitting at running=true forever (the child's
        # own promotes are atomic, so killing it never corrupts the live DB).
        deadline = time.monotonic() + 30 * 60
        exit_code = None
        while exit_code is None:
            time.sleep(2)
            if time.monotonic() > deadline:
                _append_log("Collector exceeded 30 min - killing it.")
                try:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
                except OSError:
                    pass
                _collect_state["error"] = "collector timed out and was killed"
                return
            try:
                wpid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                # Something else (e.g. a gunicorn SIGCHLD handler) already
                # reaped the child; we can't read its status, so assume it
                # finished and let the NullPool app pick up whatever it
                # promoted.
                break
            if wpid != 0:
                exit_code = os.waitstatus_to_exitcode(status)

        if exit_code not in (None, 0):
            _collect_state["error"] = f"collector process exited with code {exit_code}"
            _append_log(_collect_state["error"])
            return

        # The child already collected into a side file and, if it found
        # matches, os.replace()d it over the live DB. Nothing to do here -
        # the app's NullPool engine opens the current file on the next
        # request, so the new data shows up on its own.
        _append_log("Collection complete; data refreshed.")
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
        if _collect_state["running"]:
            # The old child is very likely still alive and working. Starting a
            # second one alongside it puts two collectors on a 0.1-CPU / 512MB
            # free instance, both writing SQLite - that thrashes the box and
            # the SITE stops responding. Kill the old one first; its promotes
            # are atomic, so killing it mid-run never corrupts the live DB.
            old_pid = _collect_state.get("pid")
            _append_log("Previous run looked stuck - killing it before starting a new one.")
            if old_pid:
                try:
                    os.kill(old_pid, 9)
                except OSError:
                    pass
        _collect_state.update({
            "running": True, "log": list(_collect_state["log"]) if is_stale else [],
            "error": None, "new_matches": None, "started_at": time.monotonic(),
            "pid": None,
        })
        threading.Thread(target=_run_collect, daemon=True).start()
    return True


# How long a cold start gets to serve pages before the collector may start.
# A cold start is already the worst moment for a visitor: the free tier only
# wakes on a request, so someone is always waiting on it. Spawning the
# collector at import time put a second process on a 0.1-CPU instance while
# gunicorn was still rendering that person's first page - a wake measured at
# ~122s. The data isn't lost by waiting: nothing can display until the
# collection finishes anyway, so the page may as well arrive first.
_AUTO_COLLECT_DELAY_SECONDS = int(os.environ.get("AUTO_COLLECT_DELAY_SECONDS", "45"))


def _auto_collect_if_empty() -> None:
    """Free hosting tiers (e.g. Render's free web service) reset the local
    filesystem on every cold start, wiping tournament.db. Rather than
    showing an empty site until someone notices and clicks "Обновить
    матчи", kick off a collection automatically whenever the database has
    no teams yet - self-healing after a reset, harmless no-op otherwise.

    Deferred by _AUTO_COLLECT_DELAY_SECONDS so the visitor whose request
    woke the instance gets served before the collector competes for the CPU."""
    def _later() -> None:
        time.sleep(_AUTO_COLLECT_DELAY_SECONDS)
        try:
            with Session(engine) as session:
                has_teams = session.execute(select(Team.team_id).limit(1)).first()
            if not has_teams:
                _start_collect_background()
        except Exception as e:
            _append_log(f"Auto-collect on empty DB failed: {e}")

    threading.Thread(target=_later, daemon=True).start()


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


# --- Access-control gate ---------------------------------------------------

_LOGIN_HTML = """<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PARI Mixer Cup — вход</title>
<style>
  :root { --bg:#23282e; --panel:#2b323c; --border:#334056; --text:#fff; --muted:#cfd4da; --accent:#0396ff; --bad:#eb4242; }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         font-family:"Jost","Segoe UI",Roboto,sans-serif; background:var(--bg); color:var(--text); }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:16px;
          padding:32px; width:100%; max-width:380px; margin:16px; }
  h1 { font-size:22px; margin:0 0 4px; }
  p.sub { color:var(--muted); font-size:14px; margin:0 0 20px; }
  label { display:block; font-size:13px; color:var(--muted); margin-bottom:6px; }
  input { width:100%; padding:11px 14px; font-size:15px; border-radius:8px;
          border:1px solid var(--border); background:#1d2630; color:var(--text); font-family:inherit; }
  input:focus { outline:none; border-color:var(--accent); }
  button { width:100%; margin-top:16px; padding:11px; font-size:15px; font-weight:500;
           border:none; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; font-family:inherit; }
  button:hover { background:#0078d6; }
  button:disabled { opacity:.5; cursor:default; }
  .err { color:var(--bad); font-size:13px; margin-top:12px; min-height:16px; }
  .access { margin-top:20px; padding-top:16px; border-top:1px solid var(--border);
            color:var(--muted); font-size:13px; line-height:1.5; }
  .access b { color:var(--text); }
</style></head><body>
<form class="card" id="f">
  <h1>PARI Mixer Cup</h1>
  <p class="sub">Приватный доступ. Введите ваш ключ.</p>
  <label for="key">Ключ доступа</label>
  <input id="key" name="key" autocomplete="off" autofocus>
  <button id="btn" type="submit">Войти</button>
  <div class="err" id="err"></div>
  <div class="access">
    Для получения доступа к сайту отправьте <b>5 USD</b> пользователю <b>zharok.pcash</b>
    и напишите в дискорде <b>nldw111</b> или в телеграмме <b>@VaxpEe</b>
  </div>
</form>
<script>
  // A stable per-browser id so the same device reuses its key slot, while
  // a different device counts as a new one.
  var dev = localStorage.getItem("pmc_device");
  if (!dev) { dev = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()) + Date.now()); localStorage.setItem("pmc_device", dev); }
  var f = document.getElementById("f"), btn = document.getElementById("btn"), err = document.getElementById("err");
  f.addEventListener("submit", async function(e){
    e.preventDefault(); err.textContent = ""; btn.disabled = true;
    try {
      var r = await fetch("/api/auth/login", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({ key: document.getElementById("key").value, device: dev })
      });
      if (r.ok) { location.href = "/"; return; }
      var d = await r.json().catch(function(){ return {}; });
      err.textContent = d.error || "Не удалось войти.";
    } catch (_) { err.textContent = "Ошибка сети."; }
    btn.disabled = false;
  });
</script>
</body></html>"""


def _session_ok() -> bool:
    kh = session.get("kh")
    device = session.get("device")
    if not kh or not device or kh not in VALID_KEY_HASHES:
        return False
    with _bindings_lock:
        devices = _device_bindings.setdefault(kh, set())
        if device in devices:
            return True
        # Bindings may have been lost (a restart before the next backup).
        # A browser holding a validly-signed session cookie re-claims its
        # slot if there's still room, so legitimate users aren't kicked out.
        if len(devices) < MAX_DEVICES_PER_KEY:
            devices.add(device)
            return True
    return False


def _is_ops_path(p: str) -> bool:
    return p == "/api/collect" or p.startswith("/api/backup") or p.startswith("/api/archive")


@app.before_request
def _gate():
    if not AUTH_ENABLED:
        return
    p = request.path
    if p == "/login" or p.startswith("/api/auth/") or p == "/favicon.ico":
        return
    if _is_ops_path(p):
        if not OPS_TOKEN:
            return
        tok = request.headers.get("X-Ops-Token") or request.args.get("ops_token")
        if tok and hmac.compare_digest(tok, OPS_TOKEN):
            return
        abort(403)
    if _session_ok():
        return
    if p.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401
    return redirect("/login")


@app.post("/api/auth/login")
def api_auth_login():
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    device = (data.get("device") or "").strip()[:80]
    if not key or not device:
        return jsonify({"error": "Введите ключ."}), 400
    kh = _key_hash(key)
    if kh not in VALID_KEY_HASHES:
        return jsonify({"error": "Неверный ключ."}), 403
    with _bindings_lock:
        devices = _device_bindings.setdefault(kh, set())
        if device not in devices:
            if len(devices) >= MAX_DEVICES_PER_KEY:
                return jsonify({
                    "error": "Этот ключ уже используется на другом устройстве."
                }), 403
            devices.add(device)
    session.permanent = True
    session["kh"] = kh
    session["device"] = device
    return jsonify({"ok": True})


@app.get("/api/auth/status")
def api_auth_status():
    return jsonify({"enabled": AUTH_ENABLED})


@app.post("/api/auth/logout")
def api_auth_logout():
    kh = session.get("kh")
    device = session.get("device")
    # Free this device's slot so the user can re-activate elsewhere.
    if kh and device:
        with _bindings_lock:
            _device_bindings.get(kh, set()).discard(device)
    session.clear()
    return jsonify({"ok": True})


@app.get("/login")
def login_page():
    return _LOGIN_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


_mixer_client = MixerCupClient()
_mixer_tournament_id_cache: int | None = None
_mixer_tournament_name_cache: dict[int, str] = {}

# mixer tournament id -> display name, for the tournament dividers on player
# pages. Overridable via env MIXER_TOURNAMENT_LABELS="26:PARI Mixer Cup #1;27:...".
MIXER_TOURNAMENT_LABELS: dict[int, str] = {26: "PARI Mixer Cup #1", 27: "PARI Mixer Cup #2"}
for _pair in os.environ.get("MIXER_TOURNAMENT_LABELS", "").replace(",", ";").split(";"):
    if ":" in _pair:
        _tid, _label = _pair.split(":", 1)
        try:
            MIXER_TOURNAMENT_LABELS[int(_tid.strip())] = _label.strip()
        except ValueError:
            pass


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
        if active.get("name"):
            _mixer_tournament_name_cache[active["id"]] = active["name"]
    return _mixer_tournament_id_cache


def _tournament_label(mixer_tournament_id: int | None, league_id: int | None) -> str:
    """Best display name for the tournament a match belongs to: the live name
    of the active tournament, then the configured mixer-id map, then the
    league-id map, then a generic fallback."""
    if mixer_tournament_id is not None:
        if mixer_tournament_id in _mixer_tournament_name_cache:
            return _mixer_tournament_name_cache[mixer_tournament_id]
        if mixer_tournament_id in MIXER_TOURNAMENT_LABELS:
            return MIXER_TOURNAMENT_LABELS[mixer_tournament_id]
        return f"Турнир {mixer_tournament_id}"
    if league_id in LEAGUE_LABELS:
        return LEAGUE_LABELS[league_id]
    return "Прочие матчи"


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
        # Sidebar shows only the ACTIVE mixer tournament's teams; earlier
        # tournaments' teams stay in the DB (their pages remain reachable
        # from player match history) but off the list. If the active
        # tournament can't be resolved (mixer API down), show everything
        # rather than an empty site.
        active = _resolve_mixer_tournament_id()
        team_query = select(Team.team_id, Team.name).order_by(Team.name)
        if active is not None:
            team_query = team_query.where(Team.tournament_id == active)
        teams = session.execute(team_query).all()
        if not teams:
            teams = session.execute(
                select(Team.team_id, Team.name).order_by(Team.name)
            ).all()

        result = []
        for team_id, name in teams:
            # The mixer-confirmed roster is authoritative and includes
            # players who haven't played a match yet (fresh substitutes) -
            # so count straight from Player rows when it exists.
            rows = session.execute(
                select(Player.account_id, Player.mmr)
                .where(Player.team_id == team_id, Player.roster_confirmed.is_(True))
            ).all()
            if not rows:
                # Unlinked team: fall back to everyone who played under it.
                # MatchPlayer has one row per match, so distinct() collapses
                # it back down to one row per player.
                rows = session.execute(
                    select(Player.account_id, Player.mmr)
                    .join(MatchPlayer, MatchPlayer.account_id == Player.account_id)
                    .where(MatchPlayer.team_id == team_id)
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


def _team_tournament_filter(tournament_id: int | None):
    """Restrict a team's matches to its OWN tournament. A steam team_id can
    be reused across tournaments that share a dotabuff league (e.g. B3SHA in
    #2 kept yuusha's #1 team_id), so without this a team page mixes both
    tournaments' games. None (unlinked team) means no scoping."""
    if tournament_id is not None:
        return Match.mixer_tournament_id == tournament_id
    return True


def _last_match_lineup(session: Session, team_id: int, tournament_id: int | None = None) -> dict | None:
    """Who actually played the team's most recent match. The roster cards
    only show mixer-confirmed players with at least one game, so a team can
    display fewer than five (fresh substitute who hasn't played yet, or an
    unlinked roster) - this fills that gap with the real last-game five."""
    row = session.execute(
        select(Match.match_id, Match.start_time, Match.radiant_team_id, Match.dire_team_id)
        .where(
            (Match.radiant_team_id == team_id) | (Match.dire_team_id == team_id),
            _team_tournament_filter(tournament_id),
        )
        .order_by(Match.start_time.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    match_id, start_time, radiant_id, dire_id = row

    lineup = session.execute(
        select(Player.account_id, Player.name)
        .join(MatchPlayer, MatchPlayer.account_id == Player.account_id)
        .where(MatchPlayer.match_id == match_id, MatchPlayer.team_id == team_id)
        .order_by(Player.name)
    ).all()
    if not lineup:
        return None

    opponent_id = dire_id if radiant_id == team_id else radiant_id
    opponent = session.get(Team, opponent_id) if opponent_id else None
    return {
        "match_id": match_id,
        "start_time": start_time,
        "opponent_name": (opponent.name if opponent and opponent.name
                          else (f"Team {opponent_id}" if opponent_id else None)),
        "players": [
            {"account_id": account_id, "name": name or f"account {account_id}"}
            for account_id, name in lineup
        ],
    }


def _recent_drafts(session: Session, team_id: int, tournament_id: int | None = None,
                   limit: int = 23) -> list[dict]:
    """Full draft (both teams' picks and bans, in actual draft order) for
    this team's last few matches - not just this team's own bans, since
    what the *opponent* banned against them is the more useful signal."""
    matches = session.execute(
        select(Match.match_id, Match.radiant_team_id, Match.dire_team_id, Match.radiant_win)
        .where(
            (Match.radiant_team_id == team_id) | (Match.dire_team_id == team_id),
            _team_tournament_filter(tournament_id),
        )
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
            .where(
                MatchPlayer.team_id == team_id, player_filter,
                _team_tournament_filter(team.tournament_id),
            )
            .group_by(Player.account_id, Hero.hero_id)
        ).all()

        recent_drafts = _recent_drafts(session, team_id, team.tournament_id)
        last_match_lineup = _last_match_lineup(session, team_id, team.tournament_id)
        mixer_uuid = team.mixer_uuid

        # Confirmed roster members with no matches yet (fresh substitutes)
        # have no MatchPlayer rows, so the inner-join query above misses
        # them - fetch the confirmed roster separately so they still get a
        # card (with an empty hero list) as soon as the substitution lands.
        confirmed_players = session.execute(
            select(Player.account_id, Player.name, Player.mmr, Player.preferred_roles)
            .where(Player.team_id == team_id, Player.roster_confirmed.is_(True))
        ).all()

    roles_by_account = {account_id: roles for account_id, _, _, roles in confirmed_players}

    players: dict[int, dict] = {}
    for account_id, name, mmr, hero_id, hero_name, games, decided_games, wins in rows:
        entry = players.setdefault(account_id, {
            "account_id": account_id,
            "name": name or f"account {account_id}",
            "mmr": mmr,
            "roles": roles_by_account.get(account_id),
            "heroes": [],
        })
        win_rate = round(100 * wins / decided_games) if decided_games else None
        entry["heroes"].append({
            "hero_id": hero_id, "name": hero_name, "games": games, "win_rate": win_rate,
        })

    for account_id, name, mmr, roles in confirmed_players:
        if account_id not in players:
            players[account_id] = {
                "account_id": account_id,
                "name": name or f"account {account_id}",
                "mmr": mmr,
                "roles": roles,
                "heroes": [],
            }

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
        "last_match_lineup": last_match_lineup,
    })


@app.get("/api/players/<int:account_id>")
def api_player_detail(account_id: int):
    """Personal player page: current team, mixer roles, hero pool and full
    match history across EVERY team they played for this tournament (the
    mixer format allows moving between teams via substitutions, and each
    MatchPlayer row remembers which team the game was actually played for)."""
    with Session(engine) as session:
        player = session.get(Player, account_id)
        if player is None:
            return jsonify({"error": "not found"}), 404

        current_team = session.get(Team, player.team_id) if player.team_id else None

        decided = case((Match.radiant_win.is_not(None), 1), else_=0)
        won = case((MatchPlayer.is_radiant == Match.radiant_win, 1), else_=0)
        hero_rows = session.execute(
            select(
                Match.mixer_tournament_id, Hero.localized_name,
                func.count(), func.sum(decided), func.sum(won),
            )
            .join(MatchPlayer, MatchPlayer.hero_id == Hero.hero_id)
            .join(Match, Match.match_id == MatchPlayer.match_id)
            .where(MatchPlayer.account_id == account_id)
            .group_by(Match.mixer_tournament_id, Hero.hero_id)
        ).all()

        match_rows = session.execute(
            select(
                Match.match_id, Match.start_time, Match.radiant_win,
                MatchPlayer.is_radiant, MatchPlayer.team_id,
                Match.radiant_team_id, Match.dire_team_id,
                Hero.localized_name, Match.league_id, Match.mixer_tournament_id,
            )
            .join(MatchPlayer, MatchPlayer.match_id == Match.match_id)
            .join(Hero, Hero.hero_id == MatchPlayer.hero_id)
            .where(MatchPlayer.account_id == account_id)
            .order_by(Match.start_time.desc())
        ).all()

        involved_ids = {tid for row in match_rows for tid in (row[4], row[5], row[6]) if tid}
        team_names = {
            t.team_id: t.name
            for t in session.execute(select(Team).where(Team.team_id.in_(involved_ids))).scalars()
        } if involved_ids else {}

        name = player.name
        mmr = player.mmr
        roles = player.preferred_roles

    # Resolve active tournament first so its live name is available to labels.
    active = _resolve_mixer_tournament_id()

    # Hero pool split per tournament (the two mixer cups run concurrently).
    pools_by_tid: dict[int | None, list] = {}
    for mixer_tid, hero_name, games, decided_games, wins in hero_rows:
        pools_by_tid.setdefault(mixer_tid, []).append({
            "name": hero_name,
            "games": games,
            "win_rate": round(100 * wins / decided_games) if decided_games else None,
        })
    hero_pools = []
    for tid in sorted(pools_by_tid, key=lambda t: (t != active, -(t or 0))):
        pool = sorted(pools_by_tid[tid], key=lambda h: -h["games"])
        hero_pools.append({
            "tournament_id": tid,
            "label": _tournament_label(tid, None),
            "heroes": pool,
        })

    matches = []
    for (match_id, start_time, radiant_win, is_radiant, played_for, r_id, d_id,
         hero, league_id, mixer_tid) in match_rows:
        opponent_id = d_id if played_for == r_id else r_id
        matches.append({
            "match_id": match_id,
            "start_time": start_time,
            "hero": hero,
            "team_id": played_for,
            "team_name": team_names.get(played_for) or (f"Team {played_for}" if played_for else "?"),
            "opponent_team_id": opponent_id,
            "opponent_name": team_names.get(opponent_id) or (f"Team {opponent_id}" if opponent_id else "?"),
            "won": (radiant_win == is_radiant) if radiant_win is not None else None,
            "league_id": league_id,
            "mixer_tournament_id": mixer_tid,
            "tournament_label": _tournament_label(mixer_tid, league_id),
        })

    return jsonify({
        "account_id": account_id,
        "name": name or f"account {account_id}",
        "mmr": mmr,
        "roles": roles,
        "current_team_id": current_team.team_id if current_team else None,
        "current_team_name": (current_team.name or f"Team {current_team.team_id}") if current_team else None,
        "hero_pools": hero_pools,
        "matches": matches,
    })


@app.get("/api/archive/player-heroes")
def api_archive_player_heroes():
    """Snapshot of every player's hero pool for the ACTIVE tournament, keyed
    by mixer tournament id. The backup workflow commits it to the data-backup
    branch as player-heroes-<id>.json - when the next tournament starts (new
    id, new file), the previous tournament's file survives there as reference
    data. Scoped by mixer_tournament_id, not league_id: consecutive mixer
    tournaments reuse the same dotabuff league."""
    from datetime import datetime, timezone

    active = _resolve_mixer_tournament_id()
    with Session(engine) as session:
        decided = case((Match.radiant_win.is_not(None), 1), else_=0)
        won = case((MatchPlayer.is_radiant == Match.radiant_win, 1), else_=0)
        rows = session.execute(
            select(
                Player.account_id, Player.name, Player.mmr, Player.preferred_roles,
                Hero.hero_id, Hero.localized_name,
                func.count(), func.sum(decided), func.sum(won),
            )
            .select_from(MatchPlayer)
            .join(Player, Player.account_id == MatchPlayer.account_id)
            .join(Hero, Hero.hero_id == MatchPlayer.hero_id)
            .join(Match, Match.match_id == MatchPlayer.match_id)
            .where(Match.mixer_tournament_id == active)
            .group_by(MatchPlayer.account_id, MatchPlayer.hero_id)
            .order_by(Player.account_id, Hero.hero_id)
        ).all()

    players: dict[int, dict] = {}
    for account_id, name, mmr, roles, hero_id, hero_name, games, decided_games, wins in rows:
        entry = players.setdefault(account_id, {
            "account_id": account_id,
            "name": name,
            "mmr": mmr,
            "roles": roles,
            "heroes": [],
        })
        entry["heroes"].append({
            "hero_id": hero_id, "name": hero_name,
            "games": games, "wins": wins or 0, "decided": decided_games or 0,
        })

    return jsonify({
        "mixer_tournament_id": active,
        "league_id": CURRENT_LEAGUE_ID,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "players": sorted(players.values(), key=lambda p: p["account_id"]),
    })


@app.get("/api/teams/<int:team_id>/analysis")
def api_team_analysis(team_id: int):
    with Session(engine) as session:
        team = session.get(Team, team_id)
        if team is None:
            return jsonify({"error": "not found"}), 404

        team_name = team.name or f"Team {team_id}"
        stats = compute_team_stats(session, team_id, team.tournament_id)
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
    active = _resolve_mixer_tournament_id()
    with Session(engine) as session:
        stats = compute_tournament_hero_stats(session, mixer_tournament_id=active)
    return jsonify(stats)


@app.get("/api/backup")
def api_backup():
    """Dump of the data that is either impossible or expensive to re-fetch.

    Impossible: substitution events (mixer-cup deletes its own history
    periodically, and queue positions were only ever known to us) and the
    substitute-queue snapshot.

    Expensive: picks/bans. OpenDota serves drafts one match at a time (~300
    calls, minutes of wall clock) while everything else is re-fetched in
    seconds, and the disk is wiped on every redeploy AND every cold start
    after an idle spin-down - so without this the draft backfill restarts from
    zero and never finishes, leaving the team analysis permanently empty.

    A GitHub Action commits this to the repo's data-backup branch, and the
    collector restores it after a wipe (see collect.restore_state_backup and
    collect.restore_draft_backup)."""
    with Session(engine) as session:
        events = session.execute(
            select(SubstitutionEvent).order_by(SubstitutionEvent.event_id)
        ).scalars().all()
        queued = session.execute(
            select(QueuedPlayer).order_by(QueuedPlayer.player_uuid)
        ).scalars().all()
        teams = session.execute(select(Team).order_by(Team.team_id)).scalars().all()
        all_players = session.execute(select(Player).order_by(Player.account_id)).scalars().all()
        draft_rows = session.execute(
            select(MatchDraftEntry.match_id, MatchDraftEntry.order_num,
                   MatchDraftEntry.hero_id, MatchDraftEntry.team_id,
                   MatchDraftEntry.is_pick)
            .order_by(MatchDraftEntry.match_id, MatchDraftEntry.order_num)
        ).all()

    # Grouped per match and written as bare tuples rather than objects with
    # repeated key names: ~7000 draft rows would otherwise dominate a file
    # that a GitHub Action rewrites on every run.
    drafts: dict[str, list] = {}
    for match_id, order_num, hero_id, team_id, is_pick in draft_rows:
        drafts.setdefault(str(match_id), []).append(
            [order_num, hero_id, team_id, 1 if is_pick else 0]
        )

    return jsonify({
        "teams": [
            {
                "team_id": t.team_id, "name": t.name,
                "mixer_uuid": t.mixer_uuid, "tournament_id": t.tournament_id,
            }
            for t in teams
        ],
        "players": [
            {
                "account_id": p.account_id, "name": p.name, "team_id": p.team_id,
                "roster_confirmed": p.roster_confirmed, "mmr": p.mmr,
                "preferred_roles": p.preferred_roles,
            }
            for p in all_players
        ],
        "substitution_events": [
            {
                "event_id": e.event_id, "team_id": e.team_id,
                "event_type": e.event_type, "nickname": e.nickname,
                "rating": e.rating, "queue_position": e.queue_position,
                "occurred_at": e.occurred_at,
            }
            for e in events
        ],
        "queued_players": [
            {
                "player_uuid": q.player_uuid, "nickname": q.nickname,
                "rating": q.rating, "queue_position": q.queue_position,
                "updated_at": q.updated_at,
            }
            for q in queued
        ],
        # match_id -> [[order, hero_id, team_id, is_pick], ...]
        "match_drafts": drafts,
        # Access-key -> device bindings, keyed by HMAC(key) so the public
        # backup branch never exposes the keys themselves. Lets device
        # bindings (the anti-sharing state) survive restarts and deploys.
        "access_bindings": {
            kh: sorted(devs) for kh, devs in _snapshot_bindings().items()
        },
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
