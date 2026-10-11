"""Очередь треков для одного сервера (guild)."""
from __future__ import annotations

import asyncio
import random
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


HISTORY_SIZE = 50


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

        # Что уже играло (для «⏪ Назад» и чтобы автоплей не повторялся).
        # Трек попадает сюда, когда НАЧАЛ играть; последний — текущий.
        self.history: deque[Track] = deque(maxlen=HISTORY_SIZE)

        # Автоплей: очередь кончилась — подобрать похожий трек (Last.fm / YT Music).
        # halted — нажали «Стоп»: автоплей молчит, пока кто-нибудь не закажет трек.
        self.autoplay: bool = False
        self.halted: bool = False

        # Живая панель-плеер: сообщение с эмбедом и кнопками + где оно висит.
        self.panel_message = None      # discord.Message | None
        self.text_channel = None       # discord.abc.Messageable | None

    # ── Операции с очередью ───────────────────────────────────────────────
    def add(self, track: Track) -> None:
        """В конец очереди. Заказанный трек встаёт ПЕРЕД подобранными автоплеем."""
        if track.autoplay:
            self._queue.append(track)
            return
        self.halted = False
        for i, queued in enumerate(self._queue):
            if queued.autoplay:
                self._queue.insert(i, track)
                return
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
        """«Стоп»: очистить очередь и бросить текущий трек (автоплей тоже молчит)."""
        self.clear()
        self.skip_current()
        self.halted = True

    def record_started(self, track: Track) -> None:
        """Трек начал играть — в историю (повтор того же трека не дублируем)."""
        if not self.history or self.history[-1] is not track:
            self.history.append(track)

    def shuffle(self) -> int:
        """Перемешать заказанные треки (автоплей остаётся в конце). → сколько перемешано."""
        own = [t for t in self._queue if not t.autoplay]
        auto = [t for t in self._queue if t.autoplay]
        random.shuffle(own)
        self._queue = deque(own + auto)
        return len(own)

    def remove(self, index: int) -> Track | None:
        """Убрать трек №index (с 1, как в /queue). None — нет такого номера."""
        if not 1 <= index <= len(self._queue):
            return None
        track = self._queue[index - 1]
        del self._queue[index - 1]
        return track

    def move(self, src: int, dst: int) -> Track | None:
        """Переставить трек №src на место №dst (номера с 1; dst за краем — в конец/начало)."""
        if not 1 <= src <= len(self._queue):
            return None
        track = self._queue[src - 1]
        del self._queue[src - 1]
        dst = min(max(dst, 1), len(self._queue) + 1)
        self._queue.insert(dst - 1, track)
        return track

    def skip_to(self, index: int) -> Track | None:
        """Сделать трек №index следующим: всё перед ним выкинуть
        (при «повторе всей очереди» — переложить в конец). None — нет такого номера."""
        if not 1 <= index <= len(self._queue):
            return None
        for _ in range(index - 1):
            dropped = self._queue.popleft()
            if self.repeat is RepeatMode.ALL:
                self._queue.append(dropped)
        return self._queue[0]

    def drop_autoplay(self) -> int:
        """Убрать из очереди треки автоплея (его выключили). → сколько убрано."""
        before = len(self._queue)
        self._queue = deque(t for t in self._queue if not t.autoplay)
        return before - len(self._queue)

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
