"""Бустинг против линейной модели - на уровне отдельных матчей.

Строка выборки - сыгранный матч, признаки - РАЗНОСТИ между командами (сила по
составу, рейтинг лучшего игрока, суммарный MMR, доигрываемость, а также счёт в
кубке на момент матча со скидкой на замены). Цель - победила ли первая команда.

Проверка та же: обучаемся на всех кубках кроме одного, предсказываем его.
"""
import math, pickle, sqlite3, sys, random
from collections import defaultdict

data = pickle.load(open(sys.argv[1], "rb"))
samples = data["samples"]
matches = data["matches"]
CUPS = sorted({s["cup"] for s in samples})
BASE_FEATS = ["gold", "elo_max", "dur", "mmr", "wr", "elo", "slots", "cores", "sups",
              "kda", "mmr_spread", "wr_spread", "newcomers"]
PRIOR_GAMES = 10
OVERLAP_POWER = 3

c = sqlite3.connect(sys.argv[2])
lineup = defaultdict(set)
for match_id, account_id, is_radiant in c.execute(
        "select match_id, account_id, is_radiant from match_players"):
    lineup[(match_id, bool(is_radiant))].add(account_id)

team_f = {(s["cup"], s["week"], s["team"]): s["f"] for s in samples}
team_y = {(s["cup"], s["week"], s["team"]): s["y"] for s in samples}


# --- линейная часть: прогноз по составу, как в проде ------------------------
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


def fit_ridge(rows, feats, y, lam=60.0):
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


def predict_ridge(model, row):
    z = [1.0] + [(row[f] - model["stats"][f][0]) / model["stats"][f][1] for f in model["feats"]]
    return sum(p * q for p, q in zip(z, model["beta"]))


def build_rows(prior):
    """Матчи в хронологии со счётом команд на момент игры (со скидкой на замены)."""
    out = []
    played = defaultdict(list)
    for m in sorted(matches, key=lambda m: m["start_time"]):
        kr = (m["cup"], m["week_number"], m["radiant_team_id"])
        kd = (m["cup"], m["week_number"], m["dire_team_id"])
        if kr not in team_f or kd not in team_f or kr not in prior or kd not in prior:
            continue
        now = {kr: lineup.get((m["match_id"], True), set()),
               kd: lineup.get((m["match_id"], False), set())}
        feats = {}
        for f in BASE_FEATS:
            feats["d_" + f] = team_f[kr][f] - team_f[kd][f]
        feats["d_prior"] = prior[kr] - prior[kd]
        for key, tag in ((kr, "r"), (kd, "d")):
            wins = games = 0.0
            for won, five in played[key]:
                w = (len(five & now[key]) / 5) ** OVERLAP_POWER
                wins += w * won
                games += w
            feats[tag + "_games"] = games
            feats[tag + "_form"] = ((prior[key] * PRIOR_GAMES + wins) / (PRIOR_GAMES + games))
        feats["d_form"] = feats["r_form"] - feats["d_form"]
        feats["min_games"] = min(feats["r_games"], feats["d_games"])
        actual = 1 if m["radiant_win"] else 0
        out.append({"cup": m["cup"], "f": feats, "y": actual})
        played[kr].append((actual, now[kr]))
        played[kd].append((1 - actual, now[kd]))
    return out


prior = {}
for held in CUPS:
    train = [s for s in samples if s["cup"] != held]
    model = fit_ridge([s["f"] for s in train], ["gold", "elo_max", "dur", "mmr"],
                      [s["y"] for s in train])
    for s in samples:
        if s["cup"] == held:
            prior[(s["cup"], s["week"], s["team"])] = predict_ridge(model, s["f"])

rows = build_rows(prior)
FEATS = [f for f in rows[0]["f"] if not f.startswith(("r_", "d_games"))]
print("матчей:", len(rows), "| признаков:", len(FEATS))


# --- градиентный бустинг на деревьях ---------------------------------------
def build_tree(rows, grad, hess, feats, depth, min_leaf, lam):
    """Одно дерево на градиентах логистической ошибки (как в XGBoost, только
    без оптимизаций: данных мало)."""
    def leaf(idx):
        g = sum(grad[i] for i in idx)
        h = sum(hess[i] for i in idx)
        return {"leaf": -g / (h + lam)}

    def split(idx, d):
        if d == 0 or len(idx) < 2 * min_leaf:
            return leaf(idx)
        G = sum(grad[i] for i in idx)
        H = sum(hess[i] for i in idx)
        base = G * G / (H + lam)
        best = None
        for f in feats:
            order = sorted(idx, key=lambda i: rows[i]["f"][f])
            gl = hl = 0.0
            for pos in range(len(order) - 1):
                i = order[pos]
                gl += grad[i]
                hl += hess[i]
                if pos + 1 < min_leaf or len(order) - pos - 1 < min_leaf:
                    continue
                v1, v2 = rows[order[pos]]["f"][f], rows[order[pos + 1]]["f"][f]
                if v1 == v2:
                    continue
                gain = gl * gl / (hl + lam) + (G - gl) ** 2 / (H - hl + lam) - base
                if best is None or gain > best[0]:
                    best = (gain, f, (v1 + v2) / 2, order[:pos + 1], order[pos + 1:])
        if best is None or best[0] <= 1e-6:
            return leaf(idx)
        _, f, thr, left, right = best
        return {"f": f, "thr": thr,
                "left": split(left, d - 1), "right": split(right, d - 1)}

    return split(list(range(len(rows))), depth)


