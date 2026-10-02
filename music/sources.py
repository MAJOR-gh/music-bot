"""Источники поиска: YouTube, YouTube Music, Spotify, Last.fm, SoundCloud.

Звук всегда берётся с YouTube/SoundCloud (через yt-dlp). Spotify и Last.fm дают
только «что за трек» (исполнитель + название) — такой трек плеер перед игрой
находит на YouTube Music функцией find_on_ytmusic().
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass

import aiohttp

import config
from . import ytdl
from .track import SearchResult

logger = logging.getLogger("music_bot.sources")

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

SOURCES = {
    "youtube": "YouTube",
    "ytmusic": "YouTube Music",
    "spotify": "Spotify",
    "lastfm": "Last.fm",
}


class SourceNotConfigured(Exception):
    """Для источника не заданы ключи в .env — текст исключения объясняет, что вписать."""


def youtube_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


# ── YouTube ───────────────────────────────────────────────────────────────────
async def search_youtube(query: str, limit: int) -> list[SearchResult]:
    data = await ytdl.extract(f"ytsearch{limit}:{query}", flat=True, timeout=40)
    out = []
    for e in data.get("entries") or []:
        url = e and (e.get("url") or e.get("webpage_url"))
        if not url:
            continue
        out.append(SearchResult(
            title=e.get("title") or "Unknown", url=url, duration=_int(e.get("duration")),
            uploader=e.get("uploader") or e.get("channel"), source="YouTube",
            thumbnail=ytdl.best_thumbnail(e),
            live=e.get("live_status") == "is_live" or bool(e.get("is_live")),
        ))
    return out


async def youtube_title(url: str) -> str | None:
    """Название ролика через oEmbed — YouTube отдаёт его даже датацентровым IP."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            async with s.get("https://www.youtube.com/oembed",
                             params={"url": url, "format": "json"}) as resp:
                if resp.status == 200:
                    return (await resp.json()).get("title")
    except Exception:  # noqa: BLE001
        pass
    return None


async def soundcloud_alternative(query: str) -> str | None:
    """Ссылка на ту же песню на SoundCloud (запасной вариант, если YouTube отказал)."""
    try:
        data = await ytdl.extract(f"scsearch1:{query}", flat=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        logger.warning("[soundcloud] %r: %s", query, e)
        return None
    entry = ytdl.first_entry(data)
    return entry and (entry.get("url") or entry.get("webpage_url"))


# ── YouTube Music ─────────────────────────────────────────────────────────────
_ytm = None


def _ytmusic():
    global _ytm
    if _ytm is None:
        from ytmusicapi import YTMusic   # импорт тут: модуль нужен только для YT Music
        _ytm = YTMusic()
    return _ytm


def _ytm_to_result(s: dict) -> SearchResult | None:
    vid = s.get("videoId")
    if not vid:
        return None
    artists = ", ".join(a["name"] for a in s.get("artists") or [] if a.get("name"))
    album = (s.get("album") or {}).get("name")
    thumbs = s.get("thumbnails") or []
    return SearchResult(
        title=s.get("title") or "Unknown", url=youtube_url(vid),
        duration=_int(s.get("duration_seconds")), uploader=artists or None,
        source="YT Music", thumbnail=thumbs[-1]["url"] if thumbs else None,
        note=album,
    )


async def search_ytmusic(query: str, limit: int) -> list[SearchResult]:
    found = await asyncio.to_thread(_ytmusic().search, query, filter="songs", limit=limit)
    return [r for r in (_ytm_to_result(s) for s in found) if r][:limit]


_NORM_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_BRACKETS_RE = re.compile(r"[\(\[].*?[\)\]]")


def _norm(text: str) -> str:
    """Для сравнения названий: без скобок (Remastered…), пунктуации и регистра."""
    text = _BRACKETS_RE.sub(" ", (text or "").lower())
    return " ".join(_NORM_RE.sub(" ", text).split())


def match_score(candidate: SearchResult, artist: str, title: str,
                duration: int | None) -> int:
    """Насколько найденный трек похож на искомый (больше — лучше)."""
    score = 0
    want, got = _norm(title), _norm(candidate.title)
    if want and got and (want in got or got in want):
        score += 2
    want_artist = _norm(artist).split()
    if want_artist and want_artist[0] in _norm(candidate.uploader or ""):
        score += 2
    if duration and candidate.duration:
        diff = abs(duration - candidate.duration)
        score += 2 if diff <= 3 else 1 if diff <= 10 else 0
    return score


async def find_on_ytmusic(artist: str, title: str,
                          duration: int | None = None) -> SearchResult | None:
    """Найти играбельную версию трека (по исполнителю и названию)."""
    query = f"{artist} {title}".strip()
    try:
        found = await search_ytmusic(query, 5)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ytmusic] поиск %r не удался: %s — пробую YouTube", query, e)
        found = []
    if found:
        # max() отдаёт первый из равных — при равных очках побеждает выдача YT Music
        return max(found, key=lambda r: match_score(r, artist, title, duration))
    try:
        found = await search_youtube(f"{artist} - {title}".strip(" -"), 1)
    except Exception as e:  # noqa: BLE001
        logger.warning("[youtube] поиск %r не удался: %s", query, e)
        return None
    return found[0] if found else None


# ── Spotify ───────────────────────────────────────────────────────────────────
SPOTIFY_LINK_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?|spotify:)(track|album|playlist)[/:]([A-Za-z0-9]+)"
)


