from __future__ import annotations

from sqlalchemy import Engine, ForeignKey, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def build_engine(db_path: str, poolclass=None) -> Engine:
    """Local SQLite file. (Turso/libSQL was tried here as a persistent
    backend, but its Rust driver appears to hold the GIL during network
    I/O badly enough to make the whole app unresponsive mid-collection -
    not worth it. Back to a plain local file; see git history if
    revisiting this.)

    Pass poolclass=NullPool for the web app's engine: the collector rebuilds
    the DB in a side file and os.replace()s it over db_path, so a pooled
    connection would keep serving the old, now-unlinked file forever. With
    NullPool every request opens a fresh connection to the current db_path
    and sees the freshly promoted data immediately."""
    from sqlalchemy import create_engine

    kwargs = {}
    if poolclass is not None:
        kwargs["poolclass"] = poolclass
    return create_engine(f"sqlite:///{db_path}", **kwargs)


def configure_sqlite(engine: Engine) -> Engine:
    """Tunes SQLite for this app's usage on Render's free tier.

    synchronous=OFF + journal_mode=MEMORY: the collector's commits were
    hanging indefinitely on Render even when writing to a file nothing
    else had open - not lock contention, but the fsync at each commit
    stalling on the free tier's throttled disk (0.1 CPU). This database is
    a disposable cache: it's rebuilt from external APIs on every collection
    pass and wiped on every redeploy anyway, and the collector builds into
    a side file that's only swapped in on success - so durability across a
    crash buys us nothing. Dropping the per-commit fsync (synchronous=OFF)
    and keeping the rollback journal in RAM (journal_mode=MEMORY) makes
    writes memory-speed and removes the stall.

    WAL mode was tried here twice and hung hard on Render both times (its
    -shm mmap doesn't agree with Render's disk), so it's deliberately not
    used. A generous busy_timeout plus the stale-run recovery in app.py
    covers the occasional lock wait."""
    if engine.url.get_backend_name() != "sqlite":
        return engine

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.execute("PRAGMA synchronous=OFF")
        cursor.execute("PRAGMA journal_mode=MEMORY")
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
    # For PLAYER_IN events: the queue position this player held before being
    # picked, from our own QueuedPlayer snapshot (see collect - MixerCup
    # drops a player from the queue the moment they're picked, so this can
    # only come from a snapshot taken beforehand; None for events synced
    # before snapshotting existed or when no snapshot covered the player).
    queue_position: Mapped[int | None]
    occurred_at: Mapped[str]


class QueuedPlayer(Base):
    """Last known state of a player in the tournament's substitute queue
    (mixer-cup.gg participantList, status BID), refreshed on every collect
    pass. Rows are never deleted when a player leaves the queue - keeping
    the last known position is the whole point, since MixerCup removes a
    player from the queue the instant they're substituted into a team,
    which is exactly when we need to know where they were standing."""
    __tablename__ = "queued_players"

    player_uuid: Mapped[str] = mapped_column(primary_key=True)
    nickname: Mapped[str | None]
    rating: Mapped[float | None]
    queue_position: Mapped[int | None]
    updated_at: Mapped[str]
