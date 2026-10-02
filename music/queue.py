"""Очередь треков для одного сервера (guild)."""
from __future__ import annotations

import asyncio
from collections import deque
from enum import IntEnum

from .track import Track


class RepeatMode(IntEnum):
    """Режим повтора. OFF → ONE → ALL → OFF по кругу."""

    OFF = 0   # без повтора
    ONE = 1   # повторять текущий трек
    ALL = 2   # повторять всю очередь (трек после проигрыша уходит в конец)

    @property
    def label(self) -> str:
        return {
            RepeatMode.OFF: "выкл",
            RepeatMode.ONE: "один трек",
            RepeatMode.ALL: "вся очередь",
        }[self]

    @property
    def emoji(self) -> str:
        return {RepeatMode.OFF: "🔁", RepeatMode.ONE: "🔂", RepeatMode.ALL: "🔁"}[self]


class GuildMusicState:
    """Состояние воспроизведения и очередь одного сервера Discord.

    Хранит очередь треков, текущий трек и event-флаги для управления
    плеером. Один экземпляр на каждый guild_id.
    """

    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self._queue: deque[Track] = deque()
        self.current: Track | None = None

        # Сигнал плееру, что появился новый трек / текущий доиграл
        self.next_event = asyncio.Event()

        # Защита от запуска нескольких плееров на один guild
        self.player_task: asyncio.Task | None = None

        # Режим повтора и флаг ручного пропуска. skipped=True означает, что трек
        # завершился НЕ сам (/skip, ⏮️, «Стоп») — тогда повтор его не возвращает.
        self.repeat: RepeatMode = RepeatMode.OFF
        self.skipped: bool = False

        # Счётчик «Стопов»/пропусков. Плеер запоминает его перед подготовкой
        # трека (поиск, скачивание — это секунды) и, если за это время нажали
        # «Стоп» или «Пропустить», выбрасывает подготовленное вместо игры.
        self.generation: int = 0

        # Живая панель-плеер: сообщение с эмбедом и кнопками + где оно висит.
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

    def skip_current(self) -> None:
        """Текущий трек — не доигрывать и не повторять (играет он или ещё готовится)."""
        self.skipped = True
        self.generation += 1

    def stop(self) -> None:
        """«Стоп»: очистить очередь и бросить текущий трек."""
        self.clear()
        self.skip_current()

    def cycle_repeat(self) -> RepeatMode:
        """Переключить режим повтора OFF→ONE→ALL→OFF. Вернуть новый режим."""
        self.repeat = RepeatMode((self.repeat + 1) % 3)
        return self.repeat

    @property
    def upcoming(self) -> list[Track]:
        """Список предстоящих треков (копия, безопасно для итерации)."""
        return list(self._queue)

    def __len__(self) -> int:
        return len(self._queue)

    @property
    def is_empty(self) -> bool:
        return not self._queue
