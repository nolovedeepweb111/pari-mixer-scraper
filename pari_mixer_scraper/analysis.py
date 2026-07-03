from __future__ import annotations

from collections import Counter
from typing import TypedDict

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .models import Hero, Match, MatchDraftEntry, MatchPlayer


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


def compute_team_stats(session: Session, team_id: int) -> TeamStats:
    matches = session.execute(
        select(Match.match_id, Match.radiant_team_id, Match.dire_team_id, Match.radiant_win)
        .where((Match.radiant_team_id == team_id) | (Match.dire_team_id == team_id))
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
        .where(MatchPlayer.team_id == team_id)
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
        .where(MatchPlayer.team_id == team_id)
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