@dataclass(slots=True)
class SpotifyTrack:
    title: str
    artist: str
    duration: int | None

    def to_result(self) -> SearchResult:
        return SearchResult(title=self.title, url=None, duration=self.duration,
                            uploader=self.artist, source="Spotify")


def parse_spotify_link(text: str) -> tuple[str, str] | None:
    """('track'|'album'|'playlist', id) или None."""
    m = SPOTIFY_LINK_RE.search(text or "")
    return (m.group(1), m.group(2)) if m else None


def parse_spotify_embed(html: str) -> tuple[str, list[SpotifyTrack]]:
    """Публичная embed-страница Spotify → (название, треки). Ключи не нужны."""
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("на странице Spotify нет данных (изменилась вёрстка?)")
    entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    name = entity.get("name") or entity.get("title") or "Spotify"
    tracks = []
    for t in entity.get("trackList") or []:
        if t.get("title"):
            tracks.append(SpotifyTrack(t["title"], t.get("subtitle") or "",
                                       _ms(t.get("duration"))))
    if not tracks and entity.get("type", "track") == "track":
        artist = ", ".join(a.get("name", "") for a in entity.get("artists") or [])
        tracks.append(SpotifyTrack(name, artist, _ms(entity.get("duration"))))
    return name, tracks


async def spotify_link_tracks(kind: str, sid: str) -> tuple[str, list[SpotifyTrack]]:
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, headers=UA) as s:
        async with s.get(f"https://open.spotify.com/embed/{kind}/{sid}") as resp:
            if resp.status != 200:
                raise ValueError(f"Spotify ответил HTTP {resp.status}")
            html = await resp.text()
    name, tracks = parse_spotify_embed(html)
    return name, tracks[:config.MAX_PLAYLIST_TRACKS]


_spotify_token: tuple[str, float] | None = None


async def _spotify_api_token() -> str:
    global _spotify_token
    if not (config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET):
        raise SourceNotConfigured(
            "Поиск по Spotify не настроен: впиши SPOTIFY_CLIENT_ID и SPOTIFY_CLIENT_SECRET "
            "в .env (бесплатно: developer.spotify.com → Dashboard → Create app). "
            "Ссылки на Spotify работают и без этого."
        )
    if _spotify_token and _spotify_token[1] > time.monotonic():
        return _spotify_token[0]
    auth = base64.b64encode(
        f"{config.SPOTIFY_CLIENT_ID}:{config.SPOTIFY_CLIENT_SECRET}".encode()).decode()
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as s:
        async with s.post("https://accounts.spotify.com/api/token",
                          data={"grant_type": "client_credentials"},
                          headers={"Authorization": f"Basic {auth}"}) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise ValueError(f"Spotify не выдал токен: {data.get('error_description') or resp.status}")
    _spotify_token = (data["access_token"], time.monotonic() + int(data.get("expires_in", 3600)) - 60)
    return _spotify_token[0]


async def search_spotify(query: str, limit: int) -> list[SearchResult]:
    token = await _spotify_api_token()
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as s:
        async with s.get("https://api.spotify.com/v1/search",
                         params={"q": query, "type": "track", "limit": str(min(limit, 10))},
                         headers={"Authorization": f"Bearer {token}"}) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise ValueError(f"Spotify: {(data.get('error') or {}).get('message') or resp.status}")
    out = []
    for t in (data.get("tracks") or {}).get("items") or []:
        images = (t.get("album") or {}).get("images") or []
        out.append(SearchResult(
            title=t.get("name") or "Unknown", url=None, duration=_ms(t.get("duration_ms")),
            uploader=", ".join(a["name"] for a in t.get("artists") or []),
            source="Spotify", thumbnail=images[0]["url"] if images else None,
            note=(t.get("album") or {}).get("name"),
        ))
    return out


# ── Last.fm ───────────────────────────────────────────────────────────────────
async def search_lastfm(query: str, limit: int) -> list[SearchResult]:
    if not config.LASTFM_API_KEY:
        raise SourceNotConfigured(
            "Поиск по Last.fm не настроен: впиши LASTFM_API_KEY в .env "
            "(бесплатно: last.fm/api/account/create)."
        )
    params = {"method": "track.search", "track": query, "api_key": config.LASTFM_API_KEY,
              "format": "json", "limit": str(limit)}
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as s:
        async with s.get("https://ws.audioscrobbler.com/2.0/", params=params) as resp:
            data = await resp.json(content_type=None)
    if "error" in data:
        raise ValueError(f"Last.fm: {data.get('message') or data['error']}")
    tracks = ((data.get("results") or {}).get("trackmatches") or {}).get("track") or []
    if isinstance(tracks, dict):
        tracks = [tracks]
    out = []
    for t in tracks:
        listeners = _int(t.get("listeners"))
        out.append(SearchResult(
            title=t.get("name") or "Unknown", url=None, duration=None,
            uploader=t.get("artist"), source="Last.fm",
            note=f"{_short(listeners)} слушателей" if listeners else None,
        ))
    return out


async def search(source: str, query: str, limit: int) -> list[SearchResult]:
    """Поиск по выбранному источнику (ключ из SOURCES)."""
    if source == "ytmusic":
        return await search_ytmusic(query, limit)
    if source == "spotify":
        return await search_spotify(query, limit)
    if source == "lastfm":
        return await search_lastfm(query, limit)
    return await search_youtube(query, limit)


# ── мелочи ────────────────────────────────────────────────────────────────────
def _int(v) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _ms(v) -> int | None:
    v = _int(v)
    return round(v / 1000) if v else None


def _short(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)
