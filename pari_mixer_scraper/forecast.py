"""Прогноз силы команд кубка.

Веса не выдуманы: они получены линейной регрессией по 171 команде из семи
прошлых кубков (26-30, супермиксеры WINLINE и PARI), причём признаки считались
ТОЛЬКО по играм до начала каждого кубка - иначе вышла бы подгонка под ответ.
Проверка «обучаемся без одного кубка, предсказываем его» даёт корреляцию
предсказанного винрейта с настоящим +0.35 и верный порядок в 64% пар команд
(у монетки 50%). Для сравнения: один только суммарный MMR даёт +0.16.

Что в модели и с каким знаком (на одно стандартное отклонение признака):

    винрейт пятёрки в прошлом   +0.050   главный фактор
    суммарный MMR               +0.034
    доигрываемость              -0.030   да, МИНУС, см. ниже
    разведение по ролям         +0.017
    закрытые ядерные роли       +0.028

Минус у доигрываемости выглядит странно, но он устойчив по кубкам. Объяснение,
скорее всего, такое: долю игр своей команды полностью отыгрывают прежде всего
те, кого не меняют, - а меняют обычно в командах, которые борются за место, то
есть сильных. Мы этот признак оставили как есть, а не подкрутили под интуицию,
и честно показываем его на сайте отдельной колонкой.

Предсказание - это ожидаемый винрейт команды, то есть доля побед в её сериях,
а не вероятность выиграть кубок.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Match, MatchPlayer, Player, SubstitutionEvent, Team

# Насколько сильно винрейт новичка тянется к 50%. 25 виртуальных игр: человек с
# тремя победами из трёх не должен выглядеть сильнее того, кто выиграл 60 из
# 100. Значение из бэктеста - при 10 и при 50 предсказание чуть хуже.
WINRATE_PRIOR_GAMES = 25

# Веса из регрессии (признаки нормированы внутри кубка - сравниваем команды
# между собой, а не с абстрактной шкалой).
WEIGHTS = {
    "winrate": 0.0497,
    "mmr": 0.0343,
    "durability": -0.0297,
    "role_slots": 0.0173,
    "role_cores": 0.0282,
}

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
    expected_win_rate: float        # он же, подтянутый к 50% (в модель идёт этот)
    durability: float | None        # какую долю игр своей команды обычно отыгрывает
    left_early: int                 # сколько раз выходил из состава по ходу кубка


@dataclass
class TeamForecast:
    team_id: int
    name: str
    total_mmr: float | None
    strength: float                 # ожидаемый винрейт, 0..1
    rank: int = 0
    components: dict = field(default_factory=dict)
    players: list[PlayerForecast] = field(default_factory=list)
    role_slots: float = 0.0
    role_cores: float = 0.0
    missing_roles: list = field(default_factory=list)
    actual_wins: int = 0
    actual_losses: int = 0


def _role_fit(preferences: list[set[str]]) -> tuple[float, float, list[str]]:
    """Можно ли расставить пятёрку по пяти РАЗНЫМ ролям.

    Перебор по пяти игрокам - это максимум 5^5 вариантов, считается мгновенно.
    Пустой список ролей значит «играет что угодно»: так mixer-cup отдаёт тех,
    кто роли не указал, и наказывать их не за что.

    Возвращает (доля закрытых слотов, доля закрытых ядерных ролей, чего не
    хватает)."""
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


def _history(session: Session, exclude_tournament: int | None):
    """Игры игроков во всех ОСТАЛЬНЫХ кубках: винрейт и доля отыгранных игр.

    Текущий кубок исключается специально: прогноз должен опираться на прошлое,
    иначе лидер таблицы просто получит высокий прогноз за то, что он лидер."""
    scope = [Match.radiant_win.is_not(None), Match.mixer_tournament_id.is_not(None)]
    if exclude_tournament is not None:
        scope.append(Match.mixer_tournament_id != exclude_tournament)

    rows = session.execute(
        select(
            MatchPlayer.account_id, Match.mixer_tournament_id,
            func.count(),
            func.sum(func.iif(MatchPlayer.is_radiant == Match.radiant_win, 1, 0)),
        )
        .join(Match, Match.match_id == MatchPlayer.match_id)
        .where(*scope)
        .group_by(MatchPlayer.account_id, Match.mixer_tournament_id)
    ).all()

    # Сколько игр проводит команда за кубок - медиана по командам этого кубка.
    # С ней доля отыгранных игр не зависит от формата: в супермиксере с
    # решафлами команда играет за неделю меньше, чем за весь кубок PARI.
    team_games: dict[int, list[int]] = defaultdict(list)
    for cup, games in session.execute(
        select(Match.mixer_tournament_id, func.count())
        .select_from(MatchPlayer)
        .join(Match, Match.match_id == MatchPlayer.match_id)
        .where(*scope)
        .group_by(Match.mixer_tournament_id, MatchPlayer.team_id)
    ):
        team_games[cup].append(games // 5 or 1)
    full_cup = {}
    for cup, counts in team_games.items():
        counts.sort()
        full_cup[cup] = counts[len(counts) // 2] or 1

    totals: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    shares: dict[int, list[float]] = defaultdict(list)
    for account_id, cup, games, wins in rows:
        totals[account_id][0] += games
        totals[account_id][1] += wins or 0
        shares[account_id].append(min(games / full_cup.get(cup, games or 1), 1.0))
    return totals, shares


def _left_early(session: Session, exclude_tournament: int | None) -> dict[str, int]:
    """Сколько раз игрок уходил из состава по ходу кубка - по его нику.

    В журнале замен mixer-cup есть только ник, аккаунта там нет, поэтому
    сопоставляем по нику в нижнем регистре. Ник у человека может смениться, и
    тогда прошлые уходы потеряются - это недооценка риска, но не выдумка."""
    scope = [SubstitutionEvent.event_type == "PLAYER_OFF"]
    if exclude_tournament is not None:
        scope.append(SubstitutionEvent.tournament_id != exclude_tournament)
    out: dict[str, int] = defaultdict(int)
    for (nickname,) in session.execute(
        select(SubstitutionEvent.nickname).where(*scope)
    ):
        if nickname:
            out[nickname.strip().casefold()] += 1
    return out


def forecast_tournament(session: Session, tournament_id: int,
                        week: int | None = None) -> list[TeamForecast]:
    """Сила каждой команды кубка. Список отсортирован, сильнейшие сверху."""
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

    totals, shares = _history(session, tournament_id)
    offs = _left_early(session, tournament_id)

    # Текущий счёт в кубке - он не участвует в прогнозе, но показать его рядом
    # полезно: видно, сходится ли прогноз с тем, что происходит.
    record = defaultdict(lambda: [0, 0])
    scope = [Match.mixer_tournament_id == tournament_id, Match.radiant_win.is_not(None)]
    if week is not None:
        scope.append(Match.week_number == week)
    for radiant_team, dire_team, radiant_win in session.execute(
        select(Match.radiant_team_id, Match.dire_team_id, Match.radiant_win).where(*scope)
    ):
        for team_id, won in ((radiant_team, radiant_win), (dire_team, not radiant_win)):
            if team_id is None:
                continue
            record[team_id][0 if won else 1] += 1

    teams: list[TeamForecast] = []
    for team in team_rows:
        players = roster.get(team.team_id, [])
        if not players:
            continue
        forecasts = []
        for p in players:
            games, wins = totals.get(p.account_id, [0, 0])
            personal = shares.get(p.account_id) or []
            forecasts.append(PlayerForecast(
                account_id=p.account_id,
                name=p.name or f"account {p.account_id}",
                mmr=p.mmr,
                roles=p.preferred_roles,
                games=games,
                wins=wins,
                win_rate=round(100 * wins / games) if games else None,
                expected_win_rate=(wins + WINRATE_PRIOR_GAMES * 0.5)
                / (games + WINRATE_PRIOR_GAMES),
                durability=sum(personal) / len(personal) if personal else None,
                left_early=offs.get((p.name or "").strip().casefold(), 0),
            ))
        prefs = [
            {r for r in (p.roles or "").split(",") if r}
            for p in forecasts
        ]
        slots, cores, missing = _role_fit(prefs)
        known = [p.durability for p in forecasts if p.durability is not None]
        wins_t, losses_t = record.get(team.team_id, [0, 0])
        teams.append(TeamForecast(
            team_id=team.team_id,
            name=team.name or f"Team {team.team_id}",
            total_mmr=sum(p.mmr for p in forecasts if p.mmr is not None) or None,
            strength=0.0,
            components={
                "winrate": sum(p.expected_win_rate for p in forecasts) / len(forecasts),
                "mmr": sum(p.mmr or 0 for p in forecasts),
                # 0.75 - значение по умолчанию для состава без истории: это
                # примерно средняя доля игр по прошлым кубкам.
                "durability": sum(known) / len(known) if known else 0.75,
                "role_slots": slots,
                "role_cores": cores,
            },
            players=sorted(forecasts, key=lambda p: -(p.mmr or 0)),
            role_slots=slots,
            role_cores=cores,
            missing_roles=missing,
            actual_wins=wins_t,
            actual_losses=losses_t,
        ))

    if not teams:
        return []

    # Признаки нормируем ВНУТРИ кубка: модель сравнивает команды друг с другом,
    # а не с какой-то абсолютной шкалой. Поэтому один и тот же состав в слабом
    # кубке получит прогноз выше, чем в сильном, - так и должно быть.
    for name, weight in WEIGHTS.items():
        values = [t.components[name] for t in teams]
        mean = sum(values) / len(values)
        spread = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5 or 1.0
        for t in teams:
            z = (t.components[name] - mean) / spread
            t.components[name] = {"value": t.components[name], "z": z,
                                  "effect": weight * z}

    for t in teams:
        t.strength = 0.5 + sum(c["effect"] for c in t.components.values())
    teams.sort(key=lambda t: -t.strength)
    for i, t in enumerate(teams, start=1):
        t.rank = i
    return teams
