"""Стенд: какие факторы и какая модель лучше предсказывают успех команды.

Всё считается строго по играм ДО начала кубка, оценка - обучаемся без одного
кубка и предсказываем его. Метрики: порядок команд внутри кубка и угадывание
исхода конкретных матчей."""
import sqlite3, sys, math, itertools, pickle
from collections import defaultdict

db = sys.argv[1]
c = sqlite3.connect(db); c.row_factory = sqlite3.Row

matches = [dict(r) for r in c.execute(
    "select match_id, mixer_tournament_id cup, start_time, radiant_team_id, dire_team_id,"
    " radiant_win, week_number from matches"
    " where mixer_tournament_id is not null and radiant_win is not null order by start_time")]
rows_mp = [dict(r) for r in c.execute(
    "select match_id, account_id, is_radiant, kills, deaths, assists, gold_per_min,"
    " net_worth, hero_id from match_players")]
players = {r["account_id"]: dict(r) for r in c.execute(
    "select account_id, name, mmr, preferred_roles from players")}

by_match = defaultdict(list)
for r in rows_mp:
    by_match[r["match_id"]].append(r)
CUPS = sorted({m["cup"] for m in matches},
              key=lambda cup: min(m["start_time"] for m in matches if m["cup"] == cup))
cup_start = {cup: min(m["start_time"] for m in matches if m["cup"] == cup) for cup in CUPS}

