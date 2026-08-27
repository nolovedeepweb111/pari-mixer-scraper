from __future__ import annotations

import hashlib
import hmac
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, request, send_from_directory, session
from werkzeug.routing import BaseConverter
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from pari_mixer_scraper.analysis import (
    compute_player_signature_heroes, compute_team_stats, generate_coach_text,
)
from pari_mixer_scraper.collect import DEFAULT_LEAGUE_ID
from pari_mixer_scraper.sources import (
    PRIMARY_SOURCE, SOURCES, all_league_ids, source_for_tournament,
)
from pari_mixer_scraper.mixercup_client import MixerCupClient, pair_substitution_events
from pari_mixer_scraper.models import (
    Base, Hero, Match, MatchDraftEntry, MatchPlayer, Player, PlayerNote, QueuedPlayer,
    TeamTournamentName,
    SubstitutionEvent, Team,
    build_engine, configure_sqlite, ensure_schema,
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
# Лиги источников добавляются всегда, даже если LEAGUE_ID задан вручную: у
# WINLINE-супермиксера своя лига, и без неё сборщик просто не пошёл бы за его
# матчами - причём молча. Требовать правки env на сервере ради этого не стоит.
for _lid in all_league_ids():
    if _lid not in LEAGUE_IDS:
        LEAGUE_IDS.append(_lid)
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
# A database file promoted by an older release can predate a column added
# since; the app reads those columns on every page.
ensure_schema(engine)

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
# before the owner sets it up) - EXCEPT in PUBLIC_ARCHIVE mode, see below.
OPS_TOKEN = os.environ.get("OPS_TOKEN", "")

# Sell access to the RUNNING cup only: finished cups are readable by anyone,
# the active one needs a key. Off by default - turning this on mid-tournament
# would hand out the very thing people paid for, so it has to be an explicit
# decision (set PUBLIC_ARCHIVE=1 when the next cup starts).
PUBLIC_ARCHIVE = os.environ.get("PUBLIC_ARCHIVE") == "1"

# What a visitor without a key is told, in one place: the login page and the
# in-app lock panel both show it.
ACCESS_OFFER = {
    "price": "5 USD",
    "recipient": "zharok.pcash",
    "discord": "nldw111",
    "telegram": "@VaxpEe",
}

app.secret_key = hashlib.sha256(("session:" + AUTH_SECRET).encode()).digest()
app.permanent_session_lifetime = timedelta(days=60)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # This cookie IS the paid access, so on a site served over TLS it must not
    # be sent in the clear. Off by default so local development over plain
    # http still logs in; the deploy sets it (see deploy/env.example).
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE") == "1",
)


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

if PUBLIC_ARCHIVE and not OPS_TOKEN:
    # Loud on purpose: /api/collect and /api/backup are refused in this state,
    # so the GitHub Actions workflow stops refreshing data and stops committing
    # backups until OPS_TOKEN is set on both sides.
    print(
        "WARNING: PUBLIC_ARCHIVE is on but OPS_TOKEN is empty - operational "
        "endpoints (/api/collect, /api/backup, /api/archive) are refused, so "
        "scheduled collection and state backups will NOT run. Set OPS_TOKEN in "
        "the service environment and as the OPS_TOKEN GitHub secret.",
        flush=True,
    )

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
  <div class="access">__ACCESS_OFFER__</div>
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


# One wording for the offer, shared by the login page and the in-app lock
# panel - the numbers and contacts live in ACCESS_OFFER only.
_ACCESS_OFFER_HTML = (
    f"Для получения доступа к сайту отправьте <b>{ACCESS_OFFER['price']}</b> пользователю "
    f"<b>{ACCESS_OFFER['recipient']}</b> и напишите в дискорде <b>{ACCESS_OFFER['discord']}</b> "
    f"или в телеграмме <b>{ACCESS_OFFER['telegram']}</b>"
)
_LOGIN_HTML = _LOGIN_HTML.replace("__ACCESS_OFFER__", _ACCESS_OFFER_HTML)


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


def _viewer_has_key() -> bool:
    """True when this request may see everything - either the site is open to
    all, or the visitor holds a valid session."""
    return not AUTH_ENABLED or _session_ok()


def _may_see_tournament(tournament_id: int | None) -> bool:
    """Whether this visitor may see data from that cup.

    In PUBLIC_ARCHIVE mode a visitor without a key gets the finished cups and
    never the running one. Anything whose cup we can't establish counts as
    off-limits: a match with no tournament linked yet, or - the case that
    matters - an active tournament we failed to resolve because mixer-cup is
    unreachable. Guessing "probably fine" there would give the current cup
    away for free every time their API hiccups."""
    if _viewer_has_key():
        return True
    if not PUBLIC_ARCHIVE:
        return False
    if tournament_id is None:
        return False
    active_ids = _active_tournament_ids()
    if not active_ids:
        return False
    return tournament_id not in active_ids


def _may_see_hero_pools() -> bool:
    """Whether this visitor gets per-player hero pools - the hero, how many
    games on it, the win rate - anywhere on the site.

    Gated even on FINISHED cups, unlike everything else in the archive. The
    individual matches stay public on purpose: every one of them is on
    Dotabuff, which our own match page links to, so hiding who played what in
    one game would be theatre. Counting those games up per player is the work
    this site actually does, and that is what's being sold."""
    return _viewer_has_key()


def _deny_tournament():
    """Uniform refusal the frontend turns into the lock panel. 403, not 401:
    401 means "your session expired, log in again" and the frontend bounces to
    /login on it, which would be wrong for a visitor who never had a key."""
    return jsonify({
        "error": "locked",
        "message": "Данные текущего турнира доступны по ключу.",
    }), 403


@app.before_request
def _gate():
    if not AUTH_ENABLED:
        return
    p = request.path
    if p == "/login" or p.startswith("/api/auth/") or p == "/favicon.ico":
        return
    if _is_ops_path(p):
        # Deliberately NOT satisfied by a visitor's session: these are the
        # owner's endpoints, and a paying customer is not an operator -
        # /api/backup would hand them every team, player and draft in one file.
        if OPS_TOKEN:
            tok = request.headers.get("X-Ops-Token") or request.args.get("ops_token")
            if tok and hmac.compare_digest(tok, OPS_TOKEN):
                return
            abort(403)
        # No token configured: these stayed open so nothing broke before the
        # owner set one up. That was only ever safe because the whole site
        # needed a key - with a public archive it would publish the full dump
        # (including the access-key hashes) to anyone, so refuse instead.
        if PUBLIC_ARCHIVE:
            abort(403)
        return
    if _session_ok():
        return
    # Public-archive mode: let the request through and let the endpoint decide
    # per tournament (see _may_see_tournament). Pages are always served - a
    # shared link should show the lock panel, not a login redirect.
    if PUBLIC_ARCHIVE:
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
    return jsonify({
        "enabled": AUTH_ENABLED,
        "authenticated": _viewer_has_key(),
        "public_archive": PUBLIC_ARCHIVE,
        # Shown on the lock panel, so the offer lives in one place.
        "offer": ACCESS_OFFER,
    })


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


