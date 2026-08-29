from __future__ import annotations

from collections import Counter
from typing import TypedDict

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .models import Hero, Match, MatchDraftEntry, MatchPlayer

# Насколько соотношение у сокомандника должно превосходить наше, чтобы отдать
# герой ему. Не 1.0: перевес в пару процентов ничего не значит и просто отбирал
# бы у игрока его же героя.
RIVAL_MARGIN = 1.25


class TeamStats(TypedDict):
    games: int
    decided: int
    wins: int
    win_rate: int | None
    top_picks: list[tuple[str, int]]
    signature_heroes: list[tuple[str, int, int, int]]  # hero, wins, games, win_rate
    first_picks: list[tuple[str, int]]
    drafts_available: int
    enemy_bans: list[tuple[str, int]]
    own_bans: list[tuple[str, int]]


def compute_team_stats(session: Session, team_id: int,
                       tournament_id: int | None = None) -> TeamStats:
    # Scope to the team's own tournament: a steam team_id can be reused
    # across tournaments sharing a dotabuff league, so without this the
    # stats mix both. None means no scoping (unlinked team).
    tour_filter = (Match.mixer_tournament_id == tournament_id) if tournament_id is not None else True

    matches = session.execute(
        select(Match.match_id, Match.radiant_team_id, Match.dire_team_id, Match.radiant_win)
        .where((Match.radiant_team_id == team_id) | (Match.dire_team_id == team_id), tour_filter)
        .order_by(Match.start_time)
    ).all()

    games = len(matches)
    wins = decided = 0
    for match_id, radiant_team_id, dire_team_id, radiant_win in matches:
        if radiant_win is None:
            continue
        decided += 1
        is_radiant = radiant_team_id == team_id
        if radiant_win == is_radiant:
            wins += 1

    top_picks = session.execute(
        select(Hero.localized_name, func.count())
        .join(MatchPlayer, MatchPlayer.hero_id == Hero.hero_id)
        .join(Match, Match.match_id == MatchPlayer.match_id)
        .where(MatchPlayer.team_id == team_id, tour_filter)
        .group_by(Hero.hero_id)
        .order_by(func.count().desc())
        .limit(5)
    ).all()

    decided_case = case((Match.radiant_win.is_not(None), 1), else_=0)
    won_case = case((MatchPlayer.is_radiant == Match.radiant_win, 1), else_=0)
    hero_wl = session.execute(
        select(Hero.localized_name, func.sum(won_case), func.sum(decided_case))
        .join(MatchPlayer, MatchPlayer.hero_id == Hero.hero_id)
        .join(Match, Match.match_id == MatchPlayer.match_id)
        .where(MatchPlayer.team_id == team_id, tour_filter)
        .group_by(Hero.hero_id)
    ).all()
    signature = [
        (hero, w, d, round(100 * w / d))
        for hero, w, d in hero_wl
        if d and d >= 2
    ]
    signature.sort(key=lambda x: (-x[3], -x[2]))

    first_pick_names: list[str] = []
    enemy_ban_names: list[str] = []
    own_ban_names: list[str] = []
    for match_id, radiant_team_id, dire_team_id, _ in matches:
        opponent_id = dire_team_id if radiant_team_id == team_id else radiant_team_id

        first_pick = session.execute(
            select(Hero.localized_name)
            .join(MatchDraftEntry, MatchDraftEntry.hero_id == Hero.hero_id)
            .where(
                MatchDraftEntry.match_id == match_id,
                MatchDraftEntry.team_id == team_id,
                MatchDraftEntry.is_pick.is_(True),
            )
            .order_by(MatchDraftEntry.order_num)
            .limit(1)
        ).scalar_one_or_none()
        if first_pick:
            first_pick_names.append(first_pick)

        own_bans = session.execute(
            select(Hero.localized_name)
            .join(MatchDraftEntry, MatchDraftEntry.hero_id == Hero.hero_id)
            .where(
                MatchDraftEntry.match_id == match_id,
                MatchDraftEntry.team_id == team_id,
                MatchDraftEntry.is_pick.is_(False),
            )
        ).scalars().all()
        own_ban_names.extend(own_bans)

        if opponent_id is not None:
            enemy_bans = session.execute(
                select(Hero.localized_name)
                .join(MatchDraftEntry, MatchDraftEntry.hero_id == Hero.hero_id)
                .where(
                    MatchDraftEntry.match_id == match_id,
                    MatchDraftEntry.team_id == opponent_id,
                    MatchDraftEntry.is_pick.is_(False),
                )
            ).scalars().all()
            enemy_ban_names.extend(enemy_bans)

    return {
        "games": games,
        "decided": decided,
        "wins": wins,
        "win_rate": round(100 * wins / decided) if decided else None,
        "top_picks": [(name, count) for name, count in top_picks],
        "signature_heroes": signature[:3],
        "first_picks": Counter(first_pick_names).most_common(3),
        "drafts_available": len(first_pick_names),
        "enemy_bans": Counter(enemy_ban_names).most_common(5),
        "own_bans": Counter(own_ban_names).most_common(5),
    }


