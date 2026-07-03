from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from .models import Hero, MatchPlayer, Player, Team, build_database_url

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def report(db_path: str) -> None:
    engine = create_engine(build_database_url(db_path))
    with Session(engine) as session:
        rows = session.execute(
            select(Player.name, Player.account_id, Team.name, Hero.localized_name)
            .join(MatchPlayer, MatchPlayer.account_id == Player.account_id)
            .join(Hero, Hero.hero_id == MatchPlayer.hero_id)
            .outerjoin(Team, Team.team_id == Player.team_id)
        ).all()

    heroes_by_player: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    team_of: dict[str, str | None] = {}

    for name, account_id, team_name, hero_name in rows:
        label = name or f"account {account_id}"
        team_of[label] = team_name
        heroes_by_player[label][hero_name] += 1

    for player in sorted(heroes_by_player):
        team = team_of.get(player)
        header = f"{player} ({team})" if team else player
        print(header)
        for hero, count in sorted(heroes_by_player[player].items(), key=lambda kv: -kv[1]):
            print(f"  {hero}: {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Print a player -> heroes-played summary from the collected data.")
    parser.add_argument("--db", default="tournament.db", help="Path to SQLite database file")
    args = parser.parse_args()
    report(args.db)


if __name__ == "__main__":
    main()