# Every in-app address is served by the same shell; the frontend router reads
# location.pathname and renders from there (see static/app.js). A converter
# rather than a bare <slug> so these rules can never swallow /app.js and
# /style.css, which the static handler serves from the same root path.
class _TournamentSlugConverter(BaseConverter):
    # Приставки собираются из реестра источников: /mixercup3, /winline1. Длинные
    # первыми, иначе "cup" отъел бы начало "mixercup" при разборе адреса.
    regex = r"(?:" + "|".join(
        re.escape(x) for x in sorted(
            {src.slug_prefix for src in SOURCES} | {"cup"}, key=len, reverse=True
        )
    ) + r")\d+"


app.url_map.converters["tslug"] = _TournamentSlugConverter


@app.get("/<tslug:slug>")
@app.get("/<tslug:slug>/players")
@app.get("/<tslug:slug>/subs")
@app.get("/<tslug:slug>/team/<int:team_id>")
@app.get("/<tslug:slug>/team/<int:team_id>/<any(composition, analysis, subs):section>")
@app.get("/players")
@app.get("/player/<int:account_id>")
@app.get("/match/<int:match_id>")
def spa_route(**_kwargs):
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/tournaments")
def api_tournaments():
    """The cup switcher's menu: every tournament with an address, newest
    first, and which one is live."""
    active = _resolve_mixer_tournament_id()
    active_ids = _active_tournament_ids()
    with Session(engine) as session:
        ids = _known_tournament_ids(session)
        played = {
            t for (t,) in session.execute(
                select(Match.mixer_tournament_id)
                .where(Match.mixer_tournament_id.is_not(None)).distinct()
            )
        }
    return jsonify({
        "active_id": active,
        "active_ids": sorted(active_ids),
        "active_slug": _tournament_slug(active) if active is not None else None,
        "tournaments": [
            {
                "id": tournament_id,
                "slug": _tournament_slug(tournament_id),
                "label": _tournament_label(tournament_id, None),
                "is_active": tournament_id in active_ids,
                "has_matches": tournament_id in played,
                # Lets the switcher mark what this visitor can't open yet.
                "locked": not _may_see_tournament(tournament_id),
            }
            for tournament_id in ids
            # Only cups this site can actually show. mixer-cup's tournament
            # list reaches back to their own test events from before we
            # collected anything - those have an id and a name here and
            # nothing else, and they are not this cup series.
            if tournament_id in played or tournament_id in active_ids
        ],
    })


# По клиенту на источник. Клиент сам сдвигает номера турниров, так что
# наружу все номера уже глобальные (см. sources.py).
_mixer_clients = {
    src.key: MixerCupClient(base_url=src.base_url, id_offset=src.id_offset)
    for src in SOURCES
}
_mixer_client = _mixer_clients[PRIMARY_SOURCE.key]

# mixer tournament id -> display name, for the tournament dividers on player
# pages. Overridable via env MIXER_TOURNAMENT_LABELS="26:PARI Mixer Cup #1;27:...".
# These WIN over the name mixer-cup reports: their numbering is their own
# (cup 28 is "Mixer Cup #5" upstream) and would read as a different series
# next to "PARI Mixer Cup #1/#2" in the same match history.
MIXER_TOURNAMENT_LABELS: dict[int, str] = {
    26: "PARI Mixer Cup #1",
    27: "PARI Mixer Cup #2",
    # Cup 28 was labelled "PARI Mixer Cup #3" here for a while, which turned
    # out to be our invention: mixer-cup calls it "Mixer Cup #5" and gave the
    # PARI-#3 name to the NEXT cup instead. Two cups then showed the same name
    # on the site. Their branding wins - it is what players see on mixer-cup.
    28: "Mixer Cup #5",
    29: "PARI Mixer Cup #3",
    30: "Pari Mixer Cup #4",
    # Супермиксер WINLINE - второй источник (см. sources.py). Метка задана
    # явно, хотя название пришло бы и из их API: без неё адрес /winline1
    # существует только после того, как кэш имён сходит в сеть, то есть
    # первый после перезапуска гость получил бы 404 на живой кубок.
    20002: "WINLINE Super Mixer #1",
}
for _pair in os.environ.get("MIXER_TOURNAMENT_LABELS", "").replace(",", ";").split(";"):
    if ":" in _pair:
        _tid, _label = _pair.split(":", 1)
        try:
            MIXER_TOURNAMENT_LABELS[int(_tid.strip())] = _label.strip()
        except ValueError:
            pass


# The active tournament is re-resolved periodically rather than once per
# process. mixer-cup flips it the moment one cup ends and the next opens,
# while a gunicorn worker here can outlive that by days - the keep-alive ping
# stops the instance from ever spinning down - and a stale value keeps the
# finished cup's teams, leaderboard and hero stats on a site that should
# already have moved on.
_TOURNAMENT_TTL_SECONDS = int(os.environ.get("TOURNAMENT_CACHE_TTL_SECONDS", "600"))
# Between cups mixer-cup reports NO active tournament (the next one sits in
# REDUCTION until it starts). Re-asking on every request would mean a blocking
# GraphQL call per request on a single-worker instance, so failures are cached
# briefly too - and the last known id is kept rather than dropped, because a
# None here silently widens every tournament-scoped endpoint to "all cups".
_TOURNAMENT_RETRY_SECONDS = int(os.environ.get("TOURNAMENT_RETRY_SECONDS", "60"))

_ENV_TOURNAMENT_ID: int | None = None
if os.environ.get("MIXER_TOURNAMENT_ID"):
    try:
        _ENV_TOURNAMENT_ID = int(os.environ["MIXER_TOURNAMENT_ID"])
    except ValueError:
        pass

_tournament_lock = threading.Lock()
_mixer_tournament_id_cache: int | None = None
# Идущие прямо сейчас кубки - по одному на источник. Раньше активный кубок был
# ровно один, и платный доступ, боковая колонка и признак "исторический"
# сравнивались с ним напрямую. С появлением второго источника таких кубков
# стало два одновременно, и сравнение с одним номером означало бы, что живой
# супермиксер считается архивным: составы бы не показывались, а сам кубок
# уехал бы в открытую часть.
_mixer_active_ids: set[int] = set()
# Последний известный активный кубок КАЖДОГО источника. Обновляется только
# тем источником, который ответил: если один из сайтов недоступен, его кубок
# обязан остаться живым (то есть закрытым за ключом). Иначе сбой чужого API
# раздавал бы идущий кубок бесплатно - ровно то, чего избегает и одиночный
# кэш ниже.
_mixer_active_by_source: dict[str, int] = {}
_mixer_tournament_names: dict[int, str] = {}
_tournament_cache_expires_at = 0.0


def _newest_known_tournament() -> int | None:
    """Best local guess at which cup is the running one, used only when
    mixer-cup can't tell us: the highest tournament id our own data mentions.
    Teams count as well as matches, because a cup has rosters before it has
    games. None only when the database is empty."""
    try:
        with Session(engine) as session:
            ids = [
                t for (t,) in session.execute(
                    select(Match.mixer_tournament_id)
                    .where(Match.mixer_tournament_id.is_not(None)).distinct()
                )
            ]
            ids += [
                t for (t,) in session.execute(
                    select(Team.tournament_id)
                    .where(Team.tournament_id.is_not(None)).distinct()
                )
            ]
    except Exception:
        return None
    return max(ids) if ids else None


