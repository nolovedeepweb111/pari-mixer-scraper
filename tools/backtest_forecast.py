"""Что на самом деле предсказывает успех команды в миксере.

Для каждого прошлого кубка берём стартовую пятёрку команды (кто играл её
ПЕРВЫЙ матч), считаем признаки ТОЛЬКО по играм до начала этого кубка и
сравниваем с тем, как команда отыграла. Иначе получится подгонка под ответ."""
import sqlite3, sys, math
from collections import defaultdict

db = sys.argv[1]
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row

CUPS = [r[0] for r in c.execute(
    "select mixer_tournament_id from matches where mixer_tournament_id is not null "
    "group by 1 having count(*) > 50 order by min(start_time)")]

matches = c.execute("""select match_id, mixer_tournament_id cup, start_time, radiant_team_id, dire_team_id,
    radiant_win, week_number from matches where mixer_tournament_id is not null and radiant_win is not null""").fetchall()
mp = c.execute("select match_id, account_id, team_id, is_radiant from match_players").fetchall()
players = {r["account_id"]: r for r in c.execute("select account_id, name, mmr, preferred_roles from players")}

by_match = defaultdict(list)
for r in mp:
    by_match[r["match_id"]].append(r)
m_by_id = {m["match_id"]: m for m in matches}

# История игрока: (время, выиграл ли) - чтобы считать винрейт на любую дату.
hist = defaultdict(list)
# Игры игрока в каждом кубке - для «доигрывает ли».
games_in_cup = defaultdict(lambda: defaultdict(int))
for m in matches:
    for r in by_match.get(m["match_id"], []):
        won = (r["is_radiant"] == 1) == (m["radiant_win"] == 1)
        hist[r["account_id"]].append((m["start_time"], won, m["cup"]))
        games_in_cup[r["account_id"]][m["cup"]] += 1
for a in hist: hist[a].sort()

cup_start = {}
cup_games_per_team = {}
for cup in CUPS:
    ms = [m for m in matches if m["cup"] == cup]
    cup_start[cup] = min(m["start_time"] for m in ms)
    per_team = defaultdict(int)
    for m in ms:
        per_team[m["radiant_team_id"]] += 1; per_team[m["dire_team_id"]] += 1
    cup_games_per_team[cup] = per_team

def starting_five(cup, team_id, week=None):
    ms = sorted((m for m in matches if m["cup"] == cup
                 and team_id in (m["radiant_team_id"], m["dire_team_id"])
                 and (week is None or m["week_number"] == week)),
                key=lambda m: m["start_time"])
    if not ms: return []
    first = ms[0]
    side = first["radiant_team_id"] == team_id
    return [r["account_id"] for r in by_match.get(first["match_id"], []) if (r["is_radiant"] == 1) == side]

PRIOR = 25  # сила априорного «50%»: столько виртуальных игр подмешиваем
def wr_before(account_id, t):
    w = n = 0
    for ts, won, _ in hist.get(account_id, []):
        if ts >= t: break
        n += 1; w += won
    return (w + PRIOR * 0.5) / (n + PRIOR), n

