# Project map

Scouting site for PARI Mixer Cup and the WINLINE super mixer: rosters, hero
pools, drafts, substitutions, opponent analysis. Running cups are sold by key;
the archive is free.

*Русская версия карты — опубликованная страница, ссылку спросите у владельца.
Здесь то же самое короче: английский текст стоит вдвое меньше токенов.*

## Commands

| Task | How |
|---|---|
| Run locally | `.venv\Scripts\python.exe app.py` (see `.claude/launch.json`) |
| Deploy | `ssh root@45.12.238.194 "pms-update"` |
| Logs | `journalctl -u pari-mixer -f` |
| Collector state | `GET /api/collect/status` — public, never lies |

Secrets (`ACCESS_KEYS`, `AUTH_SECRET`, `OPS_TOKEN`, `STEAM_API_KEY`) live only in
`/etc/pari-mixer/env` on the server. Key ledger: `D:\claude\keys-pari-mixer-cup.md`,
deliberately outside the repo — the repo is public.

## Files

| File | Responsibility |
|---|---|
| `app.py` | Flask: endpoints, key access, tournament/slug resolution, launches collector |
| `collect.py` | Collection: Steam → mixer-cup → OpenDota; builds DB aside, swaps it in |
| `models.py` | 12 SQLite tables; `ensure_schema` adds missing columns |
| `sources.py` | Tournament sources: API url, id offset, leagues, address prefix |
| `mixercup_client.py` | mixer-cup GraphQL; applies the id offset itself |
| `steam_client.py` | League match history — the only cheap source of a match list |
| `opendota_client.py` | Match details (draft, KDA); raises on the daily quota |
| `analysis.py` | Team stats, signature heroes, targeted-ban inference |
| `static/app.js` | Whole front end: History-API routing, rendering, filters |
| `deploy/` | Installer, systemd, nginx, `update.sh` (= `pms-update` on the server) |

## Data flow

Steam gives the league's match list with lineups (one cheap call). mixer-cup
gives what Dota does not have: team names, rosters, MMR, results, substitutions,
queue, weeks. OpenDota gives per-match draft and KDA — one call per match, the
bottleneck.

The collector is a **separate process**: it copies the live DB, fills the copy,
and swaps it in with `os.replace`. The app uses `NullPool`, so it sees the new
file immediately.

## Invariants

- **One worker only.** Device bindings, collect status and caches live in process
  memory. A second worker means half the requests see different state.
- **`mixer_tournament_id` is the global key** — addresses, hero pools, access,
  leaderboards. Two sources, each numbering from its own 1, so ids are separated
  by an offset (`sources.py`): WINLINE #1 is stored as 20002. The client applies
  the offset; nothing else knows there is a second source.
- **A team lives one cup, sometimes one week.** Steam team ids are reused cup to
  cup, so a match shows the name from `team_tournament_names`, not `teams.name`.
  Sources with reshuffles also carry `teams.week_number`.
- **Hero pools are always gated**, even in the free archive. That is the product.
- **Cups run concurrently** and their matches interleave in time. Any "while the
  label is the same" grouping breaks — group by tournament id.

## Traps (measured, not assumed)

- **Steam returns at most 500 league matches.** Old cups fall out of the league
  history by themselves, so matches are kept in the backup or the archive shrinks.
- **OpenDota never has private-lobby matches.** A game played without the league
  ticket is 404 forever. Such ids go to `unavailable_matches`: without that the
  collector re-asked for them every 10 minutes and burned the daily quota itself,
  which looked exactly like being rate-limited from outside.
- **Steam `GetMatchDetails` 500s** for this league. Drafts come only from OpenDota.
- **The two mixer-cup deployments run different schemas.** `api.mixer-cup.gg` has
  no week fields and answers 400 to a query mentioning them, which kills that
  source's whole sync. Hence `MixerSource.has_weeks`.
- **mixer-cup exposes no Steam id for a player** — it is parsed out of the avatar
  URL. No avatar: match by nickname among known players
  (`_resolve_account_by_nickname`), else show a card with no stats
  (`unlinked_roster_players`).
- **Never run `install.sh` from `/opt`** — it `git reset --hard`s that directory
  while running, rewriting the file bash is reading itself from. That is what
  `pms-update` exists for.

## Env

Required in production: `STEAM_API_KEY`, `ACCESS_KEYS`, `AUTH_SECRET`, `OPS_TOKEN`,
`APP_DOMAIN`, `PUBLIC_ARCHIVE=1`, `MAX_DEVICES_PER_KEY=2`.

Thresholds, all with sane defaults: `TARGETED_BAN_MIN_BANS|_LIFT|_GAMES`,
`PLAYER_HERO_MIN_GAMES`, `PLAYER_HERO_MIN_WIN_RATE`, `MISSING_MATCH_RETRY_HOURS`,
`COLLECT_INTERVAL_SECONDS`, `TOURNAMENT_CACHE_TTL_SECONDS`.

`OPENDOTA_API_KEY` is unset — worth adding; it lifts the daily cap.

## How to verify a change

The local DB is a stale copy of production: good for mechanism, useless for
volume. What works:

1. Copy the DB to a temp dir and work on the copy.
2. Run the relevant collector function directly, not a whole cycle.
3. Point `TOURNAMENT_DB` at the copy and hit endpoints via `app.test_client()` —
   faster and more reliable than the browser.
4. Check the front end in the browser, but verify by reading the DOM, not by
   screenshot.