def _refresh_tournament_cache() -> None:
    """One GraphQL call that answers both questions we have: which tournament
    is active, and what every tournament is called (the list carries `status`,
    so it doubles as activeTournament). Caller holds _tournament_lock."""
    global _mixer_tournament_id_cache, _tournament_cache_expires_at, _mixer_active_ids

    for src in SOURCES:
        client = _mixer_clients[src.key]
        found = None
        try:
            for t in client.list_tournaments():
                if t.get("name"):
                    _mixer_tournament_names[t["id"]] = t["name"]
                if found is None and t.get("status") == "ACTIVE":
                    found = t["id"]
        except Exception:
            pass
        if found is None:
            # The list didn't say (API shape changed, or the call failed) - ask
            # the dedicated endpoint before giving up on this refresh.
            try:
                active = client.get_active_tournament()
            except Exception:
                active = None
            if active:
                found = active["id"]
                if active.get("name"):
                    _mixer_tournament_names[found] = active["name"]
        if found is not None:
            _mixer_active_by_source[src.key] = found

    _mixer_active_ids = set(_mixer_active_by_source.values())
    # Основной источник задаёт кубок по умолчанию - тот, что открывается на "/".
    # Если про него неизвестно ничего, берём любой живой, чтобы сайт не
    # остался без текущего кубка вовсе.
    active_id = _mixer_active_by_source.get(PRIMARY_SOURCE.key)
    if active_id is None and _mixer_active_by_source:
        active_id = max(_mixer_active_by_source.values())

    if active_id is not None:
        _mixer_tournament_id_cache = active_id
        _tournament_cache_expires_at = time.monotonic() + _TOURNAMENT_TTL_SECONDS
    else:
        # Nothing active (we're between cups) or mixer-cup is unreachable.
        # Normally we keep serving whatever cup we last knew about, but after a
        # restart there is no such thing - and with no active cup at all every
        # tournament counted as "can't tell", which locked the public archive
        # along with the running cup and took the site down for everyone
        # without a key. Losing contact with mixer-cup must not make a finished
        # cup secret, so fall back to the newest cup our own data knows about:
        # that one stays closed, the rest of the archive stays open.
        if _mixer_tournament_id_cache is None:
            _mixer_tournament_id_cache = _newest_known_tournament()
        if not _mixer_active_ids and _mixer_tournament_id_cache is not None:
            _mixer_active_ids = {_mixer_tournament_id_cache}
        _tournament_cache_expires_at = time.monotonic() + _TOURNAMENT_RETRY_SECONDS


def _resolve_mixer_tournament_id() -> int | None:
    """Кубок по умолчанию: активный кубок ОСНОВНОГО источника. Это то, что
    открывается на "/" и подставляется, когда адрес кубка не указан."""
    with _tournament_lock:
        if time.monotonic() >= _tournament_cache_expires_at:
            _refresh_tournament_cache()
        # The env pin still lets the refresh above run: it costs one call per
        # TTL and keeps the tournament NAMES current for the history dividers.
        if _ENV_TOURNAMENT_ID is not None:
            return _ENV_TOURNAMENT_ID
        return _mixer_tournament_id_cache


def _active_tournament_ids() -> set[int]:
    """Все идущие сейчас кубки - по одному на источник. Это они закрыты за
    ключом и это они показываются как живые, а не как архив."""
    with _tournament_lock:
        if time.monotonic() >= _tournament_cache_expires_at:
            _refresh_tournament_cache()
        ids = set(_mixer_active_ids)
    if _ENV_TOURNAMENT_ID is not None:
        ids.add(_ENV_TOURNAMENT_ID)
    return ids


# Valve's own hero icons. The slug is the hero's INTERNAL name minus the
# prefix, which is not the display name - several are historical (Shadow Fiend
# is "nevermore", Timbersaw "shredder", Zeus "zuus"), so it must come from
# Hero.name rather than be derived from what we show. All 127 verified to
# return an image. ~5KB each, and reachable from Russia - unlike the Google
# Fonts CDN, which is blocked and must stay out of this site.
_HERO_ICON_BASE = "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes/icons"


def _hero_icon_url(hero_key: str | None) -> str | None:
    if not hero_key:
        return None
    return f"{_HERO_ICON_BASE}/{hero_key.replace('npc_dota_hero_', '')}.png"


def _tournament_label(mixer_tournament_id: int | None, league_id: int | None) -> str:
    """Best display name for the tournament a match belongs to: our own label
    map (which overrides mixer-cup's parallel numbering), then the live name
    mixer-cup reports, then the league-id map, then a generic fallback."""
    if mixer_tournament_id is not None:
        if mixer_tournament_id in MIXER_TOURNAMENT_LABELS:
            return MIXER_TOURNAMENT_LABELS[mixer_tournament_id]
        if mixer_tournament_id in _mixer_tournament_names:
            return _mixer_tournament_names[mixer_tournament_id]
        return f"Турнир {mixer_tournament_id}"
    if league_id in LEAGUE_LABELS:
        return LEAGUE_LABELS[league_id]
    return "Прочие матчи"


# --- Tournament URLs -------------------------------------------------------
# Every tournament gets an address of its own (/mixercup2), so a cup, a roster
# or a match can be linked to directly instead of existing only as in-page
# state. The slug is derived from the cup's number in its label, which keeps
# one source of truth: label a new cup "PARI Mixer Cup #4" and /mixercup4
# starts working. Override with env MIXER_TOURNAMENT_SLUGS="29:kubok4".
MIXER_TOURNAMENT_SLUGS: dict[int, str] = {}
for _pair in os.environ.get("MIXER_TOURNAMENT_SLUGS", "").replace(",", ";").split(";"):
    if ":" in _pair:
        _tid, _slug = _pair.split(":", 1)
        try:
            MIXER_TOURNAMENT_SLUGS[int(_tid.strip())] = _slug.strip().lower()
        except ValueError:
            pass

_LABEL_NUMBER_RE = re.compile(r"#\s*(\d+)")


def _tournament_slug(tournament_id: int) -> str:
    if tournament_id in MIXER_TOURNAMENT_SLUGS:
        return MIXER_TOURNAMENT_SLUGS[tournament_id]
    source = source_for_tournament(tournament_id)
    label = MIXER_TOURNAMENT_LABELS.get(tournament_id)
    if label is None and source is not None and source is not PRIMARY_SOURCE:
        # У неосновного источника своя серия и своя нумерация, поэтому его
        # живое название годится как есть: "WINLINE Super Mixer #1" -> /winline1.
        # Для основного так делать нельзя: там "Mixer Cup #1" и "PARI Mixer
        # Cup #1" - РАЗНЫЕ кубки, и адрес у них совпал бы, а разрешался бы
        # всегда в пользу того, что новее. Отсюда и карта меток выше.
        label = _mixer_tournament_names.get(tournament_id)
    number = _LABEL_NUMBER_RE.search(label) if label else None
    prefix = (source or PRIMARY_SOURCE).slug_prefix
    # A cup we have no label for still needs a stable address, hence the id.
    return f"{prefix}{number.group(1)}" if number else f"cup{tournament_id}"


