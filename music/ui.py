"""UI-компоненты: живая панель-плеер (кнопки) и выпадающий список поиска.

PlayerView  — кнопки Пауза/Продолжить/Пропустить/Стоп под эмбедом «сейчас играет».
SearchView  — выпадающий список топ-N вариантов текстового поиска.

Сами действия (пауза, пропуск, постановка в очередь, обновление панели) живут в
MusicCog — здесь только разметка и проброс кликов в cog, чтобы не дублировать логику.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from .player import MusicPlayer
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

    def _vc(self, interaction: discord.Interaction) -> discord.VoiceClient | None:
        guild = interaction.guild
        return guild.voice_client if guild else None

    @discord.ui.button(emoji="⏮️", label="В начало", style=discord.ButtonStyle.secondary)
    async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self._vc(interaction)
        if vc and (vc.is_playing() or vc.is_paused()):
            # Перезапуск с начала: cog кладёт трек в начало очереди и обрывает
            # текущий. Панель обновится сама через on_track_change.
            await interaction.response.defer()
            self.cog.restart_current(self.guild_id, vc)
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
        vc = self._vc(interaction)
        if vc and (vc.is_playing() or vc.is_paused()):
            # vc.stop() → сработает after-колбэк плеера → новый трек → панель
            # обновится сама через on_track_change. Здесь только подтверждаем клик.
            await interaction.response.defer()
            vc.stop()
        else:
            await interaction.response.send_message("❌ Нечего пропускать.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", label="Стоп", style=discord.ButtonStyle.danger)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self._vc(interaction)
        if vc is None:
            await interaction.response.send_message("❌ Я не в голосовом канале.", ephemeral=True)
            return
        state = self.cog.get_state(self.guild_id)
        state.clear()
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        state.panel_message = None
        await interaction.response.edit_message(
            content="⏹️ Остановлено, очередь очищена.", embed=None, view=None
        )


# ── Выпадающий список вариантов поиска ─────────────────────────────────────────
class SearchSelect(discord.ui.Select):
    """Один пункт = один вариант (longmix, sped up, slowed, remix…)."""

    def __init__(self, cog: "MusicCog", results: list[SearchResult], requester: str):
        self.cog = cog
        self.results = results
        self.requester = requester
        options = []
        for i, r in enumerate(results):
            uploader = f" • {r.uploader}" if r.uploader else ""
            options.append(discord.SelectOption(
                label=r.title[:100],
                description=f"[{r.duration_str}]{uploader}"[:100],
                value=str(i),
                emoji="🎵",
            ))
        super().__init__(
            placeholder="Выбери вариант трека…",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        chosen = self.results[int(self.values[0])]

        # Полный поток добываем только сейчас — для выбранного варианта.
        track = await MusicPlayer.resolve(chosen.url, requester=self.requester)
        if track is None:
            await interaction.edit_original_response(
                content="❌ Не удалось загрузить выбранный трек.", view=None
            )
            return

        ok, info = await self.cog.enqueue_from_interaction(interaction, track)
        if not ok:
            await interaction.edit_original_response(content=info, view=None)
            return

        # Гасим список после выбора, показываем что добавили.
        for child in self.view.children:
            child.disabled = True
        await interaction.edit_original_response(content=info, view=self.view)


class SearchView(discord.ui.View):
    def __init__(self, cog: "MusicCog", results: list[SearchResult], requester: str):
        super().__init__(timeout=60)  # минута на выбор
        self.add_item(SearchSelect(cog, results, requester))

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
