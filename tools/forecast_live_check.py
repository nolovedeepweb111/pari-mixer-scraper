"""Стоит ли подмешивать результаты ИДУЩЕГО кубка в прогноз.

Дашборд живой: к середине кубка у каждой команды уже десяток игр, и вопрос,
что предсказывает лучше - состав, текущий счёт или их смесь. Проверка честная:
каждый матч предсказывается по тому, что было известно ДО него.
"""
import math, pickle, sys, itertools
from collections import defaultdict

a = pickle.load(open(sys.argv[1], "rb"))
samples = a["samples"]
matches = a["matches"]
CUPS = sorted({s["cup"] for s in samples})
FEATS = ["gold", "elo_max", "dur", "mmr"]


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


# Прогноз по составу - как сейчас, обучение без своего кубка.
prior = {}
for held in CUPS:
    train = [s for s in samples if s["cup"] != held]
    model = fit([s["f"] for s in train], FEATS, [s["y"] for s in train])
    for s in samples:
        if s["cup"] == held:
            prior[(s["cup"], s["week"], s["team"])] = predict(model, s["f"])


def run(mode, n0=None, elo_k=None, skip_first=0):
    """Идём по матчам кубка подряд и предсказываем каждый по предыдущим."""
    ok = tot = 0
    logloss = 0.0
    record = defaultdict(lambda: [0, 0])     # победы, игры в этом кубке
    live_elo = defaultdict(float)
    for m in sorted(matches, key=lambda m: m["start_time"]):
        kr = (m["cup"], m["week_number"], m["radiant_team_id"])
        kd = (m["cup"], m["week_number"], m["dire_team_id"])
        pr, pd = prior.get(kr), prior.get(kd)
        if pr is None or pd is None:
            continue
        played = min(record[kr][1], record[kd][1])

        def strength(key, base):
            w, n = record[key]
            if mode == "static":
                return base
            if mode == "record":
                return (w + 0.5) / (n + 1) if n else base
            if mode == "blend":
                # Апостериорная оценка: прогноз по составу весит n0 виртуальных
                # игр, дальше его перебивают настоящие результаты.
                return (base * n0 + w) / (n0 + n)
            if mode == "elo":
                return base + live_elo[key]
            raise ValueError(mode)

        sr, sd = strength(kr, pr), strength(kd, pd)
        p = 1 / (1 + math.exp(-6 * (sr - sd)))
        actual = 1 if m["radiant_win"] else 0
        if played >= skip_first:
            tot += 1
            ok += (p > 0.5) == bool(actual)
            logloss -= math.log(max(min(p if actual else 1 - p, 1 - 1e-9), 1e-9))
        record[kr][0] += actual
        record[kr][1] += 1
        record[kd][0] += 1 - actual
        record[kd][1] += 1
        if elo_k:
            live_elo[kr] += elo_k * (actual - p)
            live_elo[kd] += elo_k * ((1 - actual) - (1 - p))
    label = mode if n0 is None else "%s n0=%d" % (mode, n0)
    if elo_k:
        label += " k=%.2f" % elo_k
    print("  %-22s матчей %4d  угадано %5.1f%%  logloss %.4f"
          % (label, tot, 100 * ok / tot, logloss / tot))


for skip in (0, 5):
    print("после %d сыгранных игр у обеих команд:" % skip)
    run("static", skip_first=skip)
    run("record", skip_first=skip)
    for n0 in (3, 6, 10, 20, 40):
        run("blend", n0=n0, skip_first=skip)
    for k in (0.01, 0.02, 0.04):
        run("elo", elo_k=k, skip_first=skip)
