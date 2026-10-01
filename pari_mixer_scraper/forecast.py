"""Прогноз силы команд кубка.

Веса не выдуманы: подобраны гребневой регрессией по 171 команде из семи прошлых
кубков (26-30, супермиксеры WINLINE и PARI). Признаки каждой команды считаются
ТОЛЬКО по играм до начала её кубка, иначе вышла бы подгонка под ответ.

Проверка - «обучаемся без одного кубка, предсказываем его», причём набор
факторов тоже отбирался внутри обучающей части (вложенная проверка), чтобы не
хвалить модель по тем же данным, на которых её подбирали:

                            порядок пар команд   исход матча   logloss
    только суммарный MMR          60.0%             56.1%       0.688
    первая версия (винрейт,
    MMR, роли, доигрываемость)    64.3%             57.8%       0.684
    нынешняя                      68.0%             59.4%       0.665
    честная вложенная оценка      66.4%             59.3%       0.675

«Порядок пар» - доля пар команд одного кубка, которые модель расставила
правильно (у монетки 50%). «Исход матча» - доля угаданных победителей в 2098
играх, причём сила команды берётся на НАЧАЛО кубка и по ходу не уточняется.

Что в модели и с каким знаком (на одно стандартное отклонение признака):

    доля золота в матче      +0.042   главный фактор
    сила лучшего игрока      +0.024   рейтинг Эло сильнейшего в составе
    доигрываемость           -0.024   да, МИНУС, см. ниже
    суммарный MMR            +0.019

Чего в модели НЕТ и почему. Винрейт игроков и роли предсказывают успех по
отдельности (+0.29 и +0.20), но рядом с долей золота перестают добавлять:
экономика и так отражает, кто играет ядро и насколько успешно. Их мы всё равно
показываем на странице - это полезный контекст, просто не вес в формуле.

Минус у доигрываемости устойчив по всем кубкам. Объяснение, видимо, такое:
полностью отыгрывают свои игры прежде всего те, кого не меняют, а меняют в
командах, которые борются за место. Мы оставили признак как посчитался, а не
подкрутили под интуицию.

Пока кубок идёт, к оценке состава подмешиваются его собственные результаты:
прогноз весит PRIOR_GAMES виртуальных игр, дальше настоящие победы перевешивают
его сами. Это самая большая прибавка за всё время работы над моделью - угадывание
победителя матча растёт с 59.4% до 64.4%, а у команд, сыгравших хотя бы пять игр,
до 65.9% (logloss 0.665 -> 0.639).

Что ещё пробовали и что НЕ помогло (проверено на тех же данных):
рейтинг Брэдли-Терри вместо Эло, затухание старых игр с полураспадом 30-120
дней, доля золота с поправкой на место игрока по фарму внутри команды, KDA,
доля саппортов в составе, средний Эло вместо максимума. Ни один вариант не
обошёл нынешний набор. Максимум по команде вместо суммы - не наша выдумка: то
же самое получено в работе про агрегацию рейтингов в командных играх
(arxiv 2106.11397), где MAX побеждает SUM и MIN.

Предсказание - ожидаемая доля побед команды в её сериях, а не вероятность
выиграть кубок. Пересчитать веса: tools/forecast_*.py.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Match, MatchPlayer, Player, SubstitutionEvent, Team

# Насколько сильно винрейт новичка тянется к 50%: 25 виртуальных игр. Человек с
# тремя победами из трёх не должен выглядеть сильнее того, кто выиграл 60 из 100.
WINRATE_PRIOR_GAMES = 25

# Шаг рейтинга Эло. 24 - из бэктеста: при 12 рейтинг не успевает разойтись за
# семь кубков, при 48 скачет от одной серии.
ELO_K = 24
ELO_START = 1500.0

# Веса из регрессии. Признаки нормируются ВНУТРИ кубка - модель сравнивает
# команды друг с другом, а не с абсолютной шкалой.
WEIGHTS = {
    "gold": 0.0416,
    "elo_max": 0.0242,
    "durability": -0.0236,
    "mmr": 0.0185,
}

# Сколько виртуальных игр весит прогноз по составу, пока кубок идёт. 10 -
# из проверки: при 3 текущий счёт слишком дёргает оценку, при 40 он почти не
# влияет, а в середине кубка качество одинаковое.
PRIOR_GAMES = 10

# Замены - главная особенность миксера: состав меняется по ходу кубка, и в
# 1547 матчах из 1929 хотя бы одна команда играла уже не тем составом, что
# раньше. Поэтому прошлая игра засчитывается команде не целиком, а в меру того,
# сколько человек из НЫНЕШНЕГО состава её играли: вес = (совпало/5) ** степень.
# Степень 3 выбрана проверкой: угадывание матчей то же, а logloss падает с
# 0.639 до 0.625 (на матчах после замен - с 0.646 до 0.628), то есть модель
# перестаёт быть самоуверенной насчёт команды, которая половину побед набрала
# другим составом.
ROSTER_OVERLAP_POWER = 3

CORE_ROLES = {"CARRY", "MIDLANER", "OFFLANER"}
ALL_ROLES = ("CARRY", "MIDLANER", "OFFLANER", "SOFT_SUPPORT", "HARD_SUPPORT")


@dataclass
class PlayerForecast:
    account_id: int | None
    name: str
    mmr: float | None
    roles: str | None
    games: int
    wins: int
    win_rate: float | None          # сырой винрейт, для показа
    expected_win_rate: float        # он же, подтянутый к 50%
    gold_share: float | None        # доля золота матча: 1.0 - ровно десятая часть
    elo: float                      # рейтинг по прошлым играм
    durability: float | None        # какую долю игр своей команды обычно отыгрывает
    left_early: int                 # сколько раз выходил из состава по ходу кубка


@dataclass
class TeamForecast:
    team_id: int
    name: str
    total_mmr: float | None
    strength: float                 # ожидаемая доля побед, 0..1 (с учётом игр кубка)
    strength_prior: float = 0.5     # только по составу, без результатов кубка
    results_weight: float = 0.0     # какая доля оценки сейчас идёт от результатов
    counted_games: float = 0.0      # сколько игр зачлось после скидки на замены
    rank: int = 0
    components: dict = field(default_factory=dict)
    players: list[PlayerForecast] = field(default_factory=list)
    # Контекст: в формулу не входит, но на странице показывается.
    squad_win_rate: float = 0.5
    role_slots: float = 0.0
    role_cores: float = 0.0
    missing_roles: list = field(default_factory=list)
    actual_wins: int = 0
    actual_losses: int = 0


def _role_fit(preferences: list[set[str]]) -> tuple[float, float, list[str]]:
    """Можно ли расставить пятёрку по пяти РАЗНЫМ ролям.

    Перебор по пяти игрокам считается мгновенно. Пустой список ролей значит
    «играет что угодно»: так mixer-cup отдаёт тех, кто роли не указал, и
    наказывать их не за что."""
    prefs = [p or set(ALL_ROLES) for p in preferences]
    best = {"filled": 0, "used": frozenset()}

    def walk(i: int, used: frozenset, filled: int) -> None:
        if filled > best["filled"]:
            best["filled"], best["used"] = filled, used
        if i == len(prefs) or filled + (len(prefs) - i) <= best["filled"]:
            return
        for role in prefs[i]:
            if role not in used:
                walk(i + 1, used | {role}, filled + 1)
        walk(i + 1, used, filled)

    walk(0, frozenset(), 0)
    cores = sum(1 for p in prefs if p & CORE_ROLES)
    missing = [r for r in ALL_ROLES if r not in best["used"]]
    return best["filled"] / 5, min(cores, 3) / 3, missing


def _player_history(session: Session, exclude_tournament: int | None) -> dict[int, dict]:
    """Всё, что мы знаем про игроков по ОСТАЛЬНЫМ кубкам: винрейт, доля золота,
    рейтинг Эло, доля отыгранных игр.

    Текущий кубок исключается специально: прогноз должен опираться на прошлое,
    иначе лидер таблицы получит высокий прогноз просто за то, что он лидер.

    Один проход по всем играм в хронологическом порядке - иначе Эло не
    посчитать, он обновляется матч за матчем."""
    scope = [Match.radiant_win.is_not(None), Match.mixer_tournament_id.is_not(None)]
    if exclude_tournament is not None:
        scope.append(Match.mixer_tournament_id != exclude_tournament)

    rows = session.execute(
        select(
            Match.match_id, Match.mixer_tournament_id, Match.start_time, Match.radiant_win,
            MatchPlayer.account_id, MatchPlayer.is_radiant, MatchPlayer.gold_per_min,
        )
        .join(MatchPlayer, MatchPlayer.match_id == Match.match_id)
        .where(*scope)
        .order_by(Match.start_time, Match.match_id)
    ).all()

    lineups: dict[int, list] = defaultdict(list)
    order: list[int] = []
    cup_of: dict[int, int] = {}
    for match_id, cup, _start, radiant_win, account_id, is_radiant, gpm in rows:
        if match_id not in lineups:
            order.append(match_id)
            cup_of[match_id] = cup
        lineups[match_id].append((account_id, bool(is_radiant), gpm, bool(radiant_win)))

    stats: dict[int, dict] = defaultdict(
        lambda: {"games": 0, "wins": 0, "gold": 0.0, "gold_games": 0,
                 "elo": ELO_START, "by_cup": defaultdict(int)})
    for match_id in order:
        line = lineups[match_id]
        radiant = [p for p in line if p[1]]
        dire = [p for p in line if not p[1]]
        radiant_win = line[0][3]
        for account_id, is_radiant, gpm, _ in line:
            s = stats[account_id]
            s["games"] += 1
            s["wins"] += int(is_radiant == radiant_win)
            s["by_cup"][cup_of[match_id]] += 1
        # Доля золота: сколько человек добыл от всей добычи матча, умноженная на
        # 10. Ровно средний игрок получает 1.0. Так сравниваются игроки из
        # коротких и длинных игр, а заодно из разных по силе кубков.
        total_gold = sum(p[2] or 0 for p in line)
        if total_gold:
            for account_id, _, gpm, _ in line:
                s = stats[account_id]
                s["gold"] += (gpm or 0) * 10 / total_gold
                s["gold_games"] += 1
        if len(radiant) == 5 and len(dire) == 5:
            r_elo = sum(stats[p[0]]["elo"] for p in radiant) / 5
            d_elo = sum(stats[p[0]]["elo"] for p in dire) / 5
            expected = 1 / (1 + 10 ** ((d_elo - r_elo) / 400))
            result = 1.0 if radiant_win else 0.0
            for p in radiant:
                stats[p[0]]["elo"] += ELO_K * (result - expected)
            for p in dire:
                stats[p[0]]["elo"] += ELO_K * ((1 - result) - (1 - expected))

    # Сколько игр проводит команда за кубок - медиана по его командам. С ней
    # доля отыгранных игр не зависит от формата: в супермиксере с решафлами
    # команда за неделю играет меньше, чем за весь кубок PARI.
    per_team: dict[int, list[int]] = defaultdict(list)
    for cup, _team, games in session.execute(
        select(Match.mixer_tournament_id, MatchPlayer.team_id, func.count())
        .select_from(MatchPlayer)
        .join(Match, Match.match_id == MatchPlayer.match_id)
        .where(*scope)
        .group_by(Match.mixer_tournament_id, MatchPlayer.team_id)
    ):
        per_team[cup].append(games // 5 or 1)
    full_cup = {}
    for cup, counts in per_team.items():
        counts.sort()
        full_cup[cup] = counts[len(counts) // 2] or 1

    for s in stats.values():
        shares = [min(played / full_cup.get(cup, played or 1), 1.0)
                  for cup, played in s["by_cup"].items()]
        s["durability"] = sum(shares) / len(shares) if shares else None
        s["gold_share"] = s["gold"] / s["gold_games"] if s["gold_games"] else None
    return stats


def _left_early(session: Session, exclude_tournament: int | None) -> dict[str, int]:
    """Сколько раз игрок уходил из состава по ходу кубка - по его нику.

    В журнале замен mixer-cup есть только ник, аккаунта там нет. Ник может
    смениться, и тогда прошлые уходы потеряются: это недооценка риска, но не
    выдумка."""
    scope = [SubstitutionEvent.event_type == "PLAYER_OFF"]
    if exclude_tournament is not None:
        scope.append(SubstitutionEvent.tournament_id != exclude_tournament)
    out: dict[str, int] = defaultdict(int)
    for (nickname,) in session.execute(select(SubstitutionEvent.nickname).where(*scope)):
        if nickname:
            out[nickname.strip().casefold()] += 1
    return out


def forecast_tournament(session: Session, tournament_id: int,
                        week: int | None = None,
                        roster_is_current: bool = True) -> list[TeamForecast]:
    """Сила каждой команды кубка. Список отсортирован, сильнейшие сверху.

    roster_is_current - идёт ли этот кубок прямо сейчас. У прошедшего в базе
    лежит СЕГОДНЯШНИЙ состав команды (так устроены roster_confirmed и
    Team.name), и сравнивать его с пятёрками тех игр бессмысленно: скидка на
    замены тогда обнулила бы весь счёт кубка."""
    team_rows = session.execute(
        select(Team).where(Team.tournament_id == tournament_id)
    ).scalars().all()
    if week is not None:
        team_rows = [t for t in team_rows if t.week_number in (None, week)]
    if not team_rows:
        return []

    roster = defaultdict(list)
    for player in session.execute(
        select(Player).where(
            Player.team_id.in_([t.team_id for t in team_rows]),
            Player.roster_confirmed.is_(True),
        )
    ).scalars():
        roster[player.team_id].append(player)

    history = _player_history(session, tournament_id)
    offs = _left_early(session, tournament_id)

    # Игры кубка с составами: нужны и для счёта, и для веса каждой игры (см.
    # ROSTER_OVERLAP_POWER).
    record = defaultdict(lambda: [0, 0])
    played: dict[int, list] = defaultdict(list)
    scope = [Match.mixer_tournament_id == tournament_id, Match.radiant_win.is_not(None)]
    if week is not None:
        scope.append(Match.week_number == week)
    lineups: dict[tuple, set] = defaultdict(set)
    sides: dict[int, tuple] = {}
    for match_id, radiant_team, dire_team, radiant_win, account_id, is_radiant in session.execute(
        select(Match.match_id, Match.radiant_team_id, Match.dire_team_id, Match.radiant_win,
               MatchPlayer.account_id, MatchPlayer.is_radiant)
        .join(MatchPlayer, MatchPlayer.match_id == Match.match_id)
        .where(*scope)
    ):
        sides[match_id] = (radiant_team, dire_team, radiant_win)
        lineups[(match_id, bool(is_radiant))].add(account_id)
    for match_id, (radiant_team, dire_team, radiant_win) in sides.items():
        for team_id, won, side in ((radiant_team, radiant_win, True),
                                   (dire_team, not radiant_win, False)):
            if team_id is None:
                continue
            record[team_id][0 if won else 1] += 1
            played[team_id].append((bool(won), lineups.get((match_id, side), set())))

    teams: list[TeamForecast] = []
    for team in team_rows:
        players = roster.get(team.team_id, [])
        if not players:
            continue
        forecasts = []
        for p in players:
            s = history.get(p.account_id)
            games = s["games"] if s else 0
            wins = s["wins"] if s else 0
            forecasts.append(PlayerForecast(
                account_id=p.account_id,
                name=p.name or f"account {p.account_id}",
                mmr=p.mmr,
                roles=p.preferred_roles,
                games=games,
                wins=wins,
                win_rate=round(100 * wins / games) if games else None,
                expected_win_rate=(wins + WINRATE_PRIOR_GAMES * 0.5) / (games + WINRATE_PRIOR_GAMES),
                gold_share=s["gold_share"] if s else None,
                elo=s["elo"] if s else ELO_START,
                durability=s["durability"] if s else None,
                left_early=offs.get((p.name or "").strip().casefold(), 0),
            ))
        slots, cores, missing = _role_fit(
            [{r for r in (p.roles or "").split(",") if r} for p in forecasts])
        known_dur = [p.durability for p in forecasts if p.durability is not None]
        known_gold = [p.gold_share for p in forecasts if p.gold_share is not None]
        wins_t, losses_t = record.get(team.team_id, [0, 0])
        teams.append(TeamForecast(
            team_id=team.team_id,
            name=team.name or f"Team {team.team_id}",
            total_mmr=sum(p.mmr for p in forecasts if p.mmr is not None) or None,
            strength=0.0,
            components={
                # 1.0 - доля золота среднего игрока: у состава без истории
                # нет оснований считаться ни жадным, ни скромным.
                "gold": sum(known_gold) / len(known_gold) if known_gold else 1.0,
                "elo_max": max(p.elo for p in forecasts),
                # 0.75 - примерно средняя доля игр по прошлым кубкам.
                "durability": sum(known_dur) / len(known_dur) if known_dur else 0.75,
                "mmr": sum(p.mmr or 0 for p in forecasts),
            },
            players=sorted(forecasts, key=lambda p: -(p.mmr or 0)),
            squad_win_rate=sum(p.expected_win_rate for p in forecasts) / len(forecasts),
            role_slots=slots,
            role_cores=cores,
            missing_roles=missing,
            actual_wins=wins_t,
            actual_losses=losses_t,
        ))

    if not teams:
        return []

    for name, weight in WEIGHTS.items():
        values = [t.components[name] for t in teams]
        mean = sum(values) / len(values)
        spread = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5 or 1.0
        for t in teams:
            z = (t.components[name] - mean) / spread
            t.components[name] = {"value": t.components[name], "z": z, "effect": weight * z}

    for t in teams:
        t.strength_prior = 0.5 + sum(c["effect"] for c in t.components.values())
        # Результаты идущего кубка перевешивают оценку состава по мере того, как
        # их становится больше - но игра, сыгранная другой пятёркой, засчитывается
        # этой команде лишь частично (см. ROSTER_OVERLAP_POWER).
        roster_ids = {p.account_id for p in t.players if p.account_id}
        games = played.get(t.team_id, [])
        # Скидка на замены - только для идущего кубка, см. docstring.
        use_overlap = roster_is_current and bool(roster_ids)
        weighted_wins = weighted_games = 0.0
        for won, lineup in games:
            weight = ((len(lineup & roster_ids) / 5) ** ROSTER_OVERLAP_POWER
                      if use_overlap else 1.0)
            weighted_games += weight
            weighted_wins += weight * won
        t.counted_games = round(weighted_games, 1)
        t.strength = ((t.strength_prior * PRIOR_GAMES + weighted_wins)
                      / (PRIOR_GAMES + weighted_games))
        t.results_weight = weighted_games / (PRIOR_GAMES + weighted_games)
    teams.sort(key=lambda t: -t.strength)
    for i, t in enumerate(teams, start=1):
        t.rank = i
    return teams