def durability_before(account_id, cup):
    """Какую долю игр своей команды игрок обычно отыгрывает - по прошлым кубкам."""
    shares = []
    for prev in CUPS:
        if cup_start[prev] >= cup_start[cup]: continue
        g = games_in_cup[account_id].get(prev, 0)
        if not g: continue
        team_games = sorted(cup_games_per_team[prev].values())
        full = team_games[len(team_games)//2] if team_games else 0
        if full: shares.append(min(g / full, 1.0))
    return sum(shares) / len(shares) if shares else None

CORE = {"CARRY", "MIDLANER", "OFFLANER"}
ROLES = ["CARRY", "MIDLANER", "OFFLANER", "SOFT_SUPPORT", "HARD_SUPPORT"]
def role_score(five):
    """Можно ли расставить пятёрку по пяти разным ролям (венгерка на 5 узлах -
    перебором), и сколько ядерных ролей закрыто."""
    prefs = []
    for a in five:
        p = (players.get(a) or {})
        raw = (p["preferred_roles"] if p else None) or ""
        prefs.append({r for r in raw.split(",") if r} or set(ROLES))
    best = [0]
    def rec(i, used, filled):
        if i == len(prefs):
            best[0] = max(best[0], filled); return
        if filled + (len(prefs) - i) <= best[0]: return
        for r in prefs[i]:
            if r not in used: rec(i + 1, used | {r}, filled + 1)
        rec(i + 1, used, filled)
    rec(0, frozenset(), 0)
    cores = sum(1 for p in prefs if p & CORE)
    return best[0] / 5, min(cores, 3) / 3

rows = []
for cup in CUPS:
    ms = [m for m in matches if m["cup"] == cup]
    weeks = sorted({m["week_number"] for m in ms if m["week_number"] is not None}) or [None]
    for week in weeks:
        wms = [m for m in ms if week is None or m["week_number"] == week]
        teams = {m["radiant_team_id"] for m in wms} | {m["dire_team_id"] for m in wms}
        for team in teams:
            five = starting_five(cup, team, week)
            if len(five) != 5: continue
            tms = [m for m in wms if team in (m["radiant_team_id"], m["dire_team_id"])]
            wins = sum(1 for m in tms if (m["radiant_team_id"] == team) == (m["radiant_win"] == 1))
            if len(tms) < 5: continue
            t0 = cup_start[cup]
            wrs = [wr_before(a, t0) for a in five]
            exp = [p for p, _ in wrs]
            seen = sum(n for _, n in wrs)
            durs = [durability_before(a, cup) for a in five]
            durs = [d for d in durs if d is not None]
            mmr = sum((players.get(a) or {"mmr": None})["mmr"] or 0 for a in five)
            slots, cores = role_score(five)
            rows.append({
                "cup": cup, "week": week, "team": team, "winrate": wins / len(tms), "games": len(tms),
                "wr": sum(exp) / 5, "experience": seen,
                "dur": sum(durs) / len(durs) if durs else 0.75,
                "mmr": mmr, "slots": slots, "cores": cores,
            })

def corr(xs, ys):
    n = len(xs); mx = sum(xs)/n; my = sum(ys)/n
    sx = math.sqrt(sum((x-mx)**2 for x in xs)); sy = math.sqrt(sum((y-my)**2 for y in ys))
    if not sx or not sy: return 0.0
    return sum((x-mx)*(y-my) for x, y in zip(xs, ys))/(sx*sy)

print(f"команд в выборке: {len(rows)} (кубки: {sorted({r['cup'] for r in rows})})")
target = [r["winrate"] for r in rows]
for f in ("wr", "mmr", "dur", "slots", "cores", "experience"):
    print(f"  корреляция {f:11} с винрейтом: {corr([r[f] for r in rows], target):+.3f}")

# --- Линейная модель и проверка на отложенных кубках -------------------------
FEATURES = ["wr", "mmr", "dur", "slots", "cores"]

def standardize(rows):
    stats = {}
    for f in FEATURES:
        xs = [r[f] for r in rows]
        mu = sum(xs)/len(xs)
        sd = math.sqrt(sum((x-mu)**2 for x in xs)/len(xs)) or 1.0
        stats[f] = (mu, sd)
    return stats

def design(rows, stats):
    return [[1.0] + [(r[f]-stats[f][0])/stats[f][1] for f in FEATURES] for r in rows]

def ols(X, y):
    k = len(X[0])
    A = [[sum(X[i][a]*X[i][b] for i in range(len(X))) for b in range(k)] + [sum(X[i][a]*y[i] for i in range(len(X)))] for a in range(k)]
    for col in range(k):
        piv = max(range(col, k), key=lambda r: abs(A[r][col]))
        A[col], A[piv] = A[piv], A[col]
        if abs(A[col][col]) < 1e-12: continue
        A[col] = [v / A[col][col] for v in A[col]]
        for r in range(k):
            if r != col and A[r][col]:
                factor = A[r][col]
                A[r] = [v - factor*w for v, w in zip(A[r], A[col])]
    return [A[r][k] for r in range(k)]

stats = standardize(rows)
beta = ols(design(rows, stats), target)
print("\nвеса (на всей выборке, признаки нормированы):")
print("  свободный член %.3f" % beta[0])
for f, b in zip(FEATURES, beta[1:]): print(f"  {f:11} {b:+.4f}")

cups = sorted({r["cup"] for r in rows})
print("\nпроверка: обучаем без одного кубка, предсказываем его")
all_pred, all_true, base_pred = [], [], []
for held in cups:
    train = [r for r in rows if r["cup"] != held]
    test = [r for r in rows if r["cup"] == held]
    st = standardize(train)
    b = ols(design(train, st), [r["winrate"] for r in train])
    pred = [sum(x*w for x, w in zip(row, b)) for row in design(test, st)]
    true = [r["winrate"] for r in test]
    # базовая линия: только MMR
    bm = ols([[1.0, (r["mmr"]-st["mmr"][0])/st["mmr"][1]] for r in train], [r["winrate"] for r in train])
    pm = [bm[0] + bm[1]*(r["mmr"]-st["mmr"][0])/st["mmr"][1] for r in test]
    all_pred += pred; all_true += true; base_pred += pm
    mae = sum(abs(p-t) for p, t in zip(pred, true))/len(test)
    print(f"  кубок {held:>6}: команд {len(test):>3}  корреляция {corr(pred, true):+.3f}  средняя ошибка {mae:.3f}")
print(f"\nвсё вместе: корреляция {corr(all_pred, all_true):+.3f}, "
      f"средняя ошибка {sum(abs(p-t) for p,t in zip(all_pred,all_true))/len(all_true):.3f}")
print(f"только MMR: корреляция {corr(base_pred, all_true):+.3f}, "
      f"средняя ошибка {sum(abs(p-t) for p,t in zip(base_pred,all_true))/len(all_true):.3f}")
print(f"'все по 50%': средняя ошибка {sum(abs(0.5-t) for t in all_true)/len(all_true):.3f}")
# Насколько верно угадывается ПОРЯДОК: доля пар команд одного кубка, где
# предсказание и результат согласны.
agree = tot = 0
for cup in cups:
    idx = [i for i, r in enumerate(rows) if r["cup"] == cup]
    for i in idx:
        for j in idx:
            if i < j and all_true[i] != all_true[j]:
                tot += 1
                agree += (all_pred[i] > all_pred[j]) == (all_true[i] > all_true[j])
print(f"порядок пар угадан: {100*agree/tot:.1f}% (случайно было бы 50%)")
