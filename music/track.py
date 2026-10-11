"""Описание одного трека в очереди и одного варианта поиска."""
from __future__ import annotations

import re
from dataclasses import dataclass


def format_duration(seconds: int | None, live: bool = False) -> str:
    """Длительность в формате M:SS или H:MM:SS. Эфир → 'LIVE', неизвестно → '?:??'."""
    if live:
        return "LIVE"
    if not seconds:
        return "?:??"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


_TIME_RE = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+)$")


def parse_time(text: str | None) -> int | None:
    """«90» → 90, «2:30» → 150, «1:02:03» → 3723 (секунды). Непонятное → None."""
    m = _TIME_RE.match((text or "").strip())
    if not m:
        return None
    parts = [int(p) for p in m.groups() if p is not None]
    if any(p >= 60 for p in parts[1:]):
        return None                      # «2:75» — опечатка, а не время
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def short_title(title: str, limit: int = 60) -> str:
    """Название для списков: обрезанное и без квадратных скобок (ломают ссылки в эмбеде)."""
    title = (title or "Unknown").replace("[", "(").replace("]", ")")
    return title if len(title) <= limit else title[:limit - 1] + "…"


@dataclass(slots=True)
class Track:
    """Один трек в очереди.

    webpage_url — страница трека (YouTube/SoundCloud), по ней плеер качает звук.
    Может быть None у треков из Spotify/Last.fm: тогда плеер перед проигрыванием
    сам найдёт их на YouTube Music по `match` (исполнитель, название).
    """

    title: str
    webpage_url: str | None
    duration: int | None          # секунды (None — эфир или ещё неизвестно)
    uploader: str | None
    thumbnail: str | None
    requested_by: str             # кто заказал
    source: str = "YouTube"       # откуда трек: YouTube / YT Music / Spotify / Last.fm…
    match: tuple[str, str] | None = None  # (исполнитель, название) для поиска на YT Music
    live: bool = False            # прямой эфир — играем живым потоком, не качаем
    autoplay: bool = False        # подобран автоплеем (такие треки стоят после заказанных)

    @property
    def duration_str(self) -> str:
        return format_duration(self.duration, self.live)

    @property
    def duration_tag(self) -> str:
        """« `[3:45]`» для сообщений; пусто, если длительность неизвестна (не «?:??»)."""
        return f" `[{self.duration_str}]`" if self.duration or self.live else ""

    @property
    def display_title(self) -> str:
        """Название для чата: у треков из Spotify/Last.fm добавляем исполнителя."""
        if self.uploader and self.source in ("Spotify", "Last.fm") \
                and self.uploader.lower() not in (self.title or "").lower():
            return f"{self.uploader} — {self.title}"
        return self.title

    @property
    def link_title(self) -> str:
        """Название ссылкой (если ссылка уже известна) — для эмбедов."""
        title = short_title(self.title, 200)
        return f"[{title}]({self.webpage_url})" if self.webpage_url else title


@dataclass(slots=True)
class SearchResult:
    """Один вариант из поиска. Поток не получен — его добудет плеер при игре."""

    title: str
    url: str | None               # None у Spotify/Last.fm — найдём на YT Music при игре
    duration: int | None
    uploader: str | None
    source: str = "YouTube"
    thumbnail: str | None = None
    note: str | None = None       # доп. строка в списке (напр. слушатели Last.fm)
    live: bool = False

    @property
    def duration_str(self) -> str:
        return format_duration(self.duration, self.live)

    def to_track(self, requester: str) -> Track:
        return Track(
            title=self.title,
            webpage_url=self.url,
            duration=self.duration,
            uploader=self.uploader,
            thumbnail=self.thumbnail,
            requested_by=requester,
            source=self.source,
            match=None if self.url else (self.uploader or "", self.title),
            live=self.live,
        )
