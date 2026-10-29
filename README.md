# 🎵 Discord Music Bot

Личный музыкальный бот для Discord на **discord.py 2.x** + **yt-dlp** + **FFmpeg**.
Играет YouTube, SoundCloud и ссылки Spotify, ищет по YouTube, YouTube Music,
Spotify и Last.fm. Очередь — отдельная для каждого сервера.

## Возможности

| Команда | Действие |
|---|---|
| `/join` | Подключиться к твоему голосовому каналу (или переместиться в него) |
| `/leave` | Отключиться и очистить очередь |
| `/play <запрос> [source]` | Играть по ссылке (YouTube/YouTube Music/Spotify — трек, альбом, плейлист) или искать по тексту с подсказками; `source` — где искать: YouTube, YouTube Music, Spotify, Last.fm |
| `/skip` | Пропустить текущий трек |
| `/back` | Предыдущий трек |
| `/seek <время>` | Перемотать: `90`, `2:30`, `1:02:03` |
| `/forward [время]` / `/rewind [время]` | Вперёд / назад (по умолчанию на 10 секунд) |
| `/autoplay [вкл/выкл]` | Автоплей: когда очередь кончилась — похожие треки (Last.fm, без ключа — радио YouTube Music) |
| `/shuffle` | Перемешать очередь |
| `/remove <номер>` | Убрать трек из очереди |
| `/move <from> <to>` | Переставить трек в очереди |
| `/skipto <номер>` | Перейти к треку из очереди |
| `/clear` | Очистить очередь (текущий трек доиграет) |
| `/repeat [режим]` | Повтор: выкл / один трек / вся очередь (без аргумента — по кругу) |
| `/pause` | Пауза |
| `/resume` | Продолжить |
| `/stop` | Остановить и очистить очередь (бот остаётся в канале) |
| `/queue` | Показать очередь |
| `/nowplaying` | Что играет сейчас |
| `/ping` | Диагностика: версия и аптайм бота, задержки, FFmpeg, yt-dlp, JS-рантайм, куки, память хостинга |

Дополнительно:
- 🔎 Подсказки прямо при вводе `/play`: до 25 вариантов из YouTube Music (или
  Last.fm) во всплывающем списке Discord. Enter без выбора — играет первая подсказка.
- ⚡ Ссылка на YouTube разбирается за доли секунды (метаданные из YouTube Music);
  yt-dlp запускается уже только за звуком.
- 🔁 Повтор одного трека или всей очереди — командой `/repeat` или кнопкой на панели.
- 🎛️ Кнопки на панели: ⏪ Назад, ⏮️ В начало, пауза, пропуск, 🔁 Повтор, 🔀 Перемешать, 📻 Автоплей, ⏹️ Стоп.
- ⏩ Перемотка без перекодирования: Opus с YouTube по-прежнему идёт как есть.
- ⏱️ Авто-отключение через 5 минут бездействия (настраивается `IDLE_TIMEOUT`).
- 👥 Авто-отключение, если в канале не осталось людей.
- 🔒 Защита от запуска нескольких плееров на один сервер.
- ⏩ Следующий трек качается заранее, пока играет текущий, — без пауз между треками.
- 🔌 Если сеть/туннель моргнули — бот переподключает голос сам, а не вылетает из канала.
- ⬆️ yt-dlp обновляется сам (при старте и раз в 12 часов) — меньше 403 от YouTube.
- 📋 **Плейлисты и альбомы YouTube / YouTube Music**: ссылка в `/play` ставит в очередь все треки
  (не больше `MAX_PLAYLIST_TRACKS`). Ссылка на ролик внутри плейлиста (`watch?v=…&list=…`) играет
  плейлист начиная с этого ролика. Миксы (`list=RD…`) играют как один ролик.
- 🎧 Если YouTube не отдал трек, бот может поискать ту же песню на SoundCloud (`SOUNDCLOUD_FALLBACK=1`, по умолчанию выключено).

### Spotify и Last.fm
Звук всегда берётся с YouTube. Spotify и Last.fm дают только «что за
трек», а бот перед игрой находит его на YouTube Music.
- **Ссылки Spotify** (трек/альбом/плейлист) работают без ключей.
- **Поиск** по Spotify/Last.fm требует ключей в `.env` (`SPOTIFY_CLIENT_ID` +
  `SPOTIFY_CLIENT_SECRET`, `LASTFM_API_KEY`) — см. `.env.example`.
- С `LASTFM_API_KEY` и `DEFAULT_SEARCH=lastfm` (или `source: Last.fm`) подсказки
  в `/play` идут из Last.fm.

## Структура проекта

```
music-bot/
├── main.py             # точка входа, настройка бота и синхронизация команд
├── config.py           # загрузка .env, поиск ffmpeg/deno на хостинге
├── requirements.txt
├── .env.example
├── Dockerfile
├── CHANGELOG.md
├── test_bot.py         # офлайн-тесты логики: python test_bot.py
├── test_audio.py       # тесты звукового тракта (нужен FFmpeg): python test_audio.py
└── music/
    ├── track.py        # dataclass Track / SearchResult
    ├── queue.py        # GuildMusicState — очередь и состояние сервера
    ├── player.py       # MusicPlayer — подготовка треков, предзагрузка, цикл воспроизведения
    ├── ytdl.py         # yt-dlp в отдельных процессах (поиск, метаданные, скачивание)
    ├── sources.py      # поиск: YouTube, YouTube Music, Spotify, Last.fm, SoundCloud
    ├── updater.py      # автообновление yt-dlp
    ├── pot.py          # PO Token для YouTube (bgutil-pot, по умолчанию выключен)
    ├── ui.py           # кнопки живой панели
    └── cog.py          # MusicCog — слэш-команды
```

