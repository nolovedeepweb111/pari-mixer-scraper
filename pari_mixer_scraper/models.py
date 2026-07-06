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
    """Raises SQLite's lock-wait timeout so contention between the web
    app's reads and the background collector's writes waits and fails
    loudly instead of the default short wait.

    WAL mode was tried here twice - once against Turso/libSQL, once
    against a plain local file - and both times the app hung hard on
    Render specifically (never reproduced locally under the same
    concurrent load). Whatever the exact mechanism, WAL's reliance on a
    shared-memory (-shm) file via mmap doesn't play well with Render's
    disk. Rather than chase that further, this leans on a generous
    busy_timeout plus the stale-run recovery in app.py: an occasional
    "database is locked" failure is tolerated and self-heals within
    minutes instead of being eliminated outright."""
    if engine.url.get_backend_name() != "sqlite":
        return engine

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=60000")
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


class SubstitutionEvent(Base):
    """A single PLAYER_IN/PLAYER_OFF event from mixer-cup.gg's tournament
    event log, saved permanently on our side - mixer-cup.gg's own history
    for this has been observed to disappear periodically, so this is the
    durable copy. event_id is mixer-cup.gg's own UUID for the event, used
    as the primary key so re-syncing the same event twice is a no-op."""
    __tablename__ = "substitution_events"

    event_id: Mapped[str] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"))
    event_type: Mapped[str]
    nickname: Mapped[str | None]
    # MixerCup's rating for this player as of when we synced the event (not
    # a true historical snapshot at swap time - their API only exposes the
    # player's current rating - but it's captured once and never
    # overwritten, so it stays close to the value at the time of the swap).
    rating: Mapped[float | None]
    occurred_at: Mapped[str]