def tree_predict(node, row):
    while "leaf" not in node:
        node = node["left"] if row["f"][node["f"]] <= node["thr"] else node["right"]
    return node["leaf"]


def fit_gbm(train, feats, rounds=120, depth=3, rate=0.08, min_leaf=40, lam=10.0):
    base = math.log(max(sum(r["y"] for r in train), 1) / max(len(train) - sum(r["y"] for r in train), 1))
    scores = [base] * len(train)
    trees = []
    for _ in range(rounds):
        grad, hess = [], []
        for i, r in enumerate(train):
            p = 1 / (1 + math.exp(-scores[i]))
            grad.append(p - r["y"])
            hess.append(max(p * (1 - p), 1e-6))
        tree = build_tree(train, grad, hess, feats, depth, min_leaf, lam)
        for i, r in enumerate(train):
            scores[i] += rate * tree_predict(tree, r)
        trees.append(tree)
    return {"base": base, "trees": trees, "rate": rate}


def gbm_predict(model, row):
    s = model["base"] + model["rate"] * sum(tree_predict(t, row) for t in model["trees"])
    return 1 / (1 + math.exp(-s))


def report(name, preds, ys):
    ok = sum((p > 0.5) == bool(y) for p, y in zip(preds, ys))
    ll = -sum(math.log(max(min(p if y else 1 - p, 1 - 1e-9), 1e-9)) for p, y in zip(preds, ys))
    print("  %-34s угадано %5.1f%%  logloss %.4f" % (name, 100 * ok / len(ys), ll / len(ys)))


ys = [r["y"] for r in rows]
# нынешняя модель: логистика от разницы оценок
report("нынешняя (состав + счёт)", [1 / (1 + math.exp(-6 * r["f"]["d_form"])) for r in rows], ys)

for depth, rounds, rate, min_leaf in ((2, 150, 0.06, 60), (3, 120, 0.08, 40), (4, 200, 0.05, 30)):
    preds = [0.5] * len(rows)
    for held in CUPS:
        train = [r for r in rows if r["cup"] != held]
        test_idx = [i for i, r in enumerate(rows) if r["cup"] == held]
        model = fit_gbm(train, FEATS, rounds=rounds, depth=depth, rate=rate, min_leaf=min_leaf)
        for i in test_idx:
            preds[i] = gbm_predict(model, rows[i])
    report("бустинг depth=%d rounds=%d rate=%.2f" % (depth, rounds, rate), preds, ys)

# --- вторая волна: мельче деревья, меньше признаков, ансамбль ---------------
SMALL = ["d_form", "d_prior", "min_games", "d_gold", "d_elo_max", "d_mmr"]


def fit_logistic(train, feats, iters=400, rate=0.3, lam=1.0):
    """Логистическая регрессия - честная база: масштаб подбирается по данным,
    а не берётся с потолка."""
    w = {f: 0.0 for f in feats}
    b = 0.0
    n = len(train)
    for _ in range(iters):
        gb = 0.0
        gw = {f: 0.0 for f in feats}
        for r in train:
            z = b + sum(w[f] * r["f"][f] for f in feats)
            p = 1 / (1 + math.exp(-max(min(z, 30), -30)))
            e = r["y"] - p
            gb += e
            for f in feats:
                gw[f] += e * r["f"][f]
        b += rate * gb / n
        for f in feats:
            w[f] += rate * (gw[f] / n - lam * w[f] / n)
    return {"w": w, "b": b, "feats": feats}


def logistic_predict(model, row):
    z = model["b"] + sum(model["w"][f] * row["f"][f] for f in model["feats"])
    return 1 / (1 + math.exp(-max(min(z, 30), -30)))


print()
print("вторая волна:")
for name, feats in (("логистика на d_form", ["d_form"]),
                    ("логистика, 6 признаков", SMALL)):
    preds = [0.5] * len(rows)
    for held in CUPS:
        train = [r for r in rows if r["cup"] != held]
        model = fit_logistic(train, feats)
        for i, r in enumerate(rows):
            if r["cup"] == held:
                preds[i] = logistic_predict(model, r)
    report(name, preds, ys)

gbm_preds = {}
for tag, depth, rounds, rate, min_leaf, feats in (
        ("пни, 300 раундов", 1, 300, 0.05, 60, FEATS),
        ("пни, 6 признаков", 1, 300, 0.05, 60, SMALL),
        ("depth=2, 6 признаков", 2, 150, 0.05, 80, SMALL)):
    preds = [0.5] * len(rows)
    for held in CUPS:
        train = [r for r in rows if r["cup"] != held]
        model = fit_gbm(train, feats, rounds=rounds, depth=depth, rate=rate, min_leaf=min_leaf)
        for i, r in enumerate(rows):
            if r["cup"] == held:
                preds[i] = gbm_predict(model, r)
    gbm_preds[tag] = preds
    report("бустинг: " + tag, preds, ys)

base = [1 / (1 + math.exp(-6 * r["f"]["d_form"])) for r in rows]
for tag, preds in gbm_preds.items():
    mixed = [(a + b) / 2 for a, b in zip(base, preds)]
    report("ансамбль с линейной: " + tag, mixed, ys)
