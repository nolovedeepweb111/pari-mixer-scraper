"""Вторая волна признаков - из того, что нашлось в литературе.

1. Рейтинг Брэдли-Терри: сила игрока как параметр логистической регрессии,
   подобранной по всем матчам сразу (в отличие от Эло не зависит от порядка
   игр и честно учитывает силу соперника). С гребневым штрафом, иначе игрок с
   100% побед уезжает в бесконечность.
2. Затухание: старые игры весят меньше, вес = 0.5 ** (возраст / период
   полураспада). Проверяем 30, 60, 120 дней.
3. Перформанс над своим слотом: внутри команды игроки ранжируются по добыче
   золота (1 - самый фармящий, 5 - самый бедный), и считается, насколько
   человек выше среднего по ЭТОМУ слоту. Саппорт перестаёт выглядеть слабым
   просто потому, что он саппорт.
"""
import math, pickle, sqlite3, sys, itertools
from collections import defaultdict

db, out_path = sys.argv[1], sys.argv[2]
c = sqlite3.connect(db); c.row_factory = sqlite3.Row

matches = [dict(r) for r in c.execute(
    "select match_id, mixer_tournament_id cup, start_time, radiant_team_id, dire_team_id,"
    " radiant_win, week_number from matches"
    " where mixer_tournament_id is not null and radiant_win is not null order by start_time")]
mp = [dict(r) for r in c.execute(
    "select match_id, account_id, is_radiant, kills, deaths, assists, gold_per_min from match_players")]
players = {r["account_id"]: dict(r) for r in c.execute(
    "select account_id, name, mmr, preferred_roles from players")}

by_match = defaultdict(list)
for r in mp:
    by_match[r["match_id"]].append(r)
CUPS = sorted({m["cup"] for m in matches},
              key=lambda cup: min(m["start_time"] for m in matches if m["cup"] == cup))
cup_start = {cup: min(m["start_time"] for m in matches if m["cup"] == cup) for cup in CUPS}

DAY = 86400.0
HALF_LIVES = (30 * DAY, 60 * DAY, 120 * DAY)

# Средняя доля золота для каждого места по фарму внутри команды (1..5).
slot_mean = defaultdict(lambda: [0.0, 0])
for m in matches:
    line = by_match.get(m["match_id"], [])
    for side in (True, False):
        team = sorted((r for r in line if bool(r["is_radiant"]) == side),
                      key=lambda r: -(r["gold_per_min"] or 0))
        total = sum(r["gold_per_min"] or 0 for r in line)
        if len(team) != 5 or not total:
            continue
        for i, r in enumerate(team):
            slot_mean[i][0] += (r["gold_per_min"] or 0) * 10 / total
            slot_mean[i][1] += 1
SLOT = {i: (v[0] / v[1] if v[1] else 1.0) for i, v in slot_mean.items()}


def bradley_terry(upto_time, iterations=120, lam=3.0):
    """Сила каждого игрока как параметр логистической регрессии по матчам.

    Команда = сумма параметров пятёрки, делённая на 5. Градиентный спуск -
    матриц тут не нужно, игроков несколько сотен."""
    rating = defaultdict(float)
    rows = []
    for m in matches:
        if m["start_time"] >= upto_time:
            break
        line = by_match.get(m["match_id"], [])
        rad = [r["account_id"] for r in line if r["is_radiant"]]
        dire = [r["account_id"] for r in line if not r["is_radiant"]]
        if len(rad) == 5 and len(dire) == 5:
            rows.append((rad, dire, 1.0 if m["radiant_win"] else 0.0))
    if not rows:
        return rating
    step = 0.6
    for _ in range(iterations):
        grad = defaultdict(float)
        for rad, dire, y in rows:
            diff = (sum(rating[a] for a in rad) - sum(rating[a] for a in dire)) / 5
            p = 1 / (1 + math.exp(-diff))
            err = y - p
            for a in rad:
                grad[a] += err / 5
            for a in dire:
                grad[a] -= err / 5
        for a in list(rating) + list(grad):
            rating[a] += step * (grad[a] - lam * rating[a] / len(rows))
    return rating


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


