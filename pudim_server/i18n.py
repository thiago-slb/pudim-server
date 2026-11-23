"""User-visible text the server writes into Jellyfin (playlist names) and into app notices.

``language`` in the config picks the table ("en" or "pt"). Error messages returned by the API are English.
"""

STRINGS = {
    "en": {
        "radar": "Release Radar",
        "radar_overview": "New releases from the artists you follow. Every Friday.",
        "radar_notice": "{n} new release(s) from artists you follow.",
        "radar_no_follows": "Follow a few artists so the Radar knows what to look for.",
        "radar_nothing": "No new releases from the artists you follow in the last few weeks.",
        "daylist": "daylist",
        "daylist_overview": "Changes through the day with what you usually play at this hour.",
        "daylist_empty": "Play some music so the daylist can learn your taste.",
        "weekdays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "parts": ["late night", "morning", "afternoon", "evening"],
        "daylist_name": "{daylist} · {mood}{weekday} {part}",
        "and": " and ",
        "blend": "Blend · ",
        "blend_overview": "{pct}% taste match. Two accounts in one playlist, refreshed daily.",
        "blend_chat": "I made a Blend for us: {pct}% taste match 🎧",
        "blend_not_enough": "You haven't listened to enough yet for a Blend.",
        "mix": "Weekly Mix",
        "mix_notice_title": "🎛️ {name} of {user}",
        "mix_notice": "{n} songs like your favorites, just for you.",
        "mix_empty": "No favorites or plays yet: play something or mark songs as favorites.",
        "discovery": "Weekly Discovery",
        "jam_invite": "Want to listen together? 🎶",
        "jam_title": "{name}'s Jam",
        "jam_tap": "Tap to join",
        "collab_invite": "I invited you to edit the playlist {title} ✍️",
        "collab_subtitle": "Collaborative playlist",
        "deezer_prefix": "Deezer · ",
        "top_of": "Top tracks of {name}",
        "someone": "Someone",
    },
    "pt": {
        "radar": "Radar de Novidades",
        "radar_overview": "Lançamentos dos artistas que você segue. Toda sexta.",
        "radar_notice": "{n} lançamento(s) dos artistas que você segue.",
        "radar_no_follows": "Siga alguns artistas para o Radar saber o que procurar.",
        "radar_nothing": "Nenhum lançamento novo dos artistas que você segue nas últimas semanas.",
        "daylist": "daylist",
        "daylist_overview": "Muda ao longo do dia com o que você costuma ouvir nesse horário.",
        "daylist_empty": "Ouça umas músicas para a daylist aprender o seu gosto.",
        "weekdays": ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"],
        "parts": ["de madrugada", "de manhã", "à tarde", "à noite"],
        "daylist_name": "{daylist} · {mood}{weekday} {part}",
        "and": " e ",
        "blend": "Blend · ",
        "blend_overview": "{pct}% de afinidade. O gosto das duas contas numa playlist só, atualizada todo dia.",
        "blend_chat": "Criei um Blend nosso: {pct}% de afinidade 🎧",
        "blend_not_enough": "Vocês ainda não ouviram o bastante para um Blend.",
        "mix": "Mistura da semana",
        "mix_notice_title": "🎛️ {name} de {user}",
        "mix_notice": "{n} músicas parecidas com os seus favoritos, só na sua conta.",
        "mix_empty": "Sem favoritos nem músicas tocadas ainda: ouça algo ou marque favoritos.",
        "discovery": "Descobertas da semana",
        "jam_invite": "Bora ouvir junto? 🎶",
        "jam_title": "Jam de {name}",
        "jam_tap": "Toque para entrar",
        "collab_invite": "Te convidei para editar a playlist {title} ✍️",
        "collab_subtitle": "Playlist colaborativa",
        "deezer_prefix": "Deezer · ",
        "top_of": "Mais tocadas de {name}",
        "someone": "Alguém",
    },
}


def t(conf, key, **kw):
    table = STRINGS.get(conf.get("language") or "en", STRINGS["en"])
    val = table.get(key, STRINGS["en"][key])
    return val.format(**kw) if kw and isinstance(val, str) else val
