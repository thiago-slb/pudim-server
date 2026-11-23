# Pudim server

The optional companion server for **[Pudim](https://github.com/thiago-slb/pudim-music)**, a music player for
Jellyfin. Pudim is a complete Jellyfin client on its own; point it at a Pudim server ("extras server" on the
login screen) and it unlocks the social and "made for you" features below.

The server speaks the `/api/player/*` HTTP API, signs people in with **the same Jellyfin access token** the app
already has (it asks your Jellyfin server whose token it is), and keeps its own small state (SQLite and JSON) in
a data directory. It never sees passwords.

## What it enables in the app

| Feature | Needs |
|---|---|
| Chat between accounts of the same Jellyfin server, sharing songs, albums, artists and playlists | – |
| Profile display name, renaming your Jellyfin user name | – |
| **Jam**: listen together on one shared queue (host controls, guests add songs and send commands) | – |
| **Collaborative playlists** ("added by" on each song, invites in the chat) | Jellyfin 10.9+ |
| **Blend**: a shared playlist for two accounts with a taste match score | – |
| **Release Radar** (Fridays) and **daylist** (changes with the time of day) | play history (see Recap) |
| **Weekly Mix**: favorites + most played + similar-sounding songs | [AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) |
| **Recap** (your year in music), including importing your Spotify streaming history | Jellyfin *Playback Reporting* plugin recommended |
| **Catalog**: search/browse artists, albums and playlists from the Deezer public catalog, marked with what your library already has; save a catalog playlist into your account with the songs you have | – |
| **Concerts** of the artists you follow (MusicBrainz) and following artists not in your library yet | – |
| **Podcasts**: search, top charts, subscriptions and resume position (Apple podcast directory + RSS) | – |
| **Last.fm** scrobbling and "now playing" | a Last.fm API account |
| In-app **update** notices and downloads for your own app builds | optional `updates_dir` |

Not included (see [Left out](#left-out)): downloading missing music, "Weekly Discovery" of songs that are not in
the library, and playing catalog songs that are not in the library (pluggable).

## Install

Requirements: Python 3.10+ and a Jellyfin server. Create a Jellyfin API key in *Dashboard → API Keys*; the
server uses it to read the library and to write the generated playlists into each account (privately).

### pip / venv

```sh
git clone https://github.com/thiago-slb/pudim-server.git
cd pudim-server
python3 -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install .
cp config.example.json config.json   # then edit it
pudim-server                          # or: python -m pudim_server --config config.json
```

It listens on `127.0.0.1:8097` by default. Any WSGI server works too (`waitress-serve --port 8097 wsgi:app`),
but run **one process** (use threads, not multiple workers): Jam sessions and some caches live in memory.

### Docker / docker compose

```sh
cp .env.example .env    # set PUDIM_JELLYFIN_URL and PUDIM_JELLYFIN_API_KEY
docker compose up -d --build
```

State goes to `./data` (mounted at `/data`). The compose file publishes the port on `127.0.0.1` only; put a
reverse proxy in front of it. If Jellyfin runs in another compose project, join its network and use its service
name in `PUDIM_JELLYFIN_URL`.

### systemd

An example unit is in [`deploy/pudim-server.service`](deploy/pudim-server.service) (install into
`/srv/pudim-server` with a venv, run as a dedicated `pudim` user, data dir writable, everything else read-only).

## Configuration

Settings come from `config.json` (path from `--config` or `$PUDIM_CONFIG`, default `./config.json`) and from
environment variables named `PUDIM_<KEY>`; the environment wins. Lists are JSON in the environment.

| Key | Default | Description |
|---|---|---|
| `jellyfin_url` | – (required) | Jellyfin base URL as seen **from this server**, e.g. `http://127.0.0.1:8096` |
| `jellyfin_api_key` | – (required) | Jellyfin API key (Dashboard → API Keys). Keep it secret |
| `data_dir` | `./data` | Where the databases and state files live |
| `language` | `en` | Language of generated playlist names and notices: `en` or `pt` |
| `timezone` | system | IANA zone (e.g. `Europe/Lisbon`) used when importing Spotify history |
| `host` / `port` | `127.0.0.1` / `8097` | Listen address |
| `threads` | `16` | Waitress threads (the app polls; keep this comfortably above your number of users) |
| `background_jobs` | `true` | Library index refresh, hourly play collection, Radar/daylist/Weekly Mix/Blend schedules |
| `audiomuse_url` | empty | AudioMuse-AI base URL; enables the Weekly Mix and adds similar songs to daylist and Blend |
| `lastfm_api_key` / `lastfm_api_secret` | empty | From <https://www.last.fm/api/account/create>; enables scrobbling |
| `updates_dir` | empty | Folder with `pudim-version.json`, `pudimmusic.apk`, `pudimmusic-setup.exe` (see `extras.py`) |
| `catalog_stream_command` | `[]` | External resolver for playing catalog songs not in the library (see below) |
| `podcast_country` | `US` | Two-letter country for the podcast directory and charts |
| `contact` | project URL | Contact put in the User-Agent sent to MusicBrainz (they ask for one) |

### Data directory

| File | Contents |
|---|---|
| `pudim.db` | profiles, chat messages, collaborative playlists, Last.fm sessions, podcast subscriptions and progress, followed artists, notices |
| `plays.db` | play history used by Recap, daylist and Blend |
| `mixes.json` | Radar / daylist / Weekly Mix / Blend state |
| `shows.json` | MusicBrainz concert cache |
| `images/`, `stream-cache/` | caches, safe to delete |

Back up `pudim.db` and `plays.db`; everything else can be rebuilt.

### Play history (Recap, daylist, Blend)

Install Jellyfin's **Playback Reporting** plugin. Every hour the server copies new plays from it into `plays.db`
(with the artist/album/genres at that moment). On first run it also imports Jellyfin's existing play counts
once, dated at each track's last play. Without the plugin, only those counts are available.

### Catalog playback (optional, pluggable)

Catalog items not in your library can be played if you provide a resolver. `catalog_stream_command` is the
command as a list; the server appends four arguments, `artist title duration_seconds cache_dir`, and expects the
**last line of stdout** to be JSON: `{"ok": true, "path": "<audio file inside cache_dir>"}` or
`{"ok": false, "error": "..."}`. The file is served to the app through a random, short-lived URL and deleted
after 6 hours. No resolver is bundled; make sure whatever you plug in respects the terms of the source you use.

## Reverse proxy and HTTPS

The app sends a Jellyfin access token on every request, so **serve it over HTTPS** whenever it is reachable from
outside your LAN. Proxy everything to the server (it can live under a path prefix; the app's "extras server" is
the base URL). Allow large request bodies for the Spotify history upload (up to 300 MB) and don't buffer
responses for `/api/player/stream/`. Example for Caddy:

```
music-extras.example.com {
    reverse_proxy 127.0.0.1:8097
}
```

nginx: `client_max_body_size 300m;` and `proxy_buffering off;` in the location that proxies to the server.

## API overview

All routes except `/healthz` and `/api/player/stream/<token>` need `Authorization: MediaBrowser Token="<jellyfin
access token>"` (or `X-Emby-Token`). Errors are JSON `{"error": "message"}`; the app shows the message.

| Area | Routes |
|---|---|
| Server | `GET /healthz`, `GET /api/player/server` (version and enabled features) |
| Catalog | `GET search?q=`, `GET artist?id=\|name=`, `GET album?id=`, `GET playlist?id=`, `GET featured`, `POST playlist/save` |
| Catalog playback | `GET play/<deezer_id>` → 302 to `GET stream/<token>` (only with `catalog_stream_command`) |
| Profile and chat | `GET/POST me`, `POST me/username`, `GET contacts`, `GET chats`, `GET/POST chat/<user_id>`, `GET notices?since=` |
| Jam | `GET/POST jam`, `POST jam/<id>/join\|leave\|state\|add\|cmd`, `POST jam/<id>/invite/<user_id>` |
| Collaborative playlists | `GET collab/<pid>`, `POST collab/<pid>/invite/<user_id>`, `DELETE collab/<pid>/member/<user_id>`, `POST collab/<pid>/added` |
| Made for you | `GET made`, `POST made/radar\|daylist`, `POST/DELETE blend/<user_id>`, `GET mixes`, `POST mixes/mistura` |
| Recap | `GET recap?year=`, `GET/POST/DELETE recap/spotify`, `GET recap/img?tipo=capa\|artista&item=&artista=` |
| Concerts and follows | `GET shows`, `GET follows`, `POST/DELETE follows/<deezer_id>` |
| Podcasts | `GET podcasts/search?q=`, `GET podcasts/top`, `GET podcasts/show/<id>?offset=&limit=&q=`, `GET podcasts/subs`, `POST/DELETE podcasts/subs/<id>`, `GET podcasts/latest`, `POST podcasts/progress` |
| Last.fm | `GET/DELETE lastfm`, `POST lastfm/connect`, `POST lastfm/finish`, `POST scrobble` |
| App updates | `GET update`, `GET update/<apk\|windows>` |
| Downloads (not implemented) | `GET jobs` (always empty); `POST download`, `flac`, `improve`, `albums/*`, `jobs/<id>/*` answer 501 |

(Routes in the table are relative to `/api/player/`.) Route and field names, including a few Portuguese ones
such as `mistura`, `descobertas`, `tipo=capa`, are kept for compatibility with the app.

## Security notes

- **Who can use it:** anyone with a valid token for your Jellyfin server. Contacts, chat and Blend are limited to
  the accounts of that server (disabled accounts are hidden). Token lookups are cached for 5 minutes, so a
  revoked token keeps working for up to 5 minutes.
- **The API key** in `config.json` can read and change the whole Jellyfin server. Keep the file readable only by
  the service user (`chmod 600 config.json`) and never commit it (`config.json`, `.env` and `data/` are
  gitignored).
- The server writes playlists into accounts **privately** (`IsPublic: false`). Sharing a playlist in the chat
  grants the recipient read access in Jellyfin; inviting to a collaborative playlist relies on the owner's app
  granting edit access with the owner's own token.
- Chat messages, Last.fm session keys and listening history are stored unencrypted in `data_dir`; protect it like
  any other personal data and tell your users it exists.
- `/api/player/stream/<token>` is unauthenticated by design (players can't always send headers); tokens are
  random, single-file and expire after 6 hours.
- Outbound requests go only to your Jellyfin, AudioMuse (if set), Deezer's public API, MusicBrainz, Apple's
  podcast directory and feeds, and Last.fm (if set).
- Run it behind HTTPS (see above) and keep it bound to `127.0.0.1` or a private network otherwise.

## Left out

- **Downloading missing music** (catalog track/album/artist, quality upgrades, "pick albums"): it depends on a
  separate download pipeline outside this project. The routes exist and answer `501` so the app shows a clear
  message; `/api/player/server` reports `"downloads": false`.
- **Weekly Discovery** (`mixes/descobertas`): songs *outside* the library only make sense with downloads, so it is
  reported as unavailable.
- **Catalog playback** of songs not in the library: pluggable via `catalog_stream_command`, no resolver bundled.

## Development

```sh
pip install -e ".[test]"
pytest
```

Tests run against a fake Jellyfin on a local port; no network access is needed.

## License

[GPL-3.0](LICENSE). The client is [thiago-slb/pudim-music](https://github.com/thiago-slb/pudim-music), also GPL-3.0.