def compute_player_signature_heroes(
    session: Session,
    account_ids: list[int],
    min_games: int = 4,
    min_win_rate: int = 60,
    mixer_tournament_id: int | None = None,
) -> list[dict]:
    """Heroes these players are actually dangerous on: enough games to mean
    something, and a win rate above the bar.

    Counted over EVERY cup by default rather than the one the team is playing.
    A cup that has just started has no games at all, so a per-cup version would
    be an empty block for weeks - and what you want when scouting an opponent
    is what that person is good at, which doesn't reset between cups. Pass
    mixer_tournament_id to narrow it to one.

    "Games" here means games with a known result, since that is what the win
    rate is computed from - a match still missing its result would otherwise
    count towards the threshold while contributing nothing to the rate."""
    if not account_ids:
        return []

    decided_case = case((Match.radiant_win.is_not(None), 1), else_=0)
    won_case = case((MatchPlayer.is_radiant == Match.radiant_win, 1), else_=0)
    query = (
        select(
            MatchPlayer.account_id, Hero.localized_name, Hero.name,
            func.sum(decided_case), func.sum(won_case),
        )
        .join(Hero, Hero.hero_id == MatchPlayer.hero_id)
        .join(Match, Match.match_id == MatchPlayer.match_id)
        .where(MatchPlayer.account_id.in_(account_ids))
        .group_by(MatchPlayer.account_id, MatchPlayer.hero_id)
    )
    if mixer_tournament_id is not None:
        query = query.where(Match.mixer_tournament_id == mixer_tournament_id)

    out: list[dict] = []
    for account_id, hero_name, internal_name, decided, wins in session.execute(query):
        decided, wins = decided or 0, wins or 0
        if decided < min_games:
            continue
        win_rate = round(100 * wins / decided)
        if win_rate <= min_win_rate:
            continue
        out.append({
            "account_id": account_id,
            "hero": hero_name,
            "hero_icon": internal_name,
            "games": decided,
            "wins": wins,
            "win_rate": win_rate,
        })
    # Best first, and a longer sample breaks ties: 5/5 says more than 3/4.
    out.sort(key=lambda h: (-h["win_rate"], -h["games"]))
    return out


class BanContext(TypedDict):
    """Разбор банов по всей базе - общая часть расчёта «кого ему банят».

    Вынесена отдельно, потому что стоит она одинаково для любого игрока
    (перебор всех драфтов и составов, порядка 300 мс на боевой базе), а
    зависит только от данных. Вызывающий считает её раз и переиспользует."""
    total_matches: int
    banned_in_matches: Counter
    games_by_player: Counter
    against_by_player: dict[int, Counter]
    hero_names: dict[int, str]
    hero_keys: dict[int, str]


