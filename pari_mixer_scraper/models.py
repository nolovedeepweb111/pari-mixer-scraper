from __future__ import annotations

import os

from sqlalchemy import Engine, ForeignKey, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def build_engine(db_path: str) -> Engine:
    """Turso (libSQL) if TURSO_DATABASE_URL/TURSO_AUTH_TOKEN are set -
    that's a real hosted database, so match data survives redeploys and
    restarts instead of being wiped by free-tier ephemeral disks. Falls
    back to a plain local SQLite file (unchanged local-dev behavior) when
    they're not set.

    The auth token has to go through connect_args (as auth_token, not a
    URL query param) - the libsql dialect's create_connect_args() only
    recognizes a fixed allowlist of sqlite3-style query params and passes
    everything else straight to the server, so an authToken=... query
    param silently never reaches the driver and the connection gets
    rejected as unauthorized."""
    from sqlalchemy import create_engine

    turso_url = os.environ.get("TURSO_DATABASE_URL")
    turso_token = os.environ.get("TURSO_AUTH_TOKEN")
    if turso_url and turso_token:
        hostname = turso_url.removeprefix("libsql://")
        return create_engine(
            f"sqlite+libsql://{hostname}/?secure=true",
            connect_args={"auth_token": turso_token},
        )
    return create_engine(f"sqlite:///{db_path}")


def configure_sqlite(engine: Engine) -> Engine:
    """Raises SQLite's lock-wait timeout so contention between the web
    app's reads and the background collector's writes waits and fails
    loudly instead of the default short wait. Local-file SQLite only -
    Turso/libSQL handles its own concurrency and doesn't need this pragma
    (and may not support it the same way over the wire)."""
    if engine.url.get_backend_name() != "sqlite" or engine.url.get_driver_name() == "libsql":
        return engine

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
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
