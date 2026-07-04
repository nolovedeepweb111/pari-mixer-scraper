"""Manual roster corrections.

MixerCup links a player to their team by the Steam account registered on
mixer-cup.gg (see mixercup_client.steam_account_id_from_avatar_url). That
breaks when someone plays a match on a different Steam account than the one
they registered with (smurf/alt account) - automated account_id matching
can't detect that, so confirmed cases are recorded here by hand.

Maps the Dota account_id actually seen playing -> {team_id, mmr} for the
registered roster slot they actually occupy (mmr is the rating of that
registered slot on mixer-cup.gg, since the playing account's own rating
isn't the one mixer-cup balanced the team around).
"""

MANUAL_ROSTER_OVERRIDES: dict[int, dict] = {}
