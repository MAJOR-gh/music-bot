"""Офлайн-тесты музыкального бота (без Discord и без интернета).

Запуск:  python test_bot.py

Голос Discord и скачивание подменены заглушками: проверяется логика очереди,
плеера и кнопок — повтор, пропуск, «Стоп», ⏮️, простой, предзагрузка.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
import tempfile
import time
import traceback

from music import player as player_module
from music import sources, ytdl
from music.cog import MusicCog, _field
from music.player import MusicPlayer, Prepared
from music.queue import RepeatMode
from music.track import SearchResult, Track, format_duration, short_title


# ── Заглушки Discord ──────────────────────────────────────────────────────────
class FakeSource:
    def cleanup(self) -> None:
        pass


class FakeVC:
    """Голосовое подключение: «играет» трек play_time секунд, потом зовёт after.

    Как и discord.py, after вызывается всегда — даже если stop() пришёл раньше,
    чем «воспроизведение» успело начаться.
    """

    def __init__(self, play_time: float):
        self.play_time = play_time
        self.guild = None
        self.channel = None
        self.connected = True
        self.disconnected = False
        self._task: asyncio.Task | None = None
        self._paused = False
        self._stopping = False

    def is_connected(self) -> bool:
        return self.connected

    def is_playing(self) -> bool:
        return self._task is not None and not self._paused and not self._stopping

    def is_paused(self) -> bool:
        return self._task is not None and self._paused and not self._stopping

    def play(self, source, *, after) -> None:
        if self._task is not None:
            raise RuntimeError("Already playing audio.")
        self._paused, self._stopping = False, False
        self._task = asyncio.get_running_loop().create_task(asyncio.sleep(self.play_time))
        self._task.add_done_callback(lambda _: self._finished(after))

    def _finished(self, after) -> None:
        self._task = None
        after(None)

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stop(self) -> None:
        if self._task is not None:
            self._stopping = True
            self._task.cancel()

    async def disconnect(self, force: bool = False) -> None:
        self.stop()
        self.connected = False
        self.disconnected = True
        self.guild.voice_client = None


class FakeGuild:
    def __init__(self, gid: int, vc: FakeVC):
        self.id = gid
        self.voice_client = vc
        vc.guild = self


class FakeBot:
    def __init__(self):
        self.guilds: dict[int, FakeGuild] = {}
        self.user = None

    def get_guild(self, gid: int):
        return self.guilds.get(gid)


class TestCog(MusicCog):
    """Настоящий MusicCog, только панель в чат заменена записью в список."""

    def __init__(self, bot, idle_timeout: int = 30):
        super().__init__(bot, idle_timeout)
        self.played: list[str] = []
        self.errors: list[str] = []

    async def _on_track_change(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        if state.current is not None:
            self.played.append(state.current.title)

    async def _on_track_error(self, guild_id: int, track: Track) -> None:
        self.errors.append(track.title)


# ── Подмена подготовки трека (вместо yt-dlp) ──────────────────────────────────
PREPARE_DELAY = 0.05
prepare_calls: list[str] = []
fail_titles: set[str] = set()


async def fake_prepare(track: Track) -> Prepared | None:
    prepare_calls.append(track.title)
    await asyncio.sleep(PREPARE_DELAY)
    if track.title in fail_titles:
        return None
    return Prepared("file", path=None)


source_starts: list[float] = []


async def fake_make_source(prepared: Prepared, start: float = 0):
    source_starts.append(start)
    return FakeSource(), None


MusicPlayer._prepare = staticmethod(fake_prepare)
MusicPlayer._make_source = staticmethod(fake_make_source)


def T(title: str, duration: int = 180) -> Track:
    return Track(title=title, webpage_url=f"https://youtu.be/{title}", duration=duration,
                 uploader=None, thumbnail=None, requested_by="tester")


def setup(play_time: float = 0.1, idle: int = 30):
    global PREPARE_DELAY
    PREPARE_DELAY = 0.05
    prepare_calls.clear()
    fail_titles.clear()
    source_starts.clear()
    bot = FakeBot()
    vc = FakeVC(play_time)
    guild = FakeGuild(1, vc)
    bot.guilds[1] = guild
    cog = TestCog(bot, idle)
    return cog, guild, vc, cog.get_state(1)


async def wait_until(cond, timeout: float = 3.0, what: str = "условие") -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"не дождался: {what}")


async def shutdown(cog: TestCog, guild: FakeGuild) -> None:
    await cog._disconnect(guild.id)
    await asyncio.sleep(0.05)


# ── Тесты ─────────────────────────────────────────────────────────────────────
async def test_plays_queue_in_order():
    cog, guild, vc, state = setup()
    assert cog.enqueue_tracks(guild, [T("a"), T("b"), T("c")]) is True
    await wait_until(lambda: cog.played == ["a", "b", "c"] and state.current is None, what="a,b,c")
    await shutdown(cog, guild)


async def test_second_enqueue_does_not_cut_current():
    cog, guild, vc, state = setup(play_time=0.3)
    cog.enqueue_tracks(guild, [T("a")])
    await wait_until(lambda: cog.played == ["a"])
    assert cog.enqueue_tracks(guild, [T("b")]) is False
    await asyncio.sleep(0.1)
    assert cog.played == ["a"], cog.played          # «a» не оборвался
    await wait_until(lambda: cog.played == ["a", "b"])
    await shutdown(cog, guild)


async def test_prefetch_next_while_playing():
    cog, guild, vc, state = setup(play_time=0.5)
    cog.enqueue_tracks(guild, [T("a"), T("b")])
    await wait_until(lambda: cog.played == ["a"])
    await wait_until(lambda: "b" in prepare_calls, timeout=0.3, what="предзагрузка b во время a")
    await wait_until(lambda: cog.played == ["a", "b"])
    assert prepare_calls.count("b") == 1, prepare_calls
    await shutdown(cog, guild)


async def test_repeat_one_replays_and_reuses_file():
    cog, guild, vc, state = setup(play_time=0.05)
    state.repeat = RepeatMode.ONE
    cog.enqueue_tracks(guild, [T("a"), T("b")])
    await wait_until(lambda: cog.played.count("a") >= 3, what="a трижды")
    assert "b" not in cog.played
    assert prepare_calls.count("a") == 1, prepare_calls   # не качаем заново
    await shutdown(cog, guild)


async def test_repeat_all_cycles():
    cog, guild, vc, state = setup(play_time=0.05)
    state.repeat = RepeatMode.ALL
    cog.enqueue_tracks(guild, [T("a"), T("b")])
    await wait_until(lambda: len(cog.played) >= 5)
    assert cog.played[:5] == ["a", "b", "a", "b", "a"], cog.played
    await shutdown(cog, guild)


async def test_skip_with_repeat_one_goes_next():
    cog, guild, vc, state = setup(play_time=5)
    state.repeat = RepeatMode.ONE
    cog.enqueue_tracks(guild, [T("a"), T("b")])
    await wait_until(lambda: cog.played == ["a"])
    assert cog.skip(guild)
    await wait_until(lambda: cog.played == ["a", "b"])
    assert [t.title for t in state.upcoming] == [], state.upcoming
    await shutdown(cog, guild)


async def test_skip_while_paused():
    cog, guild, vc, state = setup(play_time=5)
    cog.enqueue_tracks(guild, [T("a"), T("b")])
    await wait_until(lambda: cog.played == ["a"])
    vc.pause()
    assert vc.is_paused()
    assert cog.skip(guild)
    await wait_until(lambda: cog.played == ["a", "b"])
    await shutdown(cog, guild)


async def test_stop_with_repeat_one_stops_for_real():
    cog, guild, vc, state = setup(play_time=5)
    state.repeat = RepeatMode.ONE
    cog.enqueue_tracks(guild, [T("a"), T("b")])
    await wait_until(lambda: cog.played == ["a"])
    cog.stop_all(guild)
    await wait_until(lambda: state.current is None and not vc.is_playing())
    await asyncio.sleep(0.2)
    assert cog.played == ["a"], cog.played
    assert state.is_empty
    # После «Стопа» бот снова играет новые треки, и повтор работает как прежде
    cog.enqueue_tracks(guild, [T("c")])
    await wait_until(lambda: cog.played[-1] == "c")
    await shutdown(cog, guild)


async def test_restart_button_with_repeat_one_no_duplicate():
    cog, guild, vc, state = setup(play_time=5)
    state.repeat = RepeatMode.ONE
    cog.enqueue_tracks(guild, [T("a"), T("b")])
    await wait_until(lambda: cog.played == ["a"])
    assert cog.restart_current(guild)
    await wait_until(lambda: cog.played == ["a", "a"])
    assert [t.title for t in state.upcoming] == ["b"], [t.title for t in state.upcoming]
    assert prepare_calls.count("a") == 1, prepare_calls
    await shutdown(cog, guild)


async def test_skip_while_preparing_is_instant():
    global PREPARE_DELAY
    cog, guild, vc, state = setup(play_time=5)
    PREPARE_DELAY = 10           # «скачивание» на 10 секунд
    cog.enqueue_tracks(guild, [T("slow"), T("next")])
    await wait_until(lambda: state.current is not None and state.current.title == "slow")
    PREPARE_DELAY = 0.05
    started = time.monotonic()
    assert cog.skip(guild)
    await wait_until(lambda: cog.played == ["next"], timeout=2, what="следующий сразу после пропуска")
    assert time.monotonic() - started < 1.5
    await shutdown(cog, guild)


async def test_stop_while_preparing():
    global PREPARE_DELAY
    cog, guild, vc, state = setup(play_time=5)
    PREPARE_DELAY = 10
    cog.enqueue_tracks(guild, [T("slow"), T("next")])
    await wait_until(lambda: state.current is not None)
    cog.stop_all(guild)
    await wait_until(lambda: state.current is None, timeout=2)
    await asyncio.sleep(0.2)
    assert cog.played == [] and state.is_empty, (cog.played, state.upcoming)
    await shutdown(cog, guild)


async def test_failed_track_reported_and_skipped():
    cog, guild, vc, state = setup()
    fail_titles.add("broken")
    cog.enqueue_tracks(guild, [T("broken"), T("ok")])
    await wait_until(lambda: cog.played == ["ok"])
    assert cog.errors == ["broken"], cog.errors
    await shutdown(cog, guild)


async def test_idle_timeout_really_disconnects():
    cog, guild, vc, state = setup(play_time=0.05, idle=1)
    cog.enqueue_tracks(guild, [T("a")])
    await wait_until(lambda: cog.played == ["a"])
    await wait_until(lambda: vc.disconnected, timeout=3, what="выход из канала по простою")
    assert guild.voice_client is None
    assert state.player_task is None


async def test_player_survives_new_voice_client():
    """/leave + /join создают новый VoiceClient — плеер должен играть в новый."""
    cog, guild, vc, state = setup(play_time=0.05)
    cog.enqueue_tracks(guild, [T("a")])
    await wait_until(lambda: cog.played == ["a"])
    new_vc = FakeVC(0.05)
    new_vc.guild = guild
    guild.voice_client = new_vc       # старый объект больше не используется
    vc.connected = False
    cog.enqueue_tracks(guild, [T("b")])
    await wait_until(lambda: cog.played == ["a", "b"])
    await shutdown(cog, guild)


async def test_leave_cancels_player_and_cleans_up():
    cog, guild, vc, state = setup(play_time=5)
    cog.enqueue_tracks(guild, [T("a"), T("b")])
    await wait_until(lambda: cog.played == ["a"])
    task = state.player_task
    await cog._disconnect(guild.id)
    await asyncio.sleep(0.05)
    assert task.done() and vc.disconnected and state.current is None and state.is_empty


def test_discard_file_waits_until_unlocked():
    """Windows не даёт удалить файл, пока его держит FFmpeg, — удаление должно дождаться."""
    fd, path = tempfile.mkstemp(prefix="musicbot-test-")
    os.close(fd)
    holder = subprocess.Popen([sys.executable, "-c",
                               f"f = open({path!r}, 'rb'); import time; time.sleep(1.5)"])

    async def run():
        await asyncio.sleep(0.3)
        ytdl.discard_file(path)
        await asyncio.sleep(3.5)

    asyncio.run(run())
    holder.wait()
    assert not os.path.exists(path), "файл так и не удалился"


def test_helpers():
    assert format_duration(None) == "?:??"
    assert format_duration(None, live=True) == "LIVE"
    assert format_duration(65) == "1:05" and format_duration(3725) == "1:02:05"
    assert short_title("a[b]c") == "a(b)c"
    assert len(short_title("x" * 300)) == 60
    lines = [f"`{i}.` " + "y" * 150 for i in range(10)]
    value = _field(lines, total=40)
    assert len(value) <= 1024 and value.endswith(f"… и ещё {40 - value.count(chr(10))}"), value[-40:]
    t = T("q")
    assert t.link_title == "[q](https://youtu.be/q)"
    sr = SearchResult(title="Song", url=None, duration=200, uploader="Artist", source="Spotify")
    tr = sr.to_track("me")
    assert tr.match == ("Artist", "Song") and tr.webpage_url is None


def test_spotify_parsing():
    assert sources.parse_spotify_link("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=x") == (
        "track", "4uLU6hMCjMI75M1A2tKUQC")
    assert sources.parse_spotify_link("https://open.spotify.com/intl-de/album/1DFixLWuPkv3KT3TnV35m3") == (
        "album", "1DFixLWuPkv3KT3TnV35m3")
    assert sources.parse_spotify_link("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M") == (
        "playlist", "37i9dQZF1DXcBWIGoYBM5M")
    assert sources.parse_spotify_link("просто текст") is None
    html = ('<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"state":{"data":{"entity":{"name":"Mix","type":"playlist",'
            '"trackList":[{"title":"One","subtitle":"Band","duration":200000},'
            '{"title":"Two","subtitle":"Other","duration":61000}]}}}}}}</script>')
    name, tracks = sources.parse_spotify_embed(html)
    assert name == "Mix" and [(t.title, t.artist, t.duration) for t in tracks] == [
        ("One", "Band", 200), ("Two", "Other", 61)]


def test_match_score_prefers_right_song():
    good = SearchResult(title="Bohemian Rhapsody (Remastered 2011)", url="u1", duration=355,
                        uploader="Queen")
    bad = SearchResult(title="Bohemian Rhapsody (Piano Cover)", url="u2", duration=290,
                       uploader="Some Pianist")
    assert sources.match_score(good, "Queen", "Bohemian Rhapsody", 354) > \
        sources.match_score(bad, "Queen", "Bohemian Rhapsody", 354)


def test_split_artist_title():
    assert sources.split_artist_title("Mick Gordon - Doom 2016: Menu theme (HQ, file rip)") == (
        "Mick Gordon", "Doom 2016: Menu theme")
    assert sources.split_artist_title("Song Name [Official Video]", "Band - Topic") == (
        "Band", "Song Name")


def test_bot_check_detected():
    ytdl.last_bot_check = None
    ytdl.note_error("ERROR: [youtube] x: Sign in to confirm you’re not a bot. Use --cookies")
    assert ytdl.last_bot_check is not None
    ytdl.last_bot_check = None
    ytdl.note_error("ERROR: HTTP Error 404")
    assert ytdl.last_bot_check is None


def test_soundcloud_picks_matching_track():
    async def fake_extract(target, *, flat=False, timeout=60):
        return {"entries": [
            {"url": "sc/wrong", "title": "Doom Eternal OST - BFG Division", "duration": 500,
             "uploader": "someone"},
            {"url": "sc/right", "title": "Doom 2016 - Menu Theme", "duration": 241,
             "uploader": "Mick Gordon"},
        ]}

    async def fake_extract_junk(target, *, flat=False, timeout=60):
        return {"entries": [{"url": "sc/junk", "title": "Totally different", "duration": 30,
                             "uploader": "x"}]}

    real = ytdl.extract
    try:
        ytdl.extract = fake_extract
        urls = asyncio.run(sources.soundcloud_alternatives("Mick Gordon", "Doom 2016: Menu theme", 240))
        assert urls == ["sc/right"], urls
        ytdl.extract = fake_extract_junk
        urls = asyncio.run(sources.soundcloud_alternatives("Mick Gordon", "Doom 2016: Menu theme", 240))
        assert urls == [], urls        # лучше не сыграть, чем включить другую песню

        async def fake_extract_preview(target, *, flat=False, timeout=60):
            return {"entries": [
                {"url": "sc/preview", "title": "Bones", "duration": 30.0, "uploader": "Imagine Dragons"},
                {"url": "sc/full", "title": "Imagine Dragons - Bones", "duration": 165,
                 "uploader": "fan"},
            ]}
        ytdl.extract = fake_extract_preview
        urls = asyncio.run(sources.soundcloud_alternatives("Imagine Dragons", "Bones", 165))
        assert urls == ["sc/full"], urls   # 30-секундный отрывок Go+ пропущен
        assert "preview" in ytdl.format_for("https://api.soundcloud.com/tracks/1")
        assert ytdl.format_for("https://youtu.be/x") == "bestaudio/best"
    finally:
        ytdl.extract = real


def test_updater_picks_latest():
    from music import updater
    pypi = {"info": {"version": "2026.8.19"}, "releases": {
        "2026.8.19": [{}], "2026.10.10.233141.dev0": [{}], "2026.11.1": [{"yanked": True}]}}
    assert updater.pick_latest(pypi, False) == "2026.8.19"
    assert updater.pick_latest(pypi, True) == "2026.10.10.233141.dev0"
    assert updater.version_tuple("2026.10.10.233141.dev0") > updater.version_tuple("2026.8.19")


async def test_play_autocomplete():
    """Подсказки /play: варианты из YT Music, кэш, выбор сохраняет метаданные."""
    from music.cog import suggestion_label
    calls = []

    async def fake_search(query, limit):
        calls.append(query)
        return [SearchResult(title="BFG Division", url="https://www.youtube.com/watch?v=abc",
                             duration=512, uploader="Mick Gordon"),
                SearchResult(title="x" * 120, url="https://www.youtube.com/watch?v=def",
                             duration=None, uploader=None)]

    orig = sources.search_ytmusic
    sources.search_ytmusic = fake_search
    try:
        cog = MusicCog(None, 60)
        choices = await cog.play_autocomplete(None, "bfg division")
        assert [c.value for c in choices] == ["https://www.youtube.com/watch?v=abc",
                                              "https://www.youtube.com/watch?v=def"]
        assert choices[0].name == "BFG Division - Mick Gordon (8:32)", choices[0].name
        assert all(len(c.name) <= 100 for c in choices)
        await cog.play_autocomplete(None, "  BFG   Division ")
        assert calls == ["bfg division"], calls          # второй раз — из кэша
        assert cog._ac_results["https://www.youtube.com/watch?v=abc"].uploader == "Mick Gordon"
        assert await cog.play_autocomplete(None, "b") == []
        assert await cog.play_autocomplete(None, "https://youtu.be/x") == []
    finally:
        sources.search_ytmusic = orig
    assert suggestion_label(SearchResult(title="T", url=None, duration=None, uploader=None)) == "T"

    # Last.fm: подсказки, если источник выбран и есть ключ; value — «lastfm:…»
    import types
    import config as cfg

    async def fake_lastfm(query, limit):
        return [SearchResult(title="Bones", url=None, duration=None, uploader="Imagine Dragons",
                             source="Last.fm")]
    orig_lfm, orig_key = sources.search_lastfm, cfg.LASTFM_API_KEY
    sources.search_lastfm, cfg.LASTFM_API_KEY = fake_lastfm, "k"
    try:
        cog = MusicCog(None, 60)
        inter = types.SimpleNamespace(namespace=types.SimpleNamespace(source="lastfm"))
        choices = await cog.play_autocomplete(inter, "bones")
        assert [c.value for c in choices] == ["lastfm:Imagine Dragons - Bones"], choices
        assert choices[0].name == "Bones - Imagine Dragons"
        track = cog._ac_results["lastfm:Imagine Dragons - Bones"].to_track("me")
        assert track.match == ("Imagine Dragons", "Bones") and track.webpage_url is None
        cfg.LASTFM_API_KEY = ""                    # без ключа — обычные подсказки YT Music
        assert MusicCog._suggest_source(inter) == "ytmusic"

        async def fake_spotify(query, limit):
            return [SearchResult(title="Bones", url=None, duration=165,
                                 uploader="Imagine Dragons", source="Spotify")]
        orig_sp = sources.search_spotify, cfg.SPOTIFY_CLIENT_ID, cfg.SPOTIFY_CLIENT_SECRET
        sources.search_spotify, cfg.SPOTIFY_CLIENT_ID, cfg.SPOTIFY_CLIENT_SECRET = fake_spotify, "i", "s"
        try:
            inter.namespace.source = "SpOtIfY"
            choices = await MusicCog(None, 60).play_autocomplete(inter, "bones")
            assert [c.value for c in choices] == ["spotify:Imagine Dragons - Bones"], choices
            assert choices[0].name == "Bones - Imagine Dragons (2:45)"
        finally:
            sources.search_spotify, cfg.SPOTIFY_CLIENT_ID, cfg.SPOTIFY_CLIENT_SECRET = orig_sp
    finally:
        sources.search_lastfm, cfg.LASTFM_API_KEY = orig_lfm, orig_key
    # Сообщение в чат: без «?:??», у Last.fm/Spotify — с исполнителем и длительностью с YT Music
    lfm = SearchResult(title="Bones", url=None, duration=None, uploader="Imagine Dragons",
                       source="Last.fm").to_track("me")
    assert MusicCog.enqueue_message(True, lfm) == "▶️ Запускаю: **Imagine Dragons — Bones**"

    async def fake_find(artist, title, duration=None):
        assert (artist, title) == ("Imagine Dragons", "Bones")
        return SearchResult(title="Bones", url="https://www.youtube.com/watch?v=TO-_3tck2tg",
                            duration=165, uploader="Imagine Dragons")
    orig_find = sources.find_on_ytmusic
    sources.find_on_ytmusic = fake_find
    try:
        assert await MusicCog(None, 60)._resolve(None, lfm)
    finally:
        sources.find_on_ytmusic = orig_find
    assert lfm.webpage_url.endswith("TO-_3tck2tg")
    assert MusicCog.enqueue_message(False, lfm) == "➕ В очередь: **Imagine Dragons — Bones** `[2:45]`"
    for raw, want in [("Last.fm", "lastfm"), ("LAST FM", "lastfm"), ("lastfm", "lastfm"),
                      ("Spotify", "spotify"), ("YouTube Music", "ytmusic"), ("YTMusic", "ytmusic"),
                      ("YouTube", "youtube"), ("что-то", "youtube"), ("", "youtube"), (None, "youtube")]:
        assert sources.normalize_source(raw) == want, (raw, sources.normalize_source(raw))


async def test_page_reload_retries_with_alt_clients():
    """Плавающие отказы YouTube: перебор клиентов, YT Music, запоминание сработавшего."""
    url = "https://www.youtube.com/watch?v=V2SVRJ6dcE4"
    seen = []
    failures = ["ERROR: [youtube] x: The page needs to be reloaded.",
                "ERROR: [youtube] x: Requested format is not available."]

    async def fake_once(target, path, timeout, extra):
        seen.append((target, extra))
        if failures:
            return False, failures.pop(0)
        return True, None

    orig, orig_pref = ytdl._download_once, ytdl._preferred
    ytdl._download_once = fake_once
    try:
        ytdl._preferred = 0
        assert await ytdl.download(url, "/tmp/x", 5)
        assert seen[0] == (url, [])
        assert seen[1] == (url, ["--extractor-args", "youtube:player_client=tv_samsung"])
        assert seen[2][1] == ["--extractor-args", "youtube:player_client=default,web_embedded"]
        assert ytdl._preferred == 2                       # запомнил сработавший
        seen.clear()
        assert await ytdl.download(url, "/tmp/x", 5)      # следующий трек — сразу через него
        assert seen == [(url, ["--extractor-args", "youtube:player_client=default,web_embedded"])]

        seen.clear()

        async def other(target, path, timeout, extra):
            seen.append(target)
            return False, "ERROR: Video unavailable"
        ytdl._download_once = other
        ytdl._preferred = 0
        assert not await ytdl.download(url, "/tmp/x", 5)
        assert len(seen) == 1                             # другая ошибка — без перебора
        seen.clear()
        assert not await ytdl.download("https://soundcloud.com/a/b", "/tmp/x", 5)
        assert seen == ["https://soundcloud.com/a/b"]
    finally:
        ytdl._download_once, ytdl._preferred = orig, orig_pref

    # Все способы отказали → диагностика, музыкальный способ идёт на music.youtube.com.
    seen.clear()
    diag = []

    async def always_reload(target, path, timeout, extra):
        seen.append((target, extra))
        return False, "ERROR: The page needs to be reloaded."

    async def fake_diag(u):
        diag.append(u)
    ytdl._download_once, orig_diag = always_reload, ytdl._log_diagnosis
    ytdl._log_diagnosis = fake_diag
    try:
        ytdl._preferred, ytdl._all_failed_at = 0, 0.0
        assert not await ytdl.download(url, "/tmp/x", 5)
        assert len(seen) == len(ytdl.ATTEMPTS) and diag == [url]
        music = [t for t, e in seen if "web_music" in " ".join(e)]
        assert music == ["https://music.youtube.com/watch?v=V2SVRJ6dcE4"]
        seen.clear()                                      # пауза: дальше только один способ
        assert not await ytdl.download(url, "/tmp/x", 5)
        assert len(seen) == 1 and diag == [url]
        ytdl._all_failed_at = 0.0
    finally:
        ytdl._download_once, ytdl._log_diagnosis, ytdl._preferred = orig, orig_diag, orig_pref

    calls = []

    async def fake_run_once(args, timeout):
        calls.append(args)
        if len(calls) <= next(i for i, a in enumerate(ytdl.ATTEMPTS) if a.music):
            raise ytdl.YtdlError("ERROR: The page needs to be reloaded.")
        return b"{}"

    orig_run = ytdl._run_once
    ytdl._run_once = fake_run_once
    try:
        ytdl._preferred = 0
        assert await ytdl.extract(url) == {}
        assert calls[-1][-1] == "https://music.youtube.com/watch?v=V2SVRJ6dcE4"
        assert "web_music" in " ".join(calls[-1])
        assert ytdl.video_id("https://youtu.be/V2SVRJ6dcE4") == "V2SVRJ6dcE4"
        assert ytdl.video_id("https://www.youtube.com/watch?app=desktop&v=V2SVRJ6dcE4") == "V2SVRJ6dcE4"
        assert ytdl.video_id("ytsearch1:bfg") is None
    finally:
        ytdl._run_once, ytdl._preferred = orig_run, orig_pref


async def test_extract_cooldown_after_all_clients_fail():
    """extract, как и download: все способы отказали → дальше пробуется только один."""
    calls = []

    async def always_reload(args, timeout):
        calls.append(args)
        raise ytdl.YtdlError("ERROR: The page needs to be reloaded.")

    orig, orig_pref = ytdl._run_once, ytdl._preferred
    ytdl._run_once = always_reload
    try:
        ytdl._preferred, ytdl._all_failed_at = 0, 0.0
        url = "https://www.youtube.com/watch?v=V2SVRJ6dcE4"
        for expected in (len(ytdl.ATTEMPTS), 1):
            calls.clear()
            try:
                await ytdl.extract(url)
                raise AssertionError("extract должен был упасть")
            except ytdl.YtdlError:
                pass
            assert len(calls) == expected, (expected, len(calls))
    finally:
        ytdl._run_once, ytdl._preferred, ytdl._all_failed_at = orig, orig_pref, 0.0


async def test_youtube_link_metadata_without_ytdlp():
    """Ссылка YouTube: название берётся из YouTube Music, yt-dlp не запускается."""
    async def fast(vid):
        assert vid == "V2SVRJ6dcE4"
        return SearchResult(title="Song", url=sources.youtube_url(vid), duration=200, uploader="Band")

    async def no_ytdlp(*args, **kwargs):
        raise AssertionError("yt-dlp не нужен")

    orig_info, orig_extract = sources.youtube_info, ytdl.extract
    sources.youtube_info, ytdl.extract = fast, no_ytdlp
    try:
        track = await MusicCog(None, 60)._track_from_url("https://youtu.be/V2SVRJ6dcE4?t=5", "me")
        assert (track.title, track.duration, track.uploader) == ("Song", 200, "Band")
        assert track.webpage_url == "https://www.youtube.com/watch?v=V2SVRJ6dcE4"

        async def unknown(vid):          # эфир / недоступный ролик → как раньше, через yt-dlp
            return None

        async def ytdlp(target, *, flat=False, timeout=60):
            return {"title": "Live", "webpage_url": target, "is_live": True}
        sources.youtube_info, ytdl.extract = unknown, ytdlp
        track = await MusicCog(None, 60)._track_from_url("https://youtu.be/V2SVRJ6dcE4", "me")
        assert track.title == "Live" and track.live
    finally:
        sources.youtube_info, ytdl.extract = orig_info, orig_extract


async def test_play_enter_uses_shown_suggestion():
    """Enter без выбора подсказки играет первую из показанных, без нового поиска."""
    import types
    cog = MusicCog(None, 60)
    shown = SearchResult(title="BFG Division", url="https://www.youtube.com/watch?v=abcdefghijk",
                         duration=512, uploader="Mick Gordon", source="YT Music")
    cog._ac_cache[cog._ac_key("bfg division", "ytmusic")] = [shown]
    sent, queued = [], []

    async def noop(*args, **kwargs):
        return None

    async def voice(interaction):
        return object()

    async def enqueue(interaction, tracks):
        queued.extend(tracks)
        return True

    async def send(text=None, **kwargs):
        sent.append(text)

    async def no_search(*args, **kwargs):
        raise AssertionError("поиск не нужен — подсказка уже была")

    cog._ensure_voice, cog.enqueue = voice, enqueue
    inter = types.SimpleNamespace(
        response=types.SimpleNamespace(defer=noop), followup=types.SimpleNamespace(send=send),
        user=types.SimpleNamespace(display_name="me"), namespace=types.SimpleNamespace(source=None))
    orig = sources.search
    sources.search = no_search
    try:
        await MusicCog.play.callback(cog, inter, "  BFG   Division ", None)
    finally:
        sources.search = orig
    assert [t.title for t in queued] == ["BFG Division"], queued
    assert sent and "BFG Division" in sent[0], sent


def test_private_urls_rejected():
    from music.cog import _is_public_url
    for url in ("http://127.0.0.1:4416/ping", "http://localhost/x", "http://169.254.169.254/latest",
                "http://10.0.0.5/", "http://[::1]/", "http://printer.local/"):
        assert not _is_public_url(url), url
    for url in ("https://www.youtube.com/watch?v=x", "https://soundcloud.com/a/b", "https://8.8.8.8/"):
        assert _is_public_url(url), url


def test_queue_tools():
    from music.queue import GuildMusicState
    from music.track import parse_time
    assert [parse_time(x) for x in ("90", "2:30", "1:02:03", "2:75", "x")] == [90, 150, 3723, None, None]
    st = GuildMusicState(1)
    for name in "abcde":
        st.add(T(name))
    names = lambda: [t.title for t in st.upcoming]  # noqa: E731
    assert st.remove(2).title == "b" and names() == ["a", "c", "d", "e"]
    assert st.remove(9) is None
    assert st.move(4, 1).title == "e" and names() == ["e", "a", "c", "d"]
    assert st.move(1, 99).title == "e" and names() == ["a", "c", "d", "e"]
    assert st.skip_to(3).title == "d" and names() == ["d", "e"]
    st.repeat = RepeatMode.ALL
    assert st.skip_to(2).title == "e" and names() == ["e", "d"]   # «вся очередь» — d в конец
    auto = T("auto")
    auto.autoplay = True
    st.add(auto)
    st.add(T("mine"))                                # заказанный — перед автоплеем
    assert names() == ["e", "d", "mine", "auto"]
    st.shuffle()
    assert names()[-1] == "auto" and sorted(names()[:3]) == ["d", "e", "mine"]
    assert st.drop_autoplay() == 1 and "auto" not in names()
    st.stop()
    assert st.halted and st.is_empty
    st.add(T("again"))
    assert not st.halted


async def test_back_button_plays_previous():
    cog, guild, vc, state = setup(play_time=0.3)
    cog.enqueue_tracks(guild, [T("a"), T("b")])
    await wait_until(lambda: cog.played == ["a", "b"], what="a, b")
    assert cog.go_back(guild)
    await wait_until(lambda: cog.played == ["a", "b", "a", "b"] and state.current is None,
                     what="назад → a, потом снова b")
    await shutdown(cog, guild)


async def test_seek_reopens_same_track():
    cog, guild, vc, state = setup(play_time=0.3)
    cog.enqueue_tracks(guild, [T("a"), T("b")])
    await wait_until(lambda: cog.played == ["a"] and source_starts == [0])
    player = cog._players[guild.id]
    assert player.seek(500) is not None              # за концом трека
    assert cog.seek(guild, 42) is None
    await wait_until(lambda: source_starts == [0, 42], what="переоткрыл с 42с")
    assert cog.played == ["a"] and state.current.title == "a"   # не новый трек
    assert cog.position(guild.id) == 42
    await wait_until(lambda: cog.played == ["a", "b"], what="после перемотки — b")
    await shutdown(cog, guild)


async def test_autoplay_adds_similar_and_stop_halts():
    cog, guild, vc, state = setup(play_time=0.15)
    calls = []

    async def fake_similar(artist, title, url=None, limit=30):
        calls.append(title)
        return [SearchResult(title=n, url=f"https://youtu.be/{n}", duration=200,
                             uploader="X", source="YT Music") for n in ("a", "s1", "s2")]

    orig = sources.similar_tracks
    sources.similar_tracks = fake_similar
    try:
        cog.set_autoplay(guild, True)
        cog.enqueue_tracks(guild, [T("a")])
        await wait_until(lambda: len(cog.played) >= 3, what="автоплей подобрал треки")
        assert cog.played[0] == "a" and "a" not in cog.played[1:], cog.played  # без повторов
        assert set(cog.played[1:3]) == {"s1", "s2"}
        cog.stop_all(guild)
        await asyncio.sleep(0.3)
        n = len(cog.played)
        await asyncio.sleep(0.3)
        assert len(cog.played) == n and state.current is None     # «Стоп» — тишина
        cog.set_autoplay(guild, False)
    finally:
        sources.similar_tracks = orig
    await shutdown(cog, guild)


ASYNC_TESTS = [
    test_plays_queue_in_order,
    test_second_enqueue_does_not_cut_current,
    test_prefetch_next_while_playing,
    test_repeat_one_replays_and_reuses_file,
    test_repeat_all_cycles,
    test_skip_with_repeat_one_goes_next,
    test_skip_while_paused,
    test_stop_with_repeat_one_stops_for_real,
    test_restart_button_with_repeat_one_no_duplicate,
    test_skip_while_preparing_is_instant,
    test_stop_while_preparing,
    test_failed_track_reported_and_skipped,
    test_idle_timeout_really_disconnects,
    test_player_survives_new_voice_client,
    test_leave_cancels_player_and_cleans_up,
    test_play_autocomplete,
    test_page_reload_retries_with_alt_clients,
    test_back_button_plays_previous,
    test_seek_reopens_same_track,
    test_autoplay_adds_similar_and_stop_halts,
    test_extract_cooldown_after_all_clients_fail,
    test_youtube_link_metadata_without_ytdlp,
    test_play_enter_uses_shown_suggestion,
]
SYNC_TESTS = [test_helpers, test_spotify_parsing, test_match_score_prefers_right_song,
              test_split_artist_title, test_bot_check_detected, test_soundcloud_picks_matching_track,
              test_updater_picks_latest, test_discard_file_waits_until_unlocked, test_queue_tools,
              test_private_urls_rejected]


def main() -> int:
    player_module.RECONNECT_WAIT = 2
    failed = 0
    for test in SYNC_TESTS + ASYNC_TESTS:
        try:
            if inspect.iscoroutinefunction(test):
                asyncio.run(asyncio.wait_for(test(), 20))
            else:
                test()
            print(f"✅ {test.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"❌ {test.__name__}")
            traceback.print_exc()
    total = len(SYNC_TESTS) + len(ASYNC_TESTS)
    print(f"\n{total - failed}/{total} тестов прошло")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
