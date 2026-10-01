"""Собирает локальную базу для разработки: бэкап матчей из публичной ветки
data-backup + списки лиг из Steam + связывание с mixer-cup. Без OpenDota:
драфты и KDA для прогноза не нужны, а они и есть дорогая часть."""
import os, sys
sys.path.insert(0, r"D:/claude/pari-mixer-scraper")
os.chdir(r"D:/claude/pari-mixer-scraper")
from dotenv import load_dotenv; load_dotenv(".env")
from sqlalchemy.orm import Session
from pari_mixer_scraper.models import build_engine, ensure_schema
from pari_mixer_scraper.mixercup_client import MixerCupClient
from pari_mixer_scraper.steam_client import SteamClient
from pari_mixer_scraper import collect
from pari_mixer_scraper.sources import SOURCES

db = sys.argv[1]
eng = build_engine(db); ensure_schema(eng)
p = print
with Session(eng) as s:
    backup = collect.restore_state_backup(s, p)
    collect.sync_heroes(s, None, p)
    collect.restore_match_backup(s, backup, p)
    steam = SteamClient(os.environ["STEAM_API_KEY"])
    have = {m for (m,) in s.execute(__import__("sqlalchemy").select(collect.Match.match_id))}
    for src in SOURCES:
        for lid in src.league_ids:
            ms = collect.fetch_all_league_matches(lid, steam, p)
            new = [m for m in ms if m["match_id"] not in have]
            for m in new:
                collect.persist_match(s, lid, m, {}); have.add(m["match_id"])
            s.commit()
            p(f"league {lid}: {len(ms)} total, {len(new)} new")
    for src in SOURCES:
        client = MixerCupClient(base_url=src.base_url, id_offset=src.id_offset, weeks=src.has_weeks)
        try:
            active = client.get_active_tournament()
        except Exception as e:
            p(f"[{src.key}] нет связи: {e}"); continue
        active_id = active["id"] if active else None
        ids = collect._resolve_all_tournament_ids(s, active_id, client, p, source=src)
        for tid in ids:
            collect.link_mixercup_data(s, client, tid, p, apply_rosters=(tid == active_id))
            collect.sync_substitution_history(s, client, tid, p)
        if active_id:
            collect.sync_mixer_teams(s, client, active_id, p)
    s.commit()