def build_ban_context(session: Session,
                      mixer_tournament_id: int | None = None) -> BanContext:
    """Каких героев соперники банят ИМЕННО против этого игрока.

    Зачем: сильнейший герой игрока может не попадать в его статистику вовсе -
    именно потому, что его забирают баном. Пример, с которого всё началось:
    Earth Spirit банили в 7 из 10 его игр при обычной частоте банов 17%, а
    сыграть дали трижды. По пулу героев такого не увидеть, по банам - видно.

    Как считаем. Берём матчи игрока, где известен драфт, и смотрим только те
    баны, которые сделала КОМАНДА СОПЕРНИКА - свои баны про соперника, а не про
    него. Частоту сравниваем с тем, как часто этого героя банят вообще:
    Invoker банят почти всегда и это ничего не значит про конкретного человека,
    а Earth Spirit - нет.

    Отдельная забота - сокомандники: бан мог быть нацелен на соседа по составу.
    Разбираем это двумя правилами. Если игрок сам играет этого героя, герой
    остаётся за ним: собственные игры - прямое свидетельство, и неважно, что
    сосед тоже под него попадает (банят обоих). Если же не играет ни разу, то
    отдаём героя сокоманднику, у которого соотношение ЗАМЕТНО выше - иначе
    любой перевес на пару процентов отбирал бы у человека его же героя.

    Работает это благодаря самим миксам: составы тасуются, соседи всё время
    разные, а постоянная величина в его матчах - он сам.

    Это оценка, а не факт: настоящую причину бана знает только тот, кто банил.
    """
    match_filter = [MatchDraftEntry.is_pick.is_(False)]
    drafted_query = select(MatchDraftEntry.match_id).distinct()
    sides_query = select(Match.match_id, Match.radiant_team_id, Match.dire_team_id)
    if mixer_tournament_id is not None:
        drafted_query = drafted_query.join(
            Match, Match.match_id == MatchDraftEntry.match_id
        ).where(Match.mixer_tournament_id == mixer_tournament_id)
        sides_query = sides_query.where(Match.mixer_tournament_id == mixer_tournament_id)

    drafted = {m for (m,) in session.execute(drafted_query)}
    if not drafted:
        return []
    sides = {
        match_id: (radiant, dire)
        for match_id, radiant, dire in session.execute(sides_query)
        if match_id in drafted
    }

    # Кого банили в каждом матче: (матч, команда-жертва) -> {герои}.
    # У записи драфта хранится команда, которая банила, а нас интересует
    # сторона, ПРОТИВ которой бан, - это вторая команда матча.
    banned_against: dict[tuple[int, int], set[int]] = {}
    banned_in_matches: Counter = Counter()
    for match_id, hero_id, banning_team in session.execute(
        select(MatchDraftEntry.match_id, MatchDraftEntry.hero_id, MatchDraftEntry.team_id)
        .where(*match_filter)
    ):
        if match_id not in sides or hero_id is None:
            continue
        radiant, dire = sides[match_id]
        victim = dire if banning_team == radiant else radiant if banning_team == dire else None
        if victim is None:
            continue
        banned_against.setdefault((match_id, victim), set()).add(hero_id)
    for (match_id, _victim), heroes in banned_against.items():
        for hero_id in heroes:
            banned_in_matches[hero_id] += 1

    # Сколько матчей у каждого игрока и сколько раз против него банили героя.
    games_by_player: Counter = Counter()
    against_by_player: dict[int, Counter] = {}
    for match_id, player_id, team_id in session.execute(
        select(MatchPlayer.match_id, MatchPlayer.account_id, MatchPlayer.team_id)
    ):
        if match_id not in sides:
            continue
        games_by_player[player_id] += 1
        for hero_id in banned_against.get((match_id, team_id), ()):
            against_by_player.setdefault(player_id, Counter())[hero_id] += 1

    return BanContext(
        total_matches=len(sides),
        banned_in_matches=banned_in_matches,
        games_by_player=games_by_player,
        against_by_player=against_by_player,
        hero_names=dict(session.execute(select(Hero.hero_id, Hero.localized_name)).all()),
        hero_keys=dict(session.execute(select(Hero.hero_id, Hero.name)).all()),
    )


