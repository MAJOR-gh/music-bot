"""Очередь треков для одного сервера (guild)."""
from __future__ import annotations

import asyncio
from collections import deque

from .track import Track


class GuildMusicState:
    """Состояние воспроизведения и очередь одного сервера Discord.

    Хранит очередь треков, текущий трек и event-флаги для управления
    плеером. Один экземпляр на каждый guild_id.
    """

    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self._queue: deque[Track] = deque()
        self.current: Track | None = None

        # Сигнал плееру, что появился новый трек / можно проигрывать дальше
        self.next_event = asyncio.Event()

        # Защита от запуска нескольких плееров на один guild
        self.player_task: asyncio.Task | None = None

        # Громкость 0.0..2.0 (на будущее; FFmpegOpusAudio без PCMVolume)
        self.volume: float = 1.0

        # Живая панель-плеер: сообщение с эмбедом и кнопками + где оно висит.
        # panel_message переиспользуется/редактируется при смене состояния.
        self.panel_message = None      # discord.Message | None
        self.text_channel = None       # discord.abc.Messageable | None

    # ── Операции с очередью ───────────────────────────────────────────────
    def add(self, track: Track) -> None:
        self._queue.append(track)

    def add_front(self, track: Track) -> None:
        """Поставить трек В НАЧАЛО очереди (для рестарта/replay)."""
        self._queue.appendleft(track)

    def get_nowait(self) -> Track | None:
        """Достать следующий трек или None, если очередь пуста."""
        try:
            return self._queue.popleft()
        except IndexError:
            return None

    def clear(self) -> None:
        self._queue.clear()

    def remove(self, index: int) -> Track | None:
        """Удалить трек по индексу (0-based). None если индекс невалиден."""
        if 0 <= index < len(self._queue):
            track = self._queue[index]
            del self._queue[index]
            return track
        return None

    @property
    def upcoming(self) -> list[Track]:
        """Список предстоящих треков (копия, безопасно для итерации)."""
        return list(self._queue)

    def __len__(self) -> int:
        return len(self._queue)

    @property
    def is_empty(self) -> bool:
        return not self._queue