# Сколько игр проводит команда за кубок (медиана) - чтобы доля отыгранных игр
# не зависела от формата.
cup_full_games = {}
for cup in CUPS:
    per_team = defaultdict(int)
    for m in matches:
        if m["cup"] != cup:
            continue
        per_team[m["radiant_team_id"]] += 1
        per_team[m["dire_team_id"]] += 1
    vals = sorted(per_team.values())
    cup_full_games[cup] = vals[len(vals) // 2] if vals else 1

ELO_K = 24
elo = defaultdict(lambda: 1500.0)
hist_wr = defaultdict(lambda: [0, 0])
econ = defaultdict(lambda: [0.0, 0.0, 0])
pairs = defaultdict(lambda: defaultdict(int))
games_in_cup = defaultdict(lambda: defaultdict(int))

elo_snapshot, wr_snapshot, econ_snapshot, pairs_snapshot, cup_games_snapshot = {}, {}, {}, {}, {}
snapshot_done = set()


def snap(cup):
    elo_snapshot[cup] = dict(elo)
    wr_snapshot[cup] = {a: tuple(v) for a, v in hist_wr.items()}
    econ_snapshot[cup] = {a: tuple(v) for a, v in econ.items()}
    pairs_snapshot[cup] = {a: dict(v) for a, v in pairs.items()}
    cup_games_snapshot[cup] = {a: dict(v) for a, v in games_in_cup.items()}
    snapshot_done.add(cup)


for m in matches:
    for cup in CUPS:
        if cup not in snapshot_done and m["start_time"] >= cup_start[cup]:
            snap(cup)
    line = by_match.get(m["match_id"], [])
    rad = [r for r in line if r["is_radiant"]]
    dire = [r for r in line if not r["is_radiant"]]
    if len(rad) == 5 and len(dire) == 5:
        ra = sum(elo[r["account_id"]] for r in rad) / 5
        da = sum(elo[r["account_id"]] for r in dire) / 5
        exp_r = 1 / (1 + 10 ** ((da - ra) / 400))
        res = 1.0 if m["radiant_win"] else 0.0
        for r in rad:
            elo[r["account_id"]] += ELO_K * (res - exp_r)
        for r in dire:
            elo[r["account_id"]] += ELO_K * ((1 - res) - (1 - exp_r))
        gold = sum(r["gold_per_min"] or 0 for r in line) or 1
        for r in line:
            e = econ[r["account_id"]]
            e[0] += (r["gold_per_min"] or 0) * 10 / gold
            e[1] += ((r["kills"] or 0) + (r["assists"] or 0)) / max(r["deaths"] or 0, 1)
            e[2] += 1
        for side in (rad, dire):
            ids = sorted(r["account_id"] for r in side)
            for a, b in itertools.combinations(ids, 2):
                pairs[a][b] += 1
                pairs[b][a] += 1
    for r in line:
        won = bool(r["is_radiant"]) == bool(m["radiant_win"])
        hist_wr[r["account_id"]][0] += 1
        hist_wr[r["account_id"]][1] += won
        games_in_cup[r["account_id"]][m["cup"]] += 1
for cup in CUPS:
    if cup not in snapshot_done:
        snap(cup)

CORE = {"CARRY", "MIDLANER", "OFFLANER"}
ROLES = ["CARRY", "MIDLANER", "OFFLANER", "SOFT_SUPPORT", "HARD_SUPPORT"]


def role_fit(five):
    prefs = []
    for a in five:
        raw = players.get(a, {}).get("preferred_roles") or ""
        prefs.append({r for r in raw.split(",") if r} or set(ROLES))
    best = [0]

    def rec(i, used, filled):
        best[0] = max(best[0], filled)
        if i == len(prefs) or filled + (len(prefs) - i) <= best[0]:
            return
        for r in prefs[i]:
            if r not in used:
                rec(i + 1, used | {r}, filled + 1)
        rec(i + 1, used, filled)

    rec(0, frozenset(), 0)
    cores = sum(1 for p in prefs if p & CORE)
    sups = sum(1 for p in prefs if not (p & CORE))
    return best[0] / 5, min(cores, 3) / 3, sups / 5


PRIOR = 25


def features(cup, five):
    wr_s = wr_snapshot[cup]
    e = elo_snapshot[cup]
    ec = econ_snapshot[cup]
    pr = pairs_snapshot[cup]
    cg = cup_games_snapshot[cup]
    wrs, elos, golds, kdas, mmrs, durs, seen = [], [], [], [], [], [], []
    for a in five:
        g, w = wr_s.get(a, (0, 0))
        wrs.append((w + PRIOR * 0.5) / (g + PRIOR))
        seen.append(g)
        elos.append(e.get(a, 1500.0))
        gg, kk, n = ec.get(a, (0.0, 0.0, 0))
        golds.append(gg / n if n else 1.0)
        kdas.append(kk / n if n else 2.5)
        mmrs.append(players.get(a, {}).get("mmr") or 0)
        shares = []
        for prev, played in (cg.get(a) or {}).items():
            if cup_start.get(prev, 0) >= cup_start[cup]:
                continue
            full = cup_full_games.get(prev, 0)
            if full:
                shares.append(min(played / full, 1.0))
        if shares:
            durs.append(sum(shares) / len(shares))
    slots, cores, sups = role_fit(five)
    together = sum(pr.get(a, {}).get(b, 0) for a, b in itertools.combinations(five, 2))
    mean_wr = sum(wrs) / 5
    mean_mmr = sum(mmrs) / 5
    return {
        "wr": mean_wr,
        "wr_min": min(wrs),
        "wr_max": max(wrs),
        "wr_spread": (sum((x - mean_wr) ** 2 for x in wrs) / 5) ** 0.5,
        "elo": sum(elos) / 5,
        "elo_min": min(elos),
        "elo_max": max(elos),
        "mmr": sum(mmrs),
        "mmr_top": max(mmrs),
        "mmr_spread": (sum((x - mean_mmr) ** 2 for x in mmrs) / 5) ** 0.5,
        "dur": sum(durs) / len(durs) if durs else 0.75,
        "slots": slots,
        "cores": cores,
        "sups": sups,
        "gold": sum(golds) / 5,
        "gold_top": max(golds),
        "kda": sum(kdas) / 5,
        "together": math.log1p(together),
        "newcomers": sum(1 for g in seen if g < 10) / 5,
        "experience": math.log1p(sum(seen) + 1),
    }


def starting_five(cup, team_id, week):
    ms = sorted((m for m in matches
                 if m["cup"] == cup and team_id in (m["radiant_team_id"], m["dire_team_id"])
                 and (week is None or m["week_number"] == week)),
                key=lambda m: m["start_time"])
    if not ms:
        return []
    first = ms[0]
    side = first["radiant_team_id"] == team_id
    return [r["account_id"] for r in by_match.get(first["match_id"], [])
            if bool(r["is_radiant"]) == side]


samples = []
for cup in CUPS:
    ms = [m for m in matches if m["cup"] == cup]
    weeks = sorted({m["week_number"] for m in ms if m["week_number"] is not None}) or [None]
    for week in weeks:
        wms = [m for m in ms if week is None or m["week_number"] == week]
        teams = {m["radiant_team_id"] for m in wms} | {m["dire_team_id"] for m in wms}
        for team in teams:
            five = starting_five(cup, team, week)
            if len(five) != 5:
                continue
            tms = [m for m in wms if team in (m["radiant_team_id"], m["dire_team_id"])]
            if len(tms) < 5:
                continue
            wins = sum(1 for m in tms if (m["radiant_team_id"] == team) == bool(m["radiant_win"]))
            samples.append({"cup": cup, "week": week, "team": team, "five": five,
                            "y": wins / len(tms), "games": len(tms), "f": features(cup, five)})

with open(sys.argv[2], "wb") as fh:
    pickle.dump({"samples": samples,
                 "matches": [{k: m[k] for k in ("match_id", "cup", "start_time",
                                                "radiant_team_id", "dire_team_id",
                                                "radiant_win", "week_number")} for m in matches]},
                fh)

print("команд в выборке:", len(samples))
FEATS = list(samples[0]["f"])


def corr(xs, ys):
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return 0.0 if not sx or not sy else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


y = [s["y"] for s in samples]
print()
print("связь каждого фактора с винрейтом команды:")
for f in sorted(FEATS, key=lambda f: -abs(corr([s["f"][f] for s in samples], y))):
    print("  %-12s %+.3f" % (f, corr([s["f"][f] for s in samples], y)))
