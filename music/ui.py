"""UI-компоненты: живая панель-плеер (кнопки) и выпадающий список поиска.

PlayerView  — кнопки ⏮️/Пауза/Продолжить/Пропустить/Повтор/Стоп под эмбедом «сейчас играет».
SearchView  — выпадающий список вариантов текстового поиска.

Сами действия (пауза, пропуск, постановка в очередь, обновление панели) живут в
MusicCog — здесь только разметка и проброс кликов в cog, чтобы не дублировать логику.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from .track import SearchResult

if TYPE_CHECKING:
    from .cog import MusicCog

logger = logging.getLogger("music_bot.ui")


# ── Кнопки управления плеером ──────────────────────────────────────────────────
class PlayerView(discord.ui.View):
    """Кнопки под эмбедом «сейчас играет». Привязаны к конкретному серверу."""

    def __init__(self, cog: "MusicCog", guild_id: int):
        super().__init__(timeout=None)  # живёт пока висит панель
        self.cog = cog
        self.guild_id = guild_id
        self._sync_repeat_button()

    def _vc(self, interaction: discord.Interaction) -> discord.VoiceClient | None:
        guild = interaction.guild
        return guild.voice_client if guild else None

    def _sync_repeat_button(self) -> None:
        """Привести вид кнопки 🔁 в соответствие текущему режиму повтора."""
        mode = self.cog.get_state(self.guild_id).repeat
        self.repeat_btn.emoji = mode.emoji
        self.repeat_btn.label = f"Повтор: {mode.label}"
        self.repeat_btn.style = (
            discord.ButtonStyle.secondary if mode == 0 else discord.ButtonStyle.success
        )

    @discord.ui.button(emoji="⏮️", label="В начало", style=discord.ButtonStyle.secondary)
    async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Перезапуск с начала: cog кладёт трек в начало очереди и обрывает
        # текущий. Панель обновится сама через on_track_change.
        if self.cog.restart_current(interaction.guild):
            await interaction.response.defer()
        else:
            await interaction.response.send_message("❌ Сейчас ничего не играет.", ephemeral=True)

    @discord.ui.button(emoji="⏸️", label="Пауза", style=discord.ButtonStyle.secondary)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self._vc(interaction)
        if vc and vc.is_playing():
            vc.pause()
            await self.cog.refresh_panel_inplace(interaction)
        else:
            await interaction.response.send_message("❌ Сейчас ничего не играет.", ephemeral=True)

    @discord.ui.button(emoji="▶️", label="Продолжить", style=discord.ButtonStyle.success)
    async def resume_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self._vc(interaction)
        if vc and vc.is_paused():
            vc.resume()
            await self.cog.refresh_panel_inplace(interaction)
        else:
            await interaction.response.send_message("❌ Нечего возобновлять.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", label="Пропустить", style=discord.ButtonStyle.primary)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Плеер возьмёт следующий трек и сам перевыложит панель (on_track_change).
        if self.cog.skip(interaction.guild):
            await interaction.response.defer()
        else:
            await interaction.response.send_message("❌ Нечего пропускать.", ephemeral=True)

    @discord.ui.button(emoji="🔁", label="Повтор: выкл", style=discord.ButtonStyle.secondary, row=1)
    async def repeat_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Переключаем режим и перерисовываем панель на месте (вид кнопки и эмбед
        # обновятся через refresh_panel_inplace → новый PlayerView).
        self.cog.get_state(self.guild_id).cycle_repeat()
        await self.cog.refresh_panel_inplace(interaction)

    @discord.ui.button(emoji="⏹️", label="Стоп", style=discord.ButtonStyle.danger, row=1)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._vc(interaction) is None:
            await interaction.response.send_message("❌ Я не в голосовом канале.", ephemeral=True)
            return
        self.cog.stop_all(interaction.guild)
        state = self.cog.get_state(self.guild_id)
        if state.panel_message is not None and state.panel_message.id == interaction.message.id:
            state.panel_message = None   # это сообщение превращаем в «Остановлено», не удаляем
        await interaction.response.edit_message(
            content="⏹️ Остановлено, очередь очищена.", embed=None, view=None
        )


# ── Перелистываемый выпадающий список вариантов поиска ─────────────────────────
class SearchSelect(discord.ui.Select):
    """Выпадашка одной страницы результатов. value = АБСОЛЮТНЫЙ индекс в results."""

    def __init__(self, parent: "SearchView"):
        self.parent_view = parent
        super().__init__(
            placeholder="Выбери вариант трека…", min_values=1, max_values=1,
            options=[discord.SelectOption(label="—", value="0")],  # заглушка, заменим в _build
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view = self.parent_view
        chosen = view.results[int(self.values[0])]

        # Звук не добываем здесь: плеер сам найдёт/скачает трек перед игрой.
        track = chosen.to_track(requester=interaction.user.display_name)
        was_idle = await view.cog.enqueue(interaction, [track])
        if was_idle is None:
            await interaction.edit_original_response(
                content="❌ Не удалось подключиться к голосовому каналу.", view=None
            )
            view.stop()
            return

        # Гасим всё после выбора, показываем что добавили.
        for child in view.children:
            child.disabled = True
        view.stop()
        await interaction.edit_original_response(
            content=view.cog.enqueue_message(was_idle, track), view=view
        )


class SearchView(discord.ui.View):
    """Список результатов поиска с пагинацией: select на странице + кнопки ◀▶."""

    def __init__(
        self, cog: "MusicCog", results: list[SearchResult], requester: str,
        page_size: int = 10,
    ):
        super().__init__(timeout=60)  # минута на выбор
        self.cog = cog
        self.results = results
        self.requester = requester
        self.page_size = page_size
        self.page = 0
        self.pages = max(1, (len(results) + page_size - 1) // page_size)
        self.message: discord.Message | None = None  # сообщение со списком (для таймаута)

        self.select = SearchSelect(self)
        self.add_item(self.select)
        self._build()

    @staticmethod
    def _describe(r: SearchResult) -> str:
        parts = [f"[{r.duration_str}]"]
        if r.uploader:
            parts.append(r.uploader)
        if r.note:
            parts.append(r.note)
        return " • ".join(parts)[:100]

    def _build(self) -> None:
        """Пересобрать опции select и состояние навигации под текущую страницу."""
        start = self.page * self.page_size
        items = self.results[start:start + self.page_size]
        self.select.options = [
            discord.SelectOption(
                label=(r.title or "Unknown")[:100],
                description=self._describe(r),
                value=str(start + offset),
                emoji="🎵",
            )
            for offset, r in enumerate(items)
        ]
        self.select.placeholder = (
            f"Варианты {start + 1}–{start + len(items)} из {len(self.results)}…"
        )
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.pages - 1
        self.page_btn.label = f"Стр. {self.page + 1}/{self.pages}"

    @discord.ui.button(emoji="◀️", label="Назад", style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            self._build()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Стр. 1/1", style=discord.ButtonStyle.secondary, disabled=True, row=1)
    async def page_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Индикатор страницы, кликов не принимает (disabled).
        pass

    @discord.ui.button(emoji="▶️", label="Вперёд", style=discord.ButtonStyle.secondary, row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.pages - 1:
            self.page += 1
            self._build()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        # Без правки сообщения список выглядел бы живым, а клик давал
        # «Ошибка взаимодействия». Гасим его и в самом сообщении.
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(content=f"{self.message.content}\n⌛ Время выбора вышло.",
                                        view=self)
            except discord.HTTPException:
                pass