# Для каждого кубка - снимок истории на его начало.
print("считаю рейтинги Брэдли-Терри по кубкам...")
bt_by_cup = {cup: bradley_terry(cup_start[cup]) for cup in CUPS}

samples = []
for cup in CUPS:
    t0 = cup_start[cup]
    bt = bt_by_cup[cup]
    # история каждого игрока до начала кубка
    hist = defaultdict(list)
    for m in matches:
        if m["start_time"] >= t0:
            break
        line = by_match.get(m["match_id"], [])
        total = sum(r["gold_per_min"] or 0 for r in line)
        for side in (True, False):
            team = sorted((r for r in line if bool(r["is_radiant"]) == side),
                          key=lambda r: -(r["gold_per_min"] or 0))
            if len(team) != 5 or not total:
                continue
            for i, r in enumerate(team):
                share = (r["gold_per_min"] or 0) * 10 / total
                won = bool(r["is_radiant"]) == bool(m["radiant_win"])
                hist[r["account_id"]].append(
                    (m["start_time"], won, share, share - SLOT.get(i, 1.0)))
    ms_cup = [m for m in matches if m["cup"] == cup]
    weeks = sorted({m["week_number"] for m in ms_cup if m["week_number"] is not None}) or [None]
    for week in weeks:
        wms = [m for m in ms_cup if week is None or m["week_number"] == week]
        for team in {m["radiant_team_id"] for m in wms} | {m["dire_team_id"] for m in wms}:
            five = starting_five(cup, team, week)
            if len(five) != 5:
                continue
            tms = [m for m in wms if team in (m["radiant_team_id"], m["dire_team_id"])]
            if len(tms) < 5:
                continue
            wins = sum(1 for m in tms if (m["radiant_team_id"] == team) == bool(m["radiant_win"]))
            f = {}
            f["bt"] = sum(bt.get(a, 0.0) for a in five) / 5
            f["bt_max"] = max(bt.get(a, 0.0) for a in five)
            f["mmr"] = sum(players.get(a, {}).get("mmr") or 0 for a in five)
            for hl in HALF_LIVES:
                tag = int(hl / DAY)
                wr_num = wr_den = g_num = g_den = x_num = 0.0
                for a in five:
                    for ts, won, share, excess in hist.get(a, []):
                        w = 0.5 ** ((t0 - ts) / hl)
                        wr_num += w * won
                        wr_den += w
                        g_num += w * share
                        x_num += w * excess
                        g_den += w
                f["wr_%d" % tag] = (wr_num + 12.5) / (wr_den + 25)
                f["gold_%d" % tag] = g_num / g_den if g_den else 1.0
                f["excess_%d" % tag] = x_num / g_den if g_den else 0.0
            # без затухания, как в нынешней модели
            flat = [(won, share, excess) for a in five for _, won, share, excess in hist.get(a, [])]
            f["gold_flat"] = sum(s for _, s, _ in flat) / len(flat) if flat else 1.0
            f["excess_flat"] = sum(x for _, _, x in flat) / len(flat) if flat else 0.0
            f["wr_flat"] = (sum(w for w, _, _ in flat) + 12.5) / (len(flat) + 25)
            samples.append({"cup": cup, "week": week, "team": team, "five": five,
                            "y": wins / len(tms), "games": len(tms), "f": f})

pickle.dump({"samples": samples,
             "matches": [{k: m[k] for k in ("match_id", "cup", "start_time", "radiant_team_id",
                                            "dire_team_id", "radiant_win", "week_number")}
                         for m in matches]}, open(out_path, "wb"))


def corr(xs, ys):
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return 0.0 if not sx or not sy else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


y = [s["y"] for s in samples]
print("команд:", len(samples))
print()
print("связь с винрейтом команды:")
for f in sorted(samples[0]["f"], key=lambda f: -abs(corr([s["f"][f] for s in samples], y))):
    print("  %-12s %+.3f" % (f, corr([s["f"][f] for s in samples], y)))
