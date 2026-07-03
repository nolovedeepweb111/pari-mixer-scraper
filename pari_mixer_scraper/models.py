from __future__ import annotations

from sqlalchemy import ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Hero(Base):
    __tablename__ = "heroes"

    hero_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    localized_name: Mapped[str]


class Team(Base):
    __tablename__ = "teams"

    team_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None]


class Player(Base):
    __tablename__ = "players"

    account_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None]
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.team_id"), nullable=True)
    # True only when matched against mixer-cup.gg's current official roster
    # for team_id (see collect.link_mixercup_data). Players who only stood
    # in as a substitute for a match keep this False, so the UI can show
    # just the site's actual lineup instead of every account that ever
    # played under a team_id.
    roster_confirmed: Mapped[bool] = mapped_column(default=False)
    # MixerCup's balancing rating (their notion of MMR), pulled from
    # PlayerNode.rating alongside roster confirmation.
    mmr: Mapped[float | None] = mapped_column(nullable=True)


class Match(Base):
    __tablename__ = "matches"

    match_id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int]
    start_time: Mapped[int | None]
    duration: Mapped[int | None]
    radiant_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.team_id"), nullable=True)
    dire_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.team_id"), nullable=True)
    radiant_win: Mapped[bool | None]


class MatchPlayer(Base):
    __tablename__ = "match_players"

    match_id: Mapped[int] = mapped_column(ForeignKey("matches.match_id"), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("players.account_id"), primary_key=True)
    hero_id: Mapped[int] = mapped_column(ForeignKey("heroes.hero_id"))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.team_id"), nullable=True)
    is_radiant: Mapped[bool]
    kills: Mapped[int | None]
    deaths: Mapped[int | None]
    assists: Mapped[int | None]


class MatchDraftEntry(Base):
    """One pick or ban from a match's captain's-mode draft, in draft order.
    Sourced from OpenDota match detail (picks_bans), since Steam's cheap
    GetMatchHistory bulk endpoint doesn't include draft data."""
    __tablename__ = "match_draft"

    match_id: Mapped[int] = mapped_column(ForeignKey("matches.match_id"), primary_key=True)
    order_num: Mapped[int] = mapped_column(primary_key=True)
    hero_id: Mapped[int] = mapped_column(ForeignKey("heroes.hero_id"))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.team_id"), nullable=True)
    is_pick: Mapped[bool]
