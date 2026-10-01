"""Честная проверка: отбор факторов делается ВНУТРИ обучающей части, а
отложенный кубок модель не видит вообще - ни при обучении, ни при отборе.
Иначе набор факторов подгоняется под ту же проверку, по которой его хвалят."""
import math, pickle, sys, itertools

data = pickle.load(open(sys.argv[1], "rb"))
samples = data["samples"]
matches = data["matches"]
CUPS = sorted({s["cup"] for s in samples})
ALL = list(samples[0]["f"])


def solve(A, b):
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
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


def fit(rows, feats, y, lam):
    stats = {}
    for f in feats:
        xs = [r[f] for r in rows]
        mu = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / len(xs)) or 1.0
        stats[f] = (mu, sd)
    X = [[1.0] + [(r[f] - stats[f][0]) / stats[f][1] for f in feats] for r in rows]
    k = len(feats) + 1
    A = [[sum(X[i][a] * X[i][b] for i in range(len(X))) + (lam if a == b and a > 0 else 0.0)
          for b in range(k)] for a in range(k)]
    bb = [sum(X[i][a] * y[i] for i in range(len(X))) for a in range(k)]
    return {"beta": solve(A, bb), "stats": stats, "feats": feats}


def predict(model, row):
    z = [1.0] + [(row[f] - model["stats"][f][0]) / model["stats"][f][1] for f in model["feats"]]
    return sum(a * b for a, b in zip(z, model["beta"]))


def pair_accuracy(sample_set, preds):
    agree = tot = 0
    for cup in {s["cup"] for s in sample_set}:
        idx = [s for s in sample_set if s["cup"] == cup]
        for a, b in itertools.combinations(idx, 2):
            if a["y"] != b["y"]:
                tot += 1
                agree += (preds[id(a)] > preds[id(b)]) == (a["y"] > b["y"])
    return 100 * agree / tot if tot else 50.0


def cv_select(train, lam, max_feats=6):
    """Жадный отбор на обучающих кубках, со своей внутренней проверкой."""
    inner_cups = sorted({s["cup"] for s in train})
    chosen, best = [], 0.0
    pool = list(ALL)
    while pool and len(chosen) < max_feats:
        scored = []
        for f in pool:
            feats = chosen + [f]
            preds = {}
            for held in inner_cups:
                tr = [s for s in train if s["cup"] != held]
                te = [s for s in train if s["cup"] == held]
                model = fit([s["f"] for s in tr], feats, [s["y"] for s in tr], lam)
                for s in te:
                    preds[id(s)] = predict(model, s["f"])
            scored.append((pair_accuracy(train, preds), f))
        scored.sort(reverse=True)
        gain, f = scored[0]
        if gain <= best + 0.15:
            break
        best, _ = gain, chosen.append(f)
        pool.remove(f)
    return chosen


def outer(lam, fixed=None, label=""):
    preds, picks = {}, []
    for held in CUPS:
        train = [s for s in samples if s["cup"] != held]
        feats = fixed or cv_select(train, lam)
        picks.append((held, feats))
        model = fit([s["f"] for s in train], feats, [s["y"] for s in train], lam)
        for s in samples:
            if s["cup"] == held:
                preds[id(s)] = predict(model, s["f"])
    acc = pair_accuracy(samples, preds)
    mae = sum(abs(preds[id(s)] - s["y"]) for s in samples) / len(samples)
    by_team = {(s["cup"], s["week"], s["team"]): preds[id(s)] for s in samples}
    ok = tot = 0
    logloss = 0.0
    for m in matches:
        r = by_team.get((m["cup"], m["week_number"], m["radiant_team_id"]))
        d = by_team.get((m["cup"], m["week_number"], m["dire_team_id"]))
        if r is None or d is None:
            continue
        p = 1 / (1 + math.exp(-6 * (r - d)))
        actual = 1 if m["radiant_win"] else 0
        tot += 1
        ok += (p > 0.5) == bool(actual)
        logloss -= math.log(max(min(p if actual else 1 - p, 1 - 1e-9), 1e-9))
    print("  %-34s порядок пар %5.1f%%  ошибка %.3f  матчи %5.1f%%  logloss %.4f"
          % (label, acc, mae, 100 * ok / tot, logloss / tot))
    return picks


print("вложенная проверка (отбор внутри обучения):")
for lam in (5.0, 20.0, 60.0):
    picks = outer(lam, None, "отбор внутри, lam=%.0f" % lam)
    if lam == 20.0:
        for cup, feats in picks:
            print("        без кубка %-6s выбрал: %s" % (cup, ", ".join(feats)))

print()
print("фиксированные наборы (для сравнения):")
for feats in (["wr", "mmr", "dur", "slots", "cores"],
              ["gold", "elo_max", "dur", "mmr"],
              ["gold", "elo_max", "dur", "mmr", "cores"],
              ["gold", "elo_max", "dur", "mmr", "cores", "slots"],
              ["gold", "elo", "wr", "mmr", "dur", "cores"],
              ALL):
    for lam in (5.0, 20.0, 60.0):
        outer(lam, feats, "%s | lam=%.0f" % (",".join(feats[:4]) + ("..." if len(feats) > 4 else ""), lam))