def _known_tournament_ids(session: Session | None = None) -> list[int]:
    """Every tournament the site can show, newest first: the ones we have
    labels or live names for, plus any our own matches mention."""
    ids = set(MIXER_TOURNAMENT_LABELS) | set(MIXER_TOURNAMENT_SLUGS) | set(_mixer_tournament_names)
    if session is not None:
        ids.update(
            t for (t,) in session.execute(
                select(Match.mixer_tournament_id)
                .where(Match.mixer_tournament_id.is_not(None)).distinct()
            )
        )
    return sorted(ids, reverse=True)


def _tournament_from_slug(slug: str) -> int | None:
    slug = (slug or "").lower()
    for tournament_id in _known_tournament_ids():
        if _tournament_slug(tournament_id) == slug:
            return tournament_id
    # Not in the label map - it may only exist in our own match rows.
    with Session(engine) as session:
        for tournament_id in _known_tournament_ids(session):
            if _tournament_slug(tournament_id) == slug:
                return tournament_id
    return None


def _requested_scope() -> int | None:
    """?tournament=<id> on a listing endpoint, defaulting to the active cup.
    Lets the archive pages (/mixercup1 and friends) reuse the same endpoints
    the live cup uses."""
    raw = request.args.get("tournament")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return _resolve_mixer_tournament_id()


def _get_next_opponent(mixer_uuid: str, tournament_id: int | None = None) -> dict | None:
    """Следующая игра команды по сетке. Турнир передаётся явно: сетку нужно
    спрашивать у того сайта, которому кубок принадлежит, - у чужого этого
    номера либо нет, либо под ним лежит посторонний турнир."""
    if tournament_id is None:
        tournament_id = _resolve_mixer_tournament_id()
    if tournament_id is None:
        return None
    source = source_for_tournament(tournament_id) or PRIMARY_SOURCE
    client = _mixer_clients.get(source.key, _mixer_client)
    try:
        opponent = client.get_next_opponent(tournament_id, mixer_uuid)
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