def compute_targeted_bans(
    session: Session,
    account_id: int,
    min_bans: int = 3,
    min_lift: float = 1.6,
    mixer_tournament_id: int | None = None,
    context: BanContext | None = None,
) -> list[dict]:
    """Каких героев соперники банят именно против этого игрока - см.
    build_ban_context, где описан и сам приём, и его ограничения. context
    передаётся, когда общая часть уже посчитана и переиспользуется."""
    ctx = context if context is not None else build_ban_context(session, mixer_tournament_id)
    total_matches = ctx["total_matches"]
    banned_in_matches = ctx["banned_in_matches"]
    games_by_player = ctx["games_by_player"]
    against_by_player = ctx["against_by_player"]
    if not total_matches:
        return []

    own_games = games_by_player.get(account_id, 0)
    if own_games == 0:
        return []

    def lift_of(player_id: int, hero_id: int) -> float:
        games = games_by_player.get(player_id, 0)
        if not games:
            return 0.0
        base = banned_in_matches.get(hero_id, 0) / total_matches
        if base <= 0:
            return 0.0
        return (against_by_player.get(player_id, Counter()).get(hero_id, 0) / games) / base

    played = dict(session.execute(
        select(MatchPlayer.hero_id, func.count())
        .where(MatchPlayer.account_id == account_id)
        .group_by(MatchPlayer.hero_id)
    ).all())
    hero_names = ctx["hero_names"]
    hero_keys = ctx["hero_keys"]

    out: list[dict] = []
    for hero_id, bans in against_by_player.get(account_id, Counter()).items():
        if bans < min_bans:
            continue
        lift = lift_of(account_id, hero_id)
        if lift < min_lift:
            continue
        # Чужой бан себе не приписываем - но только когда своих игр на герое
        # нет вовсе. Если игрок на нём играет, герой его, даже если сосед по
        # составу попадает под тот же бан.
        if not played.get(hero_id):
            rival_best = max(
                (lift_of(other, hero_id) for other in against_by_player
                 if other != account_id and games_by_player.get(other, 0) >= min_bans),
                default=0.0,
            )
            if rival_best > lift * RIVAL_MARGIN:
                continue
        base = banned_in_matches.get(hero_id, 0) / total_matches
        out.append({
            "hero_id": hero_id,
            "hero": hero_names.get(hero_id, str(hero_id)),
            "hero_key": hero_keys.get(hero_id, ""),
            "bans": bans,
            "games": own_games,
            "ban_rate": round(100 * bans / own_games),
            "base_rate": round(100 * base),
            "lift": round(lift, 1),
            "played": played.get(hero_id, 0),
        })
    out.sort(key=lambda h: (-h["lift"], -h["bans"]))
    return out


def generate_coach_text(team_name: str, stats: TeamStats) -> str:
    if stats["decided"] == 0:
        return "Недостаточно завершённых матчей для анализа."

    wins, decided, win_rate = stats["wins"], stats["decided"], stats["win_rate"]
    sentences = []

    if win_rate == 100:
        sentences.append(
            f"{team_name} — без поражений в {decided} матчах, один из лучших результатов турнира на данный момент."
        )
    elif win_rate >= 75:
        sentences.append(f"{team_name} уверенно проводит турнир: {wins} побед из {decided} ({win_rate}%).")
    elif win_rate >= 50:
        sentences.append(f"{team_name} играет ровно: {wins} побед из {decided} ({win_rate}%).")
    elif win_rate >= 25:
        sentences.append(f"{team_name} испытывает трудности: только {wins} победа(ы) из {decided} ({win_rate}%).")
    elif wins:
        # Below 25% but not winless - the branch under this one claims a clean
        # sheet, which for anything from 1% up is simply false.
        sentences.append(
            f"{team_name} проводит турнир тяжело: {wins} победа(ы) из {decided} ({win_rate}%) — "
            "стоит пересмотреть подход к драфту."
        )
    else:
        sentences.append(
            f"{team_name} пока не одержали ни одной победы в {decided} матчах — стоит пересмотреть подход к драфту."
        )

    if stats["signature_heroes"]:
        hero, w, g, wr = stats["signature_heroes"][0]
        if wr >= 67:
            sentences.append(f"Явный козырь — {hero} ({w}/{g}, {wr}% WR), стоит держать в приоритете на драфте.")

    if stats["first_picks"]:
        hero, count = stats["first_picks"][0]
        total = stats["drafts_available"]
        if count >= 2 and total:
            sentences.append(
                f"На первый пик чаще всего идёт {hero} ({count} из {total} драфтов) — "
                "предсказуемый паттерн, соперники могут готовить контрпик."
            )
        else:
            sentences.append("Явного паттерна первого пика не прослеживается — драфт вариативный.")

    if stats["enemy_bans"]:
        top_bans = [h for h, c in stats["enemy_bans"][:3] if c >= 2]
        if top_bans:
            sentences.append(f"Соперники чаще всего банят {', '.join(top_bans)} — считают это главной угрозой команды.")

    return " ".join(sentences)
