"""Замены: как учитывать результаты кубка, если состав по ходу менялся.

Счёт команды складывается из игр, которые играли РАЗНЫЕ пятёрки. Проверяем,
что лучше: считать все игры одинаково или взвешивать их по тому, сколько
человек из нынешнего состава в них участвовало."""
import math, pickle, sqlite3, sys
from collections import defaultdict

data = pickle.load(open(sys.argv[1], "rb"))
samples = data["samples"]
matches = data["matches"]
CUPS = sorted({s["cup"] for s in samples})
FEATS = ["gold", "elo_max", "dur", "mmr"]

c = sqlite3.connect(sys.argv[2])
lineup = defaultdict(set)
for match_id, account_id, is_radiant in c.execute(
        "select match_id, account_id, is_radiant from match_players"):
    lineup[(match_id, bool(is_radiant))].add(account_id)


def solve(A, bb):
    n = len(A)
    M = [row[:] + [bb[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        if abs(M[col][col]) < 1e-12:
            continue
        M[col] = [v / M[col][col] for v in M[col]]
        for r in range(n):
            if r != col and M[r][col]:
                f = M[r][col]
                M[r] = [v - f * w for v, w in zip(M[r], M[col])]
    return [M[r][n] for r in range(n)]


def fit(rows, feats, y, lam=60.0):
    stats = {}
    for f in feats:
        xs = [r[f] for r in rows]
        mu = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / len(xs)) or 1.0
        stats[f] = (mu, sd)
    X = [[1.0] + [(r[f] - stats[f][0]) / stats[f][1] for f in feats] for r in rows]
    k = len(feats) + 1
    A = [[sum(X[i][p] * X[i][q] for i in range(len(X))) + (lam if p == q and p > 0 else 0.0)
          for q in range(k)] for p in range(k)]
    return {"beta": solve(A, [sum(X[i][p] * y[i] for i in range(len(X))) for p in range(k)]),
            "stats": stats, "feats": feats}


def predict(model, row):
    z = [1.0] + [(row[f] - model["stats"][f][0]) / model["stats"][f][1] for f in model["feats"]]
    return sum(p * q for p, q in zip(z, model["beta"]))


prior = {}
for held in CUPS:
    train = [s for s in samples if s["cup"] != held]
    model = fit([s["f"] for s in train], FEATS, [s["y"] for s in train])
    for s in samples:
        if s["cup"] == held:
            prior[(s["cup"], s["week"], s["team"])] = predict(model, s["f"])

PRIOR_GAMES = 10


def run(mode, power=1.0, min_overlap=0):
    """mode: plain - все игры одинаково; overlap - вес по доле нынешнего
    состава, игравшего ту игру; cut - считать только игры, где совпало не
    меньше min_overlap человек."""
    ok = tot = 0
    logloss = 0.0
    sub_ok, sub_tot, sub_ll = [0], [0], [0.0]
    # история игр команды в кубке: (победа, состав)
    played = defaultdict(list)
    for m in sorted(matches, key=lambda m: m["start_time"]):
        kr = (m["cup"], m["week_number"], m["radiant_team_id"])
        kd = (m["cup"], m["week_number"], m["dire_team_id"])
        pr, pd = prior.get(kr), prior.get(kd)
        if pr is None or pd is None:
            continue
        now = {kr: lineup.get((m["match_id"], True), set()),
               kd: lineup.get((m["match_id"], False), set())}

        def strength(key, base):
            wins = games = 0.0
            for won, five in played[key]:
                if mode == "plain":
                    w = 1.0
                else:
                    overlap = len(five & now[key])
                    if mode == "cut":
                        w = 1.0 if overlap >= min_overlap else 0.0
                    else:
                        w = (overlap / 5) ** power
                wins += w * won
                games += w
            return (base * PRIOR_GAMES + wins) / (PRIOR_GAMES + games)

        p = 1 / (1 + math.exp(-6 * (strength(kr, pr) - strength(kd, pd))))
        actual = 1 if m["radiant_win"] else 0
        # Менялся ли состав хоть у одной из команд по сравнению с её
        # предыдущими играми этого кубка.
        changed = any(
            played[k] and any(len(five & now[k]) < 5 for _, five in played[k])
            for k in (kr, kd))
        tot += 1
        ok += (p > 0.5) == bool(actual)
        logloss -= math.log(max(min(p if actual else 1 - p, 1 - 1e-9), 1e-9))
        if changed:
            sub_tot[0] += 1
            sub_ok[0] += (p > 0.5) == bool(actual)
            sub_ll[0] -= math.log(max(min(p if actual else 1 - p, 1 - 1e-9), 1e-9))
        played[kr].append((actual, now[kr]))
        played[kd].append((1 - actual, now[kd]))
    label = mode if mode == "plain" else (
        "%s^%.1f" % (mode, power) if mode == "overlap" else "%s>=%d" % (mode, min_overlap))
    print("  %-14s всё: %5.1f%% / %.4f   после замен: %5.1f%% / %.4f  (%d матчей)"
          % (label, 100 * ok / tot, logloss / tot,
             100 * sub_ok[0] / max(sub_tot[0], 1), sub_ll[0] / max(sub_tot[0], 1), sub_tot[0]))


run("plain")
for power in (0.5, 1.0, 2.0, 3.0):
    run("overlap", power=power)
for k in (3, 4, 5):
    run("cut", min_overlap=k)
