from __future__ import annotations

from sqlalchemy import Engine, ForeignKey, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def build_engine(db_path: str) -> Engine:
    """Local SQLite file. (Turso/libSQL was tried here as a persistent
    backend, but its Rust driver appears to hold the GIL during network
    I/O badly enough to make the whole app unresponsive mid-collection -
    not worth it. Back to a plain local file; see git history if
    revisiting this.)"""
    from sqlalchemy import create_engine

    return create_engine(f"sqlite:///{db_path}")


def configure_sqlite(engine: Engine) -> Engine:
    """Enables WAL mode so the web app's reads (including external uptime
    pings) and the background collector's writes don't lock each other out
    - the default rollback-journal mode serializes readers and writers, and
    under real traffic that surfaced as outright "database is locked"
    errors that aborted a whole collection mid-run. (WAL was tried and
    reverted once before, but that was against Turso/libSQL - a remote
    database over HTTP, a completely different mechanism whose Rust driver
    turned out to be the actual problem. Plain local-file SQLite WAL is
    the standard, well-supported case and was verified under concurrent
    load locally before shipping this.) Also raises the busy-wait timeout
    as a second line of defense for whatever contention remains."""
    if engine.url.get_backend_name() != "sqlite":
        return engine

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


class Hero(Base):
    __tablename__ = "heroes"

    hero_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    localized_name: Mapped[str]


class Team(Base):
    __tablename__ = "teams"

    team_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None]
    # mixer-cup.gg's own UUID for this team, once linked (see
    # collect.link_mixercup_data) - lets the site query mixer-cup.gg
    # directly for things like the team's next scheduled opponent, without
    # re-resolving Steam-team_id <-> MixerCup identity on every request.
    mixer_uuid: Mapped[str | None] = mapped_column(nullable=True)


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
