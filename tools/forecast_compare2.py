"""Сводит признаки обеих волн и сравнивает наборы честной проверкой."""
import math, pickle, sys, itertools

a = pickle.load(open(sys.argv[1], "rb"))   # первая волна
b = pickle.load(open(sys.argv[2], "rb"))   # вторая волна
matches = a["matches"]
index = {(s["cup"], s["week"], s["team"]): s for s in b["samples"]}
samples = []
for s in a["samples"]:
    other = index.get((s["cup"], s["week"], s["team"]))
    if not other:
        continue
    merged = dict(s)
    merged["f"] = {**s["f"], **other["f"]}
    samples.append(merged)
CUPS = sorted({s["cup"] for s in samples})
print("команд в общей выборке:", len(samples))


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


def fit(rows, feats, y, lam):
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


def evaluate(feats, lam, label):
    preds = {}
    for held in CUPS:
        train = [s for s in samples if s["cup"] != held]
        model = fit([s["f"] for s in train], feats, [s["y"] for s in train], lam)
        for s in samples:
            if s["cup"] == held:
                preds[(s["cup"], s["week"], s["team"])] = predict(model, s["f"])
    ys = [s["y"] for s in samples]
    ps = [preds[(s["cup"], s["week"], s["team"])] for s in samples]
    agree = tot = 0
    for cup in CUPS:
        idx = [i for i, s in enumerate(samples) if s["cup"] == cup]
        for i, j in itertools.combinations(idx, 2):
            if ys[i] != ys[j]:
                tot += 1
                agree += (ps[i] > ps[j]) == (ys[i] > ys[j])
    ok = mtot = 0
    logloss = 0.0
    for m in matches:
        r = preds.get((m["cup"], m["week_number"], m["radiant_team_id"]))
        d = preds.get((m["cup"], m["week_number"], m["dire_team_id"]))
        if r is None or d is None:
            continue
        p = 1 / (1 + math.exp(-6 * (r - d)))
        actual = 1 if m["radiant_win"] else 0
        mtot += 1
        ok += (p > 0.5) == bool(actual)
        logloss -= math.log(max(min(p if actual else 1 - p, 1 - 1e-9), 1e-9))
    mae = sum(abs(p - t) for p, t in zip(ps, ys)) / len(ys)
    print("  %-40s пары %5.1f%%  матчи %5.1f%%  logloss %.4f  ошибка %.3f"
          % (label, 100 * agree / tot, 100 * ok / mtot, logloss / mtot, mae))


SETS = [
    (["gold", "elo_max", "dur", "mmr"], "нынешняя"),
    (["excess_30", "elo_max", "dur", "mmr"], "слот-перформанс вместо золота"),
    (["excess_flat", "elo_max", "dur", "mmr"], "слот-перформанс без затухания"),
    (["gold", "excess_30", "elo_max", "dur", "mmr"], "золото + слот-перформанс"),
    (["excess_30", "elo_max", "dur", "mmr", "wr_30"], "+ свежий винрейт"),
    (["excess_30", "elo_max", "dur", "mmr", "bt"], "+ Брэдли-Терри"),
    (["excess_30", "elo", "elo_max", "dur", "mmr"], "+ средний Эло"),
    (["excess_30", "elo_max", "dur", "mmr", "cores"], "+ ядерные роли"),
    (["excess_30", "elo_max", "dur", "mmr", "kda"], "+ KDA"),
    (["excess_30", "elo_max", "dur", "mmr", "sups"], "+ доля саппортов"),
]
for lam in (20.0, 60.0):
    print("lam = %.0f" % lam)
    for feats, label in SETS:
        evaluate(feats, lam, label)
