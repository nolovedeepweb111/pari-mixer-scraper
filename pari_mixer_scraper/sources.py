"""Откуда берутся турниры.

Изначально источник был один - mixer-cup.gg. Летом 2026 организаторы подняли
ВТОРУЮ копию той же платформы под супермиксер WINLINE
(mixer-cup.sportpostproduction.com, лига 20165 на dotabuff). Схема GraphQL у
неё та же самая, а вот нумерация турниров - своя собственная, с единицы: их
первый супермиксер имеет id=2, и это тот же номер, каким на mixer-cup.gg
когда-нибудь назовётся чужой кубок.

Весь сайт опирается на mixer_tournament_id как на глобальный ключ: по нему
собираются адреса, пулы героев, доступ, лидерборд. Поэтому номера источников
разводятся сдвигом: турнир N из источника со сдвигом K хранится в базе как
K + N. Сдвиг делает сам клиент (MixerCupClient(id_offset=...)), так что весь
остальной код продолжает работать с обычными целыми номерами и о втором
источнике даже не знает.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Сколько номеров отведено одному источнику. Нужно только чтобы по номеру
# турнира восстановить, чей он: сдвиг < id < сдвиг + SPAN.
SOURCE_ID_SPAN = 10_000


@dataclass(frozen=True)
class MixerSource:
    key: str
    title: str
    base_url: str
    # Прибавляется к номерам турниров этого источника перед записью в базу.
    id_offset: int
    # Лиги Dota, в которых играются его матчи.
    league_ids: tuple[int, ...]
    # Приставка в адресе: "PARI Mixer Cup #3" -> /mixercup3, а
    # "WINLINE Super Mixer #1" -> /winline1.
    slug_prefix: str
    # Знает ли эта копия платформы про недели (еженедельные решафлы составов).
    # У api.mixer-cup.gg схема старее: запрос с полями недель отвечает 400 и
    # роняет сбор целиком, поэтому их туда не отправляем.
    has_weeks: bool = False


PARI = MixerSource(
    key="pari",
    title="PARI Mixer Cup",
    base_url="https://api.mixer-cup.gg",
    id_offset=0,
    league_ids=(19924,),
    slug_prefix="mixercup",
    has_weeks=False,
)

WINLINE = MixerSource(
    key="winline",
    title="WINLINE Super Mixer",
    base_url="https://api.mixer-cup.sportpostproduction.com",
    id_offset=20_000,
    league_ids=(20165,),
    slug_prefix="winline",
    has_weeks=True,
)

# Первый в списке - основной: его активный кубок сайт показывает на "/".
DEFAULT_SOURCES: tuple[MixerSource, ...] = (PARI, WINLINE)


def _parse_env(raw: str) -> tuple[MixerSource, ...]:
    """MIXER_SOURCES="ключ|Название|url|сдвиг|лиги|приставка[|недели]" - на случай,
    если поднимут третью копию, а выкатывать код будет некогда."""
    out = []
    for chunk in raw.split(";"):
        parts = [p.strip() for p in chunk.split("|")]
        if len(parts) not in (6, 7):
            continue
        try:
            out.append(MixerSource(
                key=parts[0], title=parts[1], base_url=parts[2],
                id_offset=int(parts[3]),
                league_ids=tuple(int(x) for x in parts[4].replace(",", " ").split()),
                slug_prefix=parts[5].lower(),
                has_weeks=len(parts) > 6 and parts[6].strip() in ("1", "true", "yes"),
            ))
        except ValueError:
            continue
    return tuple(out)


_env = os.environ.get("MIXER_SOURCES", "").strip()
SOURCES: tuple[MixerSource, ...] = _parse_env(_env) if _env else DEFAULT_SOURCES

PRIMARY_SOURCE = SOURCES[0]


def all_league_ids() -> list[int]:
    """Лиги всех источников, без повторов, в порядке объявления."""
    seen: list[int] = []
    for src in SOURCES:
        for lid in src.league_ids:
            if lid not in seen:
                seen.append(lid)
    return seen


def source_for_tournament(tournament_id: int | None) -> MixerSource | None:
    """Чей это турнир - по диапазону, в который попал его номер."""
    if tournament_id is None:
        return None
    for src in SOURCES:
        if src.id_offset <= tournament_id < src.id_offset + SOURCE_ID_SPAN:
            return src
    return None


def source_for_league(league_id: int | None) -> MixerSource | None:
    for src in SOURCES:
        if league_id in src.league_ids:
            return src
    return None