def _get_substitution_history(session: Session, team_id: int,
                              tournament_id: int | None = None) -> list[dict]:
    """Reads from our own SubstitutionEvent table (synced during collect(),
    see sync_substitution_history) rather than querying mixer-cup.gg live -
    their own substitution history has been observed to disappear
    periodically, so this is the durable copy.

    tournament_id keeps a recycled team id from showing the PREVIOUS cup's
    swaps in the moments between a new cup opening and the collector's next
    pass clearing them out. Events not yet tagged with a tournament (legacy
    rows, tagged on the collector's next run) are let through rather than
    hidden."""
    scope = [SubstitutionEvent.team_id == team_id]
    if tournament_id is not None:
        scope.append(or_(
            SubstitutionEvent.tournament_id == tournament_id,
            SubstitutionEvent.tournament_id.is_(None),
        ))
    events = session.execute(
        select(SubstitutionEvent)
        .where(*scope)
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


def _team_name_in_tournament(session: Session, team_id: int | None,
                             tournament_id: int | None) -> str | None:
    """What this Steam team_id was called in that tournament, falling back to
    its current name. Steam ids are recycled between cups, so Teams.name is
    the wrong label for anything shown against an older cup."""
    if team_id is None:
        return None
    if tournament_id is not None:
        row = session.get(TeamTournamentName, (team_id, tournament_id))
        if row is not None:
            return row.name
    team = session.get(Team, team_id)
    return team.name if team and team.name else None


def _requested_tournament(team: Team) -> tuple[int | None, bool]:
    """Which incarnation of this team the caller asked for: ?tournament=<id>
    from a past cup's match, or the cup that currently owns the team.

    Returns (tournament_id, is_historical). A Steam team_id is reused cup after
    cup, so without this a link from a #2 match opens the CURRENT squad playing
    under that id - different players, different games, different name.

    "Historical" is decided against the ACTIVE cup, not against the cup that
    owns the Team row. Those differ for a team the new cup did NOT reuse: its
    row still says the old cup, so asking for the old cup looked like asking
    for the current one, and the page was built from the confirmed roster -
    which by then is one leftover player who never moved to the new cup, or
    none. That returned 404 for every archived team the new cup didn't reuse,
    while the sidebar (built from matches) listed them all."""
    raw = request.args.get("tournament")
    requested = team.tournament_id
    if raw:
        try:
            requested = int(raw)
        except ValueError:
            pass
    active_ids = _active_tournament_ids()
    # requested None means an unlinked team asked for with no scope: leave it
    # unscoped and on the roster path, exactly as before.
    # Сравнение идёт со ВСЕМИ идущими кубками: пока живы и PARI, и супермиксер,
    # сравнение с одним кубком объявляло бы второй архивным - без составов и
    # без MMR, хотя он играется прямо сейчас.
    historical = bool(active_ids) and requested is not None and requested not in active_ids
    return requested, historical


# Пороги для блока «Лучшие герои игроков» на вкладке аналитики.
PLAYER_HERO_MIN_GAMES = int(os.environ.get("PLAYER_HERO_MIN_GAMES", "4"))
PLAYER_HERO_MIN_WIN_RATE = int(os.environ.get("PLAYER_HERO_MIN_WIN_RATE", "60"))


def _team_player_names(session: Session, team_id: int, tournament_id: int | None,
                       historical: bool) -> dict[int, str]:
    """Кто считается составом этой команды: подтверждённый ростер для текущего
    кубка (включая только что заведённых замен) и все, кто реально играл, — для
    прошедшего или когда ростера нет. То же правило, что на странице состава."""
    names: dict[int, str] = {}
    if not historical:
        for account_id, name in session.execute(
            select(Player.account_id, Player.name)
            .where(Player.team_id == team_id, Player.roster_confirmed.is_(True))
        ):
            names[account_id] = name or f"account {account_id}"
    if not names:
        for account_id, name in session.execute(
            select(Player.account_id, Player.name)
            .join(MatchPlayer, MatchPlayer.account_id == Player.account_id)
            .join(Match, Match.match_id == MatchPlayer.match_id)
            .where(MatchPlayer.team_id == team_id, _team_tournament_filter(tournament_id))
            .distinct()
        ):
            names[account_id] = name or f"account {account_id}"
    return names


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


def _past_cup_teams(session: Session, tournament_id: int) -> list[dict]:
    """Sidebar for a FINISHED cup: the teams that actually played in it, under
    the names they carried then. Can't come from the Team rows - those are
    owned by whichever cup last reused their Steam ids - so it's built from
    that cup's own matches."""
    counts = dict(session.execute(
        select(MatchPlayer.team_id, func.count(func.distinct(MatchPlayer.account_id)))
        .join(Match, Match.match_id == MatchPlayer.match_id)
        .where(Match.mixer_tournament_id == tournament_id)
        .group_by(MatchPlayer.team_id)
    ).all())
    names = dict(session.execute(
        select(TeamTournamentName.team_id, TeamTournamentName.name)
        .where(TeamTournamentName.tournament_id == tournament_id)
    ).all())
    teams = [
        {
            "team_id": team_id,
            "name": names.get(team_id) or f"Team {team_id}",
            "player_count": player_count,
            # No total: a finished cup's list includes every substitute who
            # passed through, and their ratings are today's (see api_team_detail).
            "total_mmr": None,
        }
        for team_id, player_count in counts.items()
        if team_id is not None and player_count > 1
    ]
    teams.sort(key=lambda t: t["name"].lower())
    return teams


@app.get("/api/teams")
def api_teams():
    with Session(engine) as session:
        # Sidebar shows one tournament's teams: the ACTIVE one by default, or
        # whichever cup's page the visitor is on (?tournament=). Earlier
        # tournaments' teams stay in the DB and are listed on their own page.
        # If the active tournament can't be resolved (mixer API down), show
        # everything rather than an empty site.
        active = _resolve_mixer_tournament_id()
        scope = _requested_scope()
        if not _may_see_tournament(scope):
            return _deny_tournament()
        if scope is not None and scope not in _active_tournament_ids():
            return jsonify(_past_cup_teams(session, scope))

        # Живых кубков теперь может быть несколько, поэтому колонка показывает
        # команды ЗАПРОШЕННОГО кубка, а не всегда основного.
        live_scope = scope if scope is not None else active
        team_query = select(Team.team_id, Team.name).order_by(Team.name)
        if live_scope is not None:
            team_query = team_query.where(Team.tournament_id == live_scope)
        teams = session.execute(team_query).all()
        if not teams and request.args.get("tournament") is None:
            # Запасной путь только для страницы по умолчанию: там пустой список
            # означал бы, что активный кубок не определился, и показать вообще
            # всё лучше, чем пустой сайт. Если же адрес кубка назван явно
            # (/winline1), список обязан остаться пустым: иначе туда попадают
            # команды ЧУЖОГО кубка - ровно это и вышло, когда супермиксер
            # появился на сайте раньше, чем сборщик успел принести его данные.
            # Команды при этом открывались с 404: в этом кубке они не играли.
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

        tournament_id, historical = _requested_tournament(team)
        if not _may_see_tournament(tournament_id):
            return _deny_tournament()
        # A past cup's page must be built from who actually PLAYED for the team
        # then: the mixer-confirmed roster is the current one, which for a
        # recycled team id belongs to an entirely different squad.
        player_filter = (MatchPlayer.team_id == team_id) if historical \
            else _roster_filter(session, team_id)
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
                _team_tournament_filter(tournament_id),
            )
            .group_by(Player.account_id, Hero.hero_id)
        ).all()

        recent_drafts = _recent_drafts(session, team_id, tournament_id)
        last_match_lineup = _last_match_lineup(session, team_id, tournament_id)
        mixer_uuid = team.mixer_uuid
        team_name = _team_name_in_tournament(session, team_id, tournament_id)

        # Confirmed roster members with no matches yet (fresh substitutes)
        # have no MatchPlayer rows, so the inner-join query above misses
        # them - fetch the confirmed roster separately so they still get a
        # card (with an empty hero list) as soon as the substitution lands.
        # Not for a past cup: that roster is today's squad, not that cup's.
        confirmed_players = [] if historical else session.execute(
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

    # A past cup's page lists everyone who played for the team over that whole
    # cup - substitutes included - so summing their (current!) ratings would
    # produce a "team MMR" no lineup ever had. Only the live squad gets one.
    total_mmr = None if historical else (
        sum(p["mmr"] for p in players.values() if p["mmr"] is not None) or None
    )
    # Same reason for the order: with a dozen cards, the five who actually
    # played the cup should come first, not whoever is alphabetically first.
    ordered_players = sorted(
        players.values(),
        key=(lambda p: (-sum(h["games"] for h in p["heroes"]), p["name"])) if historical
        else (lambda p: p["name"]),
    )
    # After the ordering, which needs the game counts.
    pools_locked = not _may_see_hero_pools()
    if pools_locked:
        for entry in ordered_players:
            entry["heroes"] = []
    # A finished cup has no "next opponent" - that lookup is about the live
    # bracket, which only the current squad is in.
    next_opponent = _get_next_opponent(mixer_uuid, tournament_id) if mixer_uuid and not historical else None

    return jsonify({
        "team_id": team_id,
        "name": team_name or f"Team {team_id}",
        "tournament_id": tournament_id,
        "tournament_label": _tournament_label(tournament_id, None) if tournament_id else None,
        "is_historical": historical,
        "hero_pools_locked": pools_locked,
        "total_mmr": total_mmr,
        "players": ordered_players,
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
                Match.mixer_tournament_id, Hero.localized_name, Hero.name,
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
        # A player's history spans both concurrent cups, and mixer-cup reuses
        # the same Steam team registrations in each - so Teams.name (always the
        # ACTIVE cup's name) mislabels the older cup's matches. Resolve each
        # match's teams by the tournament that match belongs to.
        names_by_tour = {
            (r.team_id, r.tournament_id): r.name
            for r in session.execute(
                select(TeamTournamentName).where(TeamTournamentName.team_id.in_(involved_ids))
            ).scalars()
        } if involved_ids else {}

        name = player.name
        mmr = player.mmr
        roles = player.preferred_roles

    # Resolve active tournament first so its live name is available to labels.
    active = _resolve_mixer_tournament_id()

    # Hero pool split per tournament (the two mixer cups run concurrently).
    pools_by_tid: dict[int | None, list] = {}
    for mixer_tid, hero_name, hero_key, games, decided_games, wins in hero_rows:
        pools_by_tid.setdefault(mixer_tid, []).append({
            "name": hero_name,
            "icon": _hero_icon_url(hero_key),
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

        def name_in_this_match(team_id):
            # The name this team went by in THIS match's tournament; only fall
            # back to Teams.name when that cup never listed them.
            if team_id is None:
                return "?"
            return (names_by_tour.get((team_id, mixer_tid))
                    or team_names.get(team_id)
                    or f"Team {team_id}")

        matches.append({
            "match_id": match_id,
            "start_time": start_time,
            "hero": hero,
            "team_id": played_for,
            "team_name": name_in_this_match(played_for),
            "opponent_team_id": opponent_id,
            "opponent_name": name_in_this_match(opponent_id),
            "won": (radiant_win == is_radiant) if radiant_win is not None else None,
            "league_id": league_id,
            "mixer_tournament_id": mixer_tid,
            "tournament_label": _tournament_label(mixer_tid, league_id),
        })

    # A player's page spans every cup, so it can't simply be allowed or
    # refused: without a key the archive stays and the running cup goes,
    # including which team they are on RIGHT NOW - the current roster is the
    # single most valuable thing here.
    visible_pools = [p for p in hero_pools if _may_see_tournament(p["tournament_id"])]
    visible_matches = [m for m in matches if _may_see_tournament(m["mixer_tournament_id"])]
    show_current_team = _may_see_tournament(active)
    pools_locked = not _may_see_hero_pools()
    if pools_locked:
        # The match list stays (each game is public on Dotabuff anyway); the
        # counted-up pool is what's held back.
        visible_pools = []

    return jsonify({
        "account_id": account_id,
        "name": name or f"account {account_id}",
        "mmr": mmr,
        "roles": roles,
        "current_team_id": current_team.team_id if current_team and show_current_team else None,
        "current_team_name": (current_team.name or f"Team {current_team.team_id}")
                             if current_team and show_current_team else None,
        "hero_pools": visible_pools,
        "matches": visible_matches,
        # So the page can say why it looks short rather than just looking wrong -
        # and in particular not claim the player is on no team when the truth is
        # that we are withholding which one.
        "locked_tournaments": len(hero_pools) - len(visible_pools) if not pools_locked else 0,
        "current_team_locked": bool(current_team) and not show_current_team,
        "hero_pools_locked": pools_locked,
    })


# --- Player notes ----------------------------------------------------------
# Scouting notes anybody with a key can leave on a player, visible to everyone
# else who has one - hence the author field: there are no accounts here, only
# shared keys, so a note says who wrote it by hand. Reading and writing both
# need a key even in PUBLIC_ARCHIVE mode.
NOTE_TEXT_LIMIT = 2000
NOTE_AUTHOR_LIMIT = 40
NOTES_PER_PLAYER_LIMIT = 200


def _viewer_key_hash() -> str | None:
    """Hash of the access key this request came with. None when no keys are
    configured at all (a fully open site), which is also why deleting is only
    offered when we can tell notes apart by author."""
    return session.get("kh")


def _notes_for(db: Session, account_id: int) -> list[dict]:
    mine = _viewer_key_hash()
    notes = db.execute(
        select(PlayerNote)
        .where(PlayerNote.account_id == account_id)
        .order_by(PlayerNote.created_at.desc())
    ).scalars().all()
    return [
        {
            "note_id": n.note_id,
            "author": n.author,
            "text": n.text,
            "created_at": n.created_at,
            # Only the key that wrote a note may remove it.
            "can_delete": bool(mine) and n.author_key_hash == mine,
        }
        for n in notes
    ]


@app.get("/api/players/<int:account_id>/notes")
def api_player_notes(account_id: int):
    if not _viewer_has_key():
        return _deny_tournament()
    with Session(engine) as db:
        return jsonify({"notes": _notes_for(db, account_id)})


@app.post("/api/players/<int:account_id>/notes")
def api_player_note_create(account_id: int):
    if not _viewer_has_key():
        return _deny_tournament()
    data = request.get_json(silent=True) or {}
    author = (data.get("author") or "").strip()[:NOTE_AUTHOR_LIMIT]
    text = (data.get("text") or "").strip()[:NOTE_TEXT_LIMIT]
    if not author or not text:
        return jsonify({"error": "Заполните ник и текст."}), 400

    with Session(engine) as db:
        if db.get(Player, account_id) is None:
            return jsonify({"error": "not found"}), 404
        count = db.execute(
            select(func.count()).select_from(PlayerNote)
            .where(PlayerNote.account_id == account_id)
        ).scalar() or 0
        if count >= NOTES_PER_PLAYER_LIMIT:
            return jsonify({"error": "Слишком много заметок об этом игроке."}), 409
        db.add(PlayerNote(
            note_id=uuid.uuid4().hex,
            account_id=account_id,
            author=author,
            text=text,
            author_key_hash=_viewer_key_hash(),
            created_at=datetime.now(timezone.utc).isoformat(),
        ))
        db.commit()
        return jsonify({"notes": _notes_for(db, account_id)}), 201


@app.delete("/api/players/<int:account_id>/notes/<note_id>")
def api_player_note_delete(account_id: int, note_id: str):
    if not _viewer_has_key():
        return _deny_tournament()
    with Session(engine) as db:
        note = db.get(PlayerNote, note_id)
        if note is None or note.account_id != account_id:
            return jsonify({"error": "not found"}), 404
        mine = _viewer_key_hash()
        # A note restored from a backup written before author_key_hash existed
        # belongs to nobody, so nobody can delete it.
        if not mine or note.author_key_hash != mine:
            return jsonify({"error": "Удалять можно только свои заметки."}), 403
        db.delete(note)
        db.commit()
        return jsonify({"notes": _notes_for(db, account_id)})


@app.get("/api/matches/<int:match_id>")
def api_match_detail(match_id: int):
    """Everything we know about one game: both lineups (hero + player +
    KDA where known) and the full draft in pick order. KDA and duration come
    only from OpenDota-sourced details - Steam's league history doesn't carry
    them - so they're null on many matches and the frontend shows them only
    when present."""
    with Session(engine) as session:
        match = session.get(Match, match_id)
        if match is None:
            return jsonify({"error": "not found"}), 404

        mixer_tid = match.mixer_tournament_id
        if not _may_see_tournament(mixer_tid):
            return _deny_tournament()

        def side_name(team_id):
            # The name this team used in THIS match's tournament (ids are
            # recycled between cups), falling back to the current name.
            if team_id is None:
                return "?"
            return _team_name_in_tournament(session, team_id, mixer_tid) or f"Team {team_id}"

        player_rows = session.execute(
            select(
                MatchPlayer.account_id, MatchPlayer.is_radiant,
                MatchPlayer.kills, MatchPlayer.deaths, MatchPlayer.assists,
                MatchPlayer.gold_per_min, MatchPlayer.xp_per_min, MatchPlayer.net_worth,
                Hero.localized_name, Hero.name, Player.name,
            )
            .join(Hero, Hero.hero_id == MatchPlayer.hero_id)
            .outerjoin(Player, Player.account_id == MatchPlayer.account_id)
            .where(MatchPlayer.match_id == match_id)
        ).all()

        draft_rows = session.execute(
            select(
                MatchDraftEntry.order_num, MatchDraftEntry.is_pick,
                MatchDraftEntry.team_id, Hero.localized_name, Hero.name,
            )
            .join(Hero, Hero.hero_id == MatchDraftEntry.hero_id)
            .where(MatchDraftEntry.match_id == match_id)
            .order_by(MatchDraftEntry.order_num)
        ).all()

        radiant_id, dire_id = match.radiant_team_id, match.dire_team_id
        result = {
            "match_id": match_id,
            "start_time": match.start_time,
            "duration": match.duration,
            "radiant_win": match.radiant_win,
            "mixer_tournament_id": mixer_tid,
            "tournament_label": _tournament_label(mixer_tid, match.league_id),
        }

    def lineup(want_radiant: bool) -> list[dict]:
        rows = [
            {
                "account_id": account_id,
                "name": player_name or f"account {account_id}",
                "hero": hero_name,
                "hero_icon": _hero_icon_slug(internal_name),
                "kills": kills, "deaths": deaths, "assists": assists,
                "gpm": gpm, "xpm": xpm, "net_worth": net_worth,
            }
            for (account_id, is_radiant, kills, deaths, assists, gpm, xpm, net_worth,
                 hero_name, internal_name, player_name) in player_rows
            if bool(is_radiant) == want_radiant
        ]
        # Strongest first when we know net worth - the carry at the top reads
        # more naturally than draft-slot order.
        rows.sort(key=lambda r: (r["net_worth"] is None, -(r["net_worth"] or 0)))
        return rows

    def draft_side(team_id) -> list[dict]:
        return [
            {
                "order": order_num,
                "is_pick": is_pick,
                "hero": hero_name,
                "hero_icon": _hero_icon_slug(internal_name),
            }
            for order_num, is_pick, tid, hero_name, internal_name in draft_rows
            if tid == team_id
        ]

    result["radiant"] = {
        "team_id": radiant_id, "name": side_name(radiant_id),
        "players": lineup(True), "draft": draft_side(radiant_id),
    }
    result["dire"] = {
        "team_id": dire_id, "name": side_name(dire_id),
        "players": lineup(False), "draft": draft_side(dire_id),
    }
    result["has_draft"] = bool(draft_rows)
    return jsonify(result)


@app.get("/api/players")
def api_players_leaderboard():
    """Every participant of one tournament: current rosters (including subs who
    haven't played yet) plus anyone who actually played a game in it. Scoped to
    one cup by default - mixing cups would grade players on a team that no
    longer exists.

    ?tournament=all instead totals every cup at once, for the career view at
    /players. It counts only the cups this visitor may see, so a visitor
    without a key gets the archive's totals rather than a number the running
    cup silently contributes to."""
    active = _resolve_mixer_tournament_id()
    all_mode = (request.args.get("tournament") or "").strip().lower() == "all"

    with Session(engine) as session:
        if all_mode:
            known = [
                t for (t,) in session.execute(
                    select(Match.mixer_tournament_id)
                    .where(Match.mixer_tournament_id.is_not(None)).distinct()
                )
            ]
            counted = sorted((t for t in known if _may_see_tournament(t)), reverse=True)
            if not counted:
                return _deny_tournament()
            scope = None
            tour_filter = Match.mixer_tournament_id.in_(counted)
            # Today's roster is only meaningful when the running cup is part of
            # the picture; otherwise there is no "current team" to show.
            show_rosters = active in counted
            historical = False
        else:
            scope = _requested_scope()
            if not _may_see_tournament(scope):
                return _deny_tournament()
            counted = [scope] if scope is not None else []
            historical = scope is not None and scope not in _active_tournament_ids()
            tour_filter = (Match.mixer_tournament_id == scope) if scope is not None else True
            show_rosters = not historical

        decided = case((Match.radiant_win.is_not(None), 1), else_=0)
        won = case((MatchPlayer.is_radiant == Match.radiant_win, 1), else_=0)
        stat_rows = session.execute(
            select(MatchPlayer.account_id, func.count(), func.sum(decided), func.sum(won))
            .join(Match, Match.match_id == MatchPlayer.match_id)
            .where(tour_filter)
            .group_by(MatchPlayer.account_id)
        ).all()
        stats = {aid: (games, dec or 0, wins or 0) for aid, games, dec, wins in stat_rows}

        hero_rows = session.execute(
            select(MatchPlayer.account_id, Hero.localized_name, Hero.name, func.count())
            .join(Hero, Hero.hero_id == MatchPlayer.hero_id)
            .join(Match, Match.match_id == MatchPlayer.match_id)
            .where(tour_filter)
            .group_by(MatchPlayer.account_id, MatchPlayer.hero_id)
        ).all()
        heroes_by_player: dict[int, list] = {}
        for aid, hero_name, internal_name, count in hero_rows:
            heroes_by_player.setdefault(aid, []).append(
                (count, hero_name, _hero_icon_slug(internal_name))
            )

        # Current rosters only make sense for the live cup. On a finished
        # cup's page, a player's team is the one they actually played most of
        # that cup for - today's roster says nothing about it.
        # Состав берётся из ПОКАЗЫВАЕМОГО кубка, а не всегда из основного:
        # на странице игроков супермиксера иначе подставились бы команды
        # параллельного кубка PARI, где эти же люди тоже играют.
        roster_scope = scope if scope is not None else active
        roster_rows = session.execute(
            select(Player, Team)
            .join(Team, Team.team_id == Player.team_id)
            .where(Team.tournament_id == roster_scope, Player.roster_confirmed.is_(True))
        ).all() if roster_scope is not None and show_rosters else []
        rostered = {p.account_id: (p, t) for p, t in roster_rows}

        past_team_of: dict[int, tuple[int, str]] = {}
        if historical:
            names = dict(session.execute(
                select(TeamTournamentName.team_id, TeamTournamentName.name)
                .where(TeamTournamentName.tournament_id == scope)
            ).all())
            for account_id, team_id, _ in sorted(session.execute(
                select(MatchPlayer.account_id, MatchPlayer.team_id, func.count())
                .join(Match, Match.match_id == MatchPlayer.match_id)
                .where(tour_filter, MatchPlayer.team_id.is_not(None))
                .group_by(MatchPlayer.account_id, MatchPlayer.team_id)
            ).all(), key=lambda r: r[2]):
                # Ascending count, so the most-played team lands last and wins.
                past_team_of[account_id] = (team_id, names.get(team_id) or f"Team {team_id}")

        # Players who appear in games but were since subbed out still belong
        # on the board - their games happened - just without a current team.
        extra_ids = set(stats) - set(rostered)
        extras = {
            p.account_id: p
            for p in session.execute(
                select(Player).where(Player.account_id.in_(extra_ids))
            ).scalars()
        } if extra_ids else {}

        players = []
        for account_id in set(rostered) | set(stats):
            player, team = rostered.get(account_id, (extras.get(account_id), None))
            games, dec, wins = stats.get(account_id, (0, 0, 0))
            top = sorted(heroes_by_player.get(account_id, []), reverse=True)[:3]
            past_team_id, past_team_name = past_team_of.get(account_id, (None, None))
            players.append({
                "account_id": account_id,
                "name": (player.name if player and player.name else f"account {account_id}"),
                "mmr": player.mmr if player else None,
                "roles": player.preferred_roles if player else None,
                "team_id": team.team_id if team else past_team_id,
                "team_name": (team.name or f"Team {team.team_id}") if team else past_team_name,
                "games": games,
                "wins": wins,
                "losses": dec - wins,
                "win_rate": round(100 * wins / dec) if dec else None,
                "top_heroes": [
                    {"name": name, "icon": slug, "games": count}
                    for count, name, slug in top
                ] if _may_see_hero_pools() else [],
            })

    players.sort(key=lambda p: (p["mmr"] is None, -(p["mmr"] or 0)))
    if all_mode:
        label = "все турниры" if active in counted else "прошедшие турниры"
    else:
        label = _tournament_label(scope, None) if scope is not None else None
    return jsonify({
        "tournament_id": scope,
        "tournament_label": label,
        "is_historical": historical,
        "all_tournaments": all_mode,
        # Which cups the numbers actually cover - the running one drops out for
        # a visitor without a key, and the page should be able to say so.
        "tournaments_counted": [
            {"id": t, "label": _tournament_label(t, None)} for t in counted
        ] if all_mode else None,
        "hero_pools_locked": not _may_see_hero_pools(),
        "players": players,
    })


@app.get("/api/archive/player-heroes")
def api_archive_player_heroes():
    """Snapshot of every player's hero pool for the ACTIVE tournament, keyed
    by mixer tournament id. The backup workflow commits it to the data-backup
    branch as player-heroes-<id>.json - when the next tournament starts (new
    id, new file), the previous tournament's file survives there as reference
    data. Scoped by mixer_tournament_id, not league_id: consecutive mixer
    tournaments reuse the same dotabuff league."""
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

        tournament_id, historical = _requested_tournament(team)
        if not _may_see_tournament(tournament_id):
            return _deny_tournament()
        team_name = _team_name_in_tournament(session, team_id, tournament_id) or f"Team {team_id}"
        stats = compute_team_stats(session, team_id, tournament_id)
        text = generate_coach_text(team_name, stats)

        # Which heroes this squad's players are dangerous on. It is a hero pool
        # by another name, so it lives behind the key like every other one.
        roster = _team_player_names(session, team_id, tournament_id, historical)
        pools_locked = not _may_see_hero_pools()
        signature = [] if pools_locked else compute_player_signature_heroes(
            session, list(roster),
            min_games=PLAYER_HERO_MIN_GAMES, min_win_rate=PLAYER_HERO_MIN_WIN_RATE,
        )

    by_player: dict[int, list] = {}
    for hero in signature:
        by_player.setdefault(hero["account_id"], []).append({
            "hero": hero["hero"],
            "hero_icon": _hero_icon_slug(hero["hero_icon"]),
            "games": hero["games"],
            "wins": hero["wins"],
            "win_rate": hero["win_rate"],
        })
    player_heroes = [
        {"account_id": account_id, "name": roster[account_id], "heroes": heroes}
        for account_id, heroes in sorted(
            by_player.items(), key=lambda kv: roster[kv[0]].lower()
        )
    ]

    return jsonify({
        "player_heroes": player_heroes,
        "player_heroes_locked": pools_locked,
        "player_heroes_min_games": PLAYER_HERO_MIN_GAMES,
        "player_heroes_min_win_rate": PLAYER_HERO_MIN_WIN_RATE,
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
        # Only the active cup's substitutions are kept (see
        # collect._purge_past_tournament_subs), so a past cup's page has
        # nothing to show rather than the current squad's swaps.
        tournament_id, historical = _requested_tournament(team)
        if not _may_see_tournament(tournament_id):
            return _deny_tournament()
        swaps = [] if historical else _get_substitution_history(session, team_id, tournament_id)

    return jsonify({"team_id": team_id, "substitutions": swaps})


@app.get("/api/substitutions")
def api_all_substitutions():
    """Every substitution across the tournament, newest first, with the
    team it happened in - the per-team tab shows the same data scoped to
    one team; this powers the tournament-wide substitutions page."""
    scope = _requested_scope()
    # Only the active cup's substitutions are kept at all, so this page is
    # entirely current-tournament data.
    if not _may_see_tournament(scope):
        return _deny_tournament()
    with Session(engine) as session:
        team_filter = [Team.team_id.in_(select(SubstitutionEvent.team_id).distinct())]
        # Команды именно этого кубка. Раньше брались все подряд: замены хранились
        # только по одному активному кубку, так что чужих строк взяться было
        # неоткуда. Теперь кубка два (PARI и супермиксер), и без этого фильтра
        # команда с ещё не размеченными событиями всплыла бы на чужой странице.
        if scope is not None:
            team_filter.append(Team.tournament_id == scope)
        teams = session.execute(
            select(Team.team_id, Team.name).where(*team_filter)
        ).all()

        all_swaps = []
        for team_id, team_name in teams:
            for swap in _get_substitution_history(session, team_id, scope):
                swap["team_id"] = team_id
                swap["team_name"] = (
                    _team_name_in_tournament(session, team_id, scope)
                    or team_name or f"Team {team_id}"
                )
                all_swaps.append(swap)

    all_swaps.sort(key=lambda s: s["at"], reverse=True)
    return jsonify({
        "tournament_id": scope,
        "tournament_label": _tournament_label(scope, None) if scope is not None else None,
        "substitutions": all_swaps,
    })


# The tournament-wide hero stats page (win rates, ban counts, which heroes one
# player had to themselves) lived here. Removed at the owner's call: it read as
# trivia rather than something you act on, unlike the per-team and per-player
# views. compute_tournament_hero_stats() went with it.


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
        notes = session.execute(
            select(PlayerNote).order_by(PlayerNote.created_at)
        ).scalars().all()
        teams = session.execute(select(Team).order_by(Team.team_id)).scalars().all()
        all_players = session.execute(select(Player).order_by(Player.account_id)).scalars().all()
        draft_rows = session.execute(
            select(MatchDraftEntry.match_id, MatchDraftEntry.order_num,
                   MatchDraftEntry.hero_id, MatchDraftEntry.team_id,
                   MatchDraftEntry.is_pick)
            .order_by(MatchDraftEntry.match_id, MatchDraftEntry.order_num)
        ).all()
        match_rows = session.execute(
            select(Match.match_id, Match.league_id, Match.start_time, Match.duration,
                   Match.radiant_team_id, Match.dire_team_id, Match.radiant_win,
                   Match.mixer_tournament_id)
            .order_by(Match.match_id)
        ).all()
        lineup_rows = session.execute(
            select(MatchPlayer.match_id, MatchPlayer.account_id, MatchPlayer.hero_id,
                   MatchPlayer.team_id, MatchPlayer.is_radiant,
                   MatchPlayer.kills, MatchPlayer.deaths, MatchPlayer.assists,
                   MatchPlayer.gold_per_min, MatchPlayer.xp_per_min, MatchPlayer.net_worth)
            .order_by(MatchPlayer.match_id, MatchPlayer.account_id)
        ).all()

    # Grouped per match and written as bare tuples rather than objects with
    # repeated key names: ~7000 draft rows would otherwise dominate a file
    # that a GitHub Action rewrites on every run.
    drafts: dict[str, list] = {}
    for match_id, order_num, hero_id, team_id, is_pick in draft_rows:
        drafts.setdefault(str(match_id), []).append(
            [order_num, hero_id, team_id, 1 if is_pick else 0]
        )

    # Lineups, KDA and economy together - they are columns of the same row, so
    # there is no reason to carry the numbers separately from who played.
    # [account_id, hero_id, team_id, is_radiant, k, d, a, gpm, xpm, net_worth]
    lineups: dict[str, list] = {}
    for (match_id, account_id, hero_id, team_id, is_radiant,
         k, d, a, gpm, xpm, nw) in lineup_rows:
        lineups.setdefault(str(match_id), []).append(
            [account_id, hero_id, team_id, 1 if is_radiant else 0, k, d, a, gpm, xpm, nw]
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
                "tournament_id": e.tournament_id,
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
        # The only authored data here - nothing else in this file would be lost
        # for good if it went missing (see models.PlayerNote).
        "player_notes": [
            {
                "note_id": n.note_id, "account_id": n.account_id,
                "author": n.author, "text": n.text,
                "author_key_hash": n.author_key_hash, "created_at": n.created_at,
            }
            for n in notes
        ],
        # Steam's league history is a sliding window (measured at 500 matches
        # for this league), and the cups have outgrown it: a cup's oldest games
        # stop coming back after the host wipes the disk, which is how cup #1
        # lost half of its matches. Keeping them here is what makes the archive
        # durable - Steam then only has to supply what is new.
        # [match_id, league_id, start_time, duration, radiant_team_id,
        #  dire_team_id, radiant_win, mixer_tournament_id]
        "matches": [list(row) for row in match_rows],
        # match_id -> [[account_id, hero_id, team_id, is_radiant, k, d, a,
        #               gpm, xpm, net_worth], ...]
        "match_players": lineups,
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
