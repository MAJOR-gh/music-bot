"""Описание одного трека в очереди."""
from __future__ import annotations

from dataclasses import dataclass


def format_duration(seconds: int | None) -> str:
    """Длительность в формате M:SS или H:MM:SS. None/0 → 'LIVE'."""
    if not seconds:
        return "LIVE"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


@dataclass(slots=True)
class Track:
    """Один аудиотрек, готовый к воспроизведению.

    stream_url — прямая ссылка на аудиопоток (получена через yt-dlp, download=False).
    """

    title: str
    stream_url: str
    webpage_url: str
    duration: int | None          # длительность в секундах (None для стримов)
    uploader: str | None
    thumbnail: str | None
    requested_by: str             # имя пользователя, заказавшего трек

    @property
    def duration_str(self) -> str:
        return format_duration(self.duration)


@dataclass(slots=True)
class SearchResult:
    """Один вариант из текстового поиска (поток ещё не получен).

    url ведёт на страницу видео; полный Track с потоком добывается позже,
    только для выбранного пользователем варианта.
    """

    title: str
    url: str
    duration: int | None
    uploader: str | None

    @property
    def duration_str(self) -> str:
        return format_duration(self.duration)