## Архитектура

- **`Track`** (`track.py`) — `@dataclass(slots=True)`: метаданные трека и ссылка
  на его страницу. У треков из Spotify/Last.fm ссылки нет — её найдёт плеер.
- **`GuildMusicState`** (`queue.py`) — очередь (`deque`), текущий трек,
  `asyncio.Event` для управления плеером. Один экземпляр на сервер.
- **`MusicPlayer`** (`player.py`) — готовит трек (находит на YT Music, берёт прямой
  поток или качает через `yt-dlp` во временный файл) и крутит `player_loop`.
  Opus 48 кГц с пакетами по 20 мс (так отдаёт YouTube) идёт в голос без
  перекодирования; остальное перекодируется в stereo Opus 256 кбит/с (`AUDIO_BITRATE`).
  yt-dlp работает отдельными процессами (`ytdl.py`), поэтому не тормозит отправку звука.
- **`MusicCog`** (`cog.py`) — слэш-команды и словарь состояний по серверам.

---

## 1. Установка FFmpeg

FFmpeg обязателен — через него идёт декодирование аудио.

### Windows

**Вариант А — winget (проще всего):**
```powershell
winget install Gyan.FFmpeg
```
Перезапусти терминал и проверь: `ffmpeg -version`.

**Вариант Б — вручную:**
1. Скачай сборку с https://www.gyan.dev/ffmpeg/builds/ (`ffmpeg-release-essentials.zip`).
2. Распакуй, например, в `C:\ffmpeg`.
3. Добавь `C:\ffmpeg\bin` в переменную среды **PATH**
   (Параметры → Система → Доп. параметры системы → Переменные среды → Path → Изменить → Создать).
4. Перезапусти терминал, проверь: `ffmpeg -version`.

### Linux

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y ffmpeg

# Fedora
sudo dnf install -y ffmpeg

# Arch
sudo pacman -S ffmpeg
```
Проверка: `ffmpeg -version`.

> Если `ffmpeg` не в PATH, бот его не найдёт. Это самая частая причина «нет звука».

---

## 2. Создание Discord Application и получение токена

1. Зайди на **https://discord.com/developers/applications** → **New Application**, дай имя.
2. Слева открой вкладку **Bot** → **Add Bot** (Reset Token, если нужно) → скопируй **TOKEN**.
   Это и есть `DISCORD_TOKEN` для `.env`. Никому его не показывай.
3. Привилегированные интенты (**Server Members**, **Message Content**) **не нужны** —
   бот работает на слэш-командах.
4. Слева **OAuth2 → URL Generator**:
   - **Scopes:** отметь `bot` и `applications.commands`.
   - **Bot Permissions:** `View Channels`, `Connect`, `Speak`,
     `Send Messages`, `Use Slash Commands`.
5. Скопируй сгенерированную ссылку внизу, открой её в браузере и пригласи бота
   на свой сервер.
6. *(Опционально, для мгновенных команд)* включи в Discord **Режим разработчика**
   (Настройки → Расширенные), кликни правой кнопкой по серверу → **Копировать ID
   сервера** — это `GUILD_ID` для `.env`.

---

## 3. Запуск бота

```bash
# 1. Перейти в папку проекта
cd music-bot

# 2. (рекомендуется) Виртуальное окружение
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

# 3. Установить зависимости
pip install -r requirements.txt

# 4. Создать .env из примера и вписать токен
#   Windows:  copy .env.example .env
#   Linux:    cp .env.example .env
#   затем открой .env и впиши DISCORD_TOKEN (и при желании GUILD_ID)

# 5. Запуск
python main.py
```

После старта в логах появится `✅ Бот онлайн: ...`. Заходи в голосовой канал
и пиши `/play <название песни>`.

### Замечания
- Если задан `GUILD_ID` — слэш-команды появятся почти сразу. Без него —
  глобальная синхронизация может занять до часа (Discord так кэширует).
- `PyNaCl` обязателен для передачи голоса (уже в `requirements.txt`).
- Требуется **Python 3.12+**.

## Возможные проблемы

| Симптом | Причина / решение |
|---|---|
| Бот заходит в канал, но тишина | FFmpeg не в PATH — проверь `ffmpeg -version` |
| На хостинге трек «начинается» и тут же пропадает (Connection timed out к googlevideo) | Датацентровый IP заблокирован YouTube — поставь `FORCE_PIPE=1` в `.env`: аудио пойдёт через yt-dlp pipe, минуя прямые ссылки |
| `/play` не находит команды | Подожди (глобальная синхр.) или задай `GUILD_ID` |
| `Could not find PyNaCl` | `pip install PyNaCl` |
| Трек обрывается | yt-dlp устарел — бот обновляет его сам; вручную: `pip install -U yt-dlp` |
| `403 / Sign in to confirm` от YouTube | Обнови yt-dlp; если не помогло — положи `cookies.txt` аккаунта YouTube рядом с ботом |
| «Поиск по Spotify не настроен» | Впиши ключи Spotify в `.env` или ищи через YouTube Music (ссылки Spotify работают и так) |

## Тесты

```bash
python test_bot.py      # очередь, плеер, кнопки, поиск — без Discord и интернета
python test_audio.py    # звуковой тракт
```

`test_audio.py` использует установленный FFmpeg с `libopus`, без Discord, токена и
интернета. Проверяются файл, прямой HTTP-поток на localhost и yt-dlp pipe
(подменён локальным процессом), mono/stereo Opus с пакетами по 20/60 мс,
AAC и PCM. На выходе должны быть только аудиопакеты по 20 мс в stereo.
Это не проверяет потери UDP-пакетов, нагрузку или DAVE на реальном хостинге.
