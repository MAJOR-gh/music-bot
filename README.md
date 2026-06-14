# 🎵 Discord Music Bot

Личный музыкальный бот для Discord на **discord.py 2.x** + **yt-dlp** + **FFmpeg**.
Стримит аудио напрямую (без скачивания на диск), поддерживает YouTube, SoundCloud
и текстовый поиск. Очередь — отдельная для каждого сервера.

## Возможности

| Команда | Действие |
|---|---|
| `/join` | Подключиться к твоему голосовому каналу (или переместиться в него) |
| `/leave` | Отключиться и очистить очередь |
| `/play <запрос>` | Играть по ссылке (YouTube/SoundCloud) или искать по тексту (перелистываемый список вариантов) |
| `/skip` | Пропустить текущий трек |
| `/repeat [режим]` | Повтор: выкл / один трек / вся очередь (без аргумента — по кругу) |
| `/pause` | Пауза |
| `/resume` | Продолжить |
| `/stop` | Остановить и очистить очередь (бот остаётся в канале) |
| `/queue` | Показать очередь |
| `/nowplaying` | Что играет сейчас |

Дополнительно:
- 🔎 Текстовый поиск показывает до 25 вариантов (`SEARCH_RESULTS`), список
  листается по 10 на страницу кнопками ◀▶.
- 🔁 Повтор одного трека или всей очереди — командой `/repeat` или кнопкой на панели.
- ⏱️ Авто-отключение через 5 минут бездействия (настраивается `IDLE_TIMEOUT`).
- 👥 Авто-отключение, если в канале не осталось людей.
- 🔒 Защита от запуска нескольких плееров на один сервер.
- 🔁 Авто-переход к следующему треку, авто-reconnect потока FFmpeg.

## Структура проекта

```
music_bot/
├── main.py             # точка входа, настройка бота и синхронизация команд
├── config.py           # загрузка .env
├── requirements.txt
├── .env.example
├── README.md
└── music/
    ├── __init__.py
    ├── track.py        # dataclass Track
    ├── queue.py        # GuildMusicState — очередь и состояние сервера
    ├── player.py       # MusicPlayer — yt-dlp + цикл воспроизведения
    └── cog.py          # MusicCog — слэш-команды
```

## Архитектура

- **`Track`** (`track.py`) — `@dataclass(slots=True)`: метаданные трека и прямой
  URL аудиопотока (получен через `yt-dlp` с `download=False`).
- **`GuildMusicState`** (`queue.py`) — очередь (`deque`), текущий трек,
  `asyncio.Event` для управления плеером. Один экземпляр на сервер.
- **`MusicPlayer`** (`player.py`) — извлекает поток через `yt-dlp` (в executor,
  чтобы не блокировать event loop) и крутит `player_loop`, проигрывая треки через
  `discord.FFmpegOpusAudio`.
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
3. В разделе **Bot** включи тумблер **Server Members Intent** *(не обязателен,
   но желателен)*. **Message Content Intent НЕ нужен** — бот работает на слэш-командах.
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
cd music_bot

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
| `/play` не находит команды | Подожди (глобальная синхр.) или задай `GUILD_ID` |
| `Could not find PyNaCl` | `pip install PyNaCl` |
| Трек обрывается | yt-dlp устарел: `pip install -U yt-dlp` |
| `403 / Sign in to confirm` от YouTube | Обнови yt-dlp; YouTube периодически меняет защиту |
