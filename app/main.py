import os
import asyncio
import pathlib
import uuid
import time
import io
from typing import Optional, List, Dict, Any

from yt_dlp import YoutubeDL
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import NetworkError, TimedOut, RetryAfter
from telegram.request import HTTPXRequest
import telegram.ext
import logging

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
COOKIEFILE = os.getenv("COOKIEFILE")  # путь до cookies.txt (формат Netscape)
DOWNLOAD_TIMEOUT = int(
    os.getenv("DOWNLOAD_TIMEOUT", "7200")
)  # сек, общий таймаут скачивания (по умолчанию 2ч)

FORCE_DOCUMENT = os.getenv("FORCE_DOCUMENT", "false").lower() in {"1", "true", "yes"}
TG_READ_TIMEOUT = int(
    os.getenv("TG_READ_TIMEOUT", "1200")
)  # чтение при аплоаде (сек), по умолчанию 20 мин
TG_WRITE_TIMEOUT = int(os.getenv("TG_WRITE_TIMEOUT", "1200"))  # запись/аплоад (сек)


PROGRESS_INTERVAL = float(
    os.getenv("PROGRESS_INTERVAL", "1.0")
)  # сек между логами аплоада

DOWNLOAD_DIR = os.getenv(
    "DOWNLOAD_DIR", os.path.join(os.path.dirname(__file__), "download")
)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

HELP_TEXT = (
    "Пришли мне ссылку на видео/риелс/тикток/ютуб — я предложу выбрать качество и пришлю файл.\n"
    "Если файл большой, отправлю как документ."
)

# Хранилище невысоких рисков: токен -> URL (живёт пока процесс бота жив)
PENDING_URLS: dict[str, str] = {}

# token -> list of (label, format_str)
PENDING_CHOICES: dict[str, List[tuple[str, str]]] = {}


def _probe_quality_options(
    url: str, cookiefile: Optional[str] = None
) -> List[tuple[str, str]]:
    """Возвращает список вариантов [(label, format_str)], отфильтрованных по mp4, отсортированных по качеству.
    label — то, что покажем на кнопке, format_str — что передадим в yt-dlp (например, "137+140" или "22").
    """
    probe_opts: Dict[str, Any] = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "geo_bypass": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            )
        },
    }
    if cookiefile:
        probe_opts["cookiefile"] = cookiefile
    with YoutubeDL(probe_opts) as y:
        info = y.extract_info(url, download=False)

    formats: List[Dict[str, Any]] = info.get("formats", [])

    # best audio (m4a/aac приоритет)
    audio_candidates = [
        f
        for f in formats
        if (f.get("acodec") not in (None, "none"))
        and (f.get("vcodec") in (None, "none"))
    ]

    def audio_score(f: Dict[str, Any]):
        ext = (f.get("ext") or "").lower()
        pref = 2 if ext in ("m4a", "mp4", "aac") else 1
        return (pref, f.get("abr") or 0)

    best_audio = max(audio_candidates, key=audio_score) if audio_candidates else None

    options: List[tuple[str, str]] = []
    seen_fmt: set[str] = set()

    for f in formats:
        # Только mp4-видео
        if (f.get("ext") or "").lower() != "mp4":
            continue
        if f.get("vcodec") in (None, "none"):
            continue
        height = f.get("height") or 0
        fps = f.get("fps")
        v_id = str(f.get("format_id"))
        vext = f.get("ext")
        acodec = f.get("acodec")
        size = f.get("filesize") or f.get("filesize_approx")

        if acodec and acodec != "none":
            # прогрессивный поток (видео+аудио в одном)
            label = f"{height}p{'' if not fps else f'{int(fps)}fps '}mp4"
            fmt = v_id
        else:
            # видео-only mp4 — попробуем объединить с лучшим аудио
            if best_audio:
                a_id = str(best_audio.get("format_id"))
                label = f"{height}p{'' if not fps else f'{int(fps)}fps '}mp4 + m4a"
                fmt = f"{v_id}+{a_id}"
            else:
                label = (
                    f"{height}p{'' if not fps else f'{int(fps)}fps '}mp4 (video-only)"
                )
                fmt = v_id

        if fmt in seen_fmt:
            continue
        seen_fmt.add(fmt)
        options.append((label, fmt))

    # Сортировка: по высоте (desc), затем по fps (desc)
    def parse_h(lbl: str) -> int:
        try:
            return int(lbl.split("p", 1)[0])
        except Exception:
            return 0

    def parse_fps(lbl: str) -> int:
        try:
            if "fps" in lbl:
                return int(lbl.split("p", 1)[1].split("fps", 1)[0].strip())
        except Exception:
            pass
        return 0

    options.sort(key=lambda x: (parse_h(x[0]), parse_fps(x[0])), reverse=True)

    # Если ничего не нашли (редко), добавим дефолт
    if not options:
        options.append(("🎥 Best", "bv*+ba/best"))
    return options


def _progress_hook(d):
    if d.get("status") == "downloading":
        p = d.get("downloaded_bytes") or 0
        t = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        spd = d.get("speed") or 0
        eta = d.get("eta")
        pct = (p / t * 100) if t else 0
        logger.info(
            f"DL: {pct:5.1f}% of {t/1024/1024/1024:.2f}GiB at {spd/1024/1024:.2f}MiB/s ETA {eta if eta is not None else '-'}"
        )
    elif d.get("status") == "finished":
        logger.info(f"DL finished, postprocessing: {d.get('filename')}")


def _download_video(
    url: str, quality: str = "best", format_override: Optional[str] = None
) -> str:
    """Скачивает видео по URL и возвращает путь к локальному файлу (mp4)."""
    logger.info(f"Начало скачивания: url={url}, quality={quality}")
    quality_map = {
        "best": "bv*+ba/best",
        "1080": "bv*[height<=1080]+ba/best[height<=1080]/best",
        "720": "bv*[height<=720]+ba/best[height<=720]/best",
        "480": "bv*[height<=480]+ba/best[height<=480]/best",
        "audio": "bestaudio/best",
    }
    selected_format = (
        format_override
        if format_override
        else quality_map.get(quality, quality_map["best"])
    )
    audio_only = (quality == "audio") and (format_override is None)

    outtmpl = os.path.join(DOWNLOAD_DIR, "%(title).80s [%(id)s].%(ext)s")

    ydl_opts = {
        # Сначала пробуем лучшее видео+аудио, иначе просто best
        "format": selected_format,
        # Принудительно делаем mp4, если возможно
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": False,
        "noprogress": False,
        "geo_bypass": True,
        # Добавим заголовки, чтобы меньше палиться под ботом
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            )
        },
        # Больше попыток при временных фейлах
        "retries": 5,
        "fragment_retries": 5,
        "retry_sleep": 2,
        # Комментарий: для некоторых источников может потребоваться cookies
        # "cookiefile": "cookies.txt",
        "postprocessors": (
            [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]
            if audio_only
            else [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
        ),
        "progress_hooks": [_progress_hook],
    }

    # Если задан путь к cookies.txt (формат Netscape), используем его
    if COOKIEFILE:
        ydl_opts["cookiefile"] = COOKIEFILE

    proxy = os.getenv("PROXY")
    if proxy:
        ydl_opts["proxy"] = proxy

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        logger.info(f"Завершено скачивание: {info.get('title')}")
        # Определяем итоговый путь файла
        final_path = None
        # Новый способ (yt-dlp >= 2023): requested_downloads[0]['filepath']
        rds = info.get("requested_downloads")
        if rds and isinstance(rds, list) and rds and isinstance(rds[0], dict):
            final_path = rds[0].get("filepath") or rds[0].get("_filename")
        # Старые поля на случай другой версии
        if not final_path:
            final_path = info.get("filepath") or info.get("_filename")
        # Если всё ещё не нашли — попробуем по шаблону из prepare_filename
        if not final_path:
            try:
                final_path = ydl.prepare_filename(info)
            except Exception:
                pass
        if not final_path:
            raise RuntimeError("Не удалось определить путь скачанного файла")
        # Если ремукс в mp4 — возможно расширение изменилось. Попробуем заменить на .mp4 при наличии такого файла.
        if final_path and final_path.endswith((".mkv", ".webm", ".m4a", ".mp3")):
            alt = os.path.splitext(final_path)[0] + ".mp4"
            if os.path.exists(alt):
                final_path = alt
        if not os.path.exists(final_path):
            # как запасной вариант посмотреть в каталоге скачивания файл с таким же base без учёта регистра
            base = os.path.splitext(os.path.basename(final_path))[0].lower()
            for p in pathlib.Path(DOWNLOAD_DIR).iterdir():
                if p.is_file() and os.path.splitext(p.name)[0].lower() == base:
                    final_path = str(p)
                    break
        if not os.path.exists(final_path):
            raise RuntimeError("Скачивание завершилось, но файл не найден в download/")
        logger.info(f"Файл сохранён: {final_path}")
        return final_path


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("👋 Привет! " + HELP_TEXT)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    url = update.message.text.strip()

    # Покажем кнопки выбора качества
    token = uuid.uuid4().hex[:12]
    PENDING_URLS[token] = url
    logger.info(f"Получена ссылка: {url}, token={token}")
    # Построим список доступных mp4-качейств для выбора
    choices = _probe_quality_options(url, COOKIEFILE)
    PENDING_CHOICES[token] = choices
    # Ограничим количество кнопок (например, до 12) и разложим по рядам по 3
    max_buttons = min(12, len(choices))
    rows: List[List[InlineKeyboardButton]] = []
    for i in range(max_buttons):
        label = choices[i][0]
        btn = InlineKeyboardButton(label, callback_data=f"pick|{token}|{i}")
        if not rows or len(rows[-1]) >= 3:
            rows.append([btn])
        else:
            rows[-1].append(btn)
    # Добавим базовые варианты
    rows.append([InlineKeyboardButton("🎥 Best", callback_data=f"pick|{token}|best")])
    rows.append(
        [InlineKeyboardButton("🎧 Audio (mp3)", callback_data=f"pick|{token}|audio")]
    )
    await update.message.reply_text(
        "Выбери качество загрузки:", reply_markup=InlineKeyboardMarkup(rows)
    )
    return


class ProgressFile(io.BufferedReader):
    def __init__(self, raw: io.BufferedReader, total_bytes: int, label: str = "upload"):
        super().__init__(raw)
        self._raw = raw
        self.total = total_bytes
        self.label = label
        self.start = time.time()
        self.last_log = 0.0
        self.read_so_far = 0
        self._filename = getattr(raw, "name", "upload.bin")

    def read(self, size: int = -1):
        chunk = super().read(size)
        if chunk:
            self.read_so_far += len(chunk)
            now = time.time()
            if (
                now - self.last_log
            ) >= PROGRESS_INTERVAL or self.read_so_far >= self.total:
                pct = (self.read_so_far / self.total * 100) if self.total else 0
                speed = self.read_so_far / max(1e-6, now - self.start)
                logger.info(
                    f"UP: {pct:5.1f}% of {self.total/1024/1024:.2f}MiB at {speed/1024/1024:.2f}MiB/s ({self.label})"
                )
                self.last_log = now
        return chunk

    @property
    def name(self):
        # Read-only alias for multipart filename; avoids assigning to BufferedReader.name
        return self._filename

    def seek(self, offset, whence=io.SEEK_SET):
        return self._raw.seek(offset, whence)

    def tell(self):
        return self._raw.tell()

    def readable(self):
        return True


async def _send_with_retries(send_coro_factory, attempts: int = 3):
    delay = 3
    for i in range(attempts):
        try:
            return await send_coro_factory()
        except RetryAfter as e:
            await asyncio.sleep(getattr(e, "retry_after", delay))
        except (TimedOut, NetworkError):
            if i == attempts - 1:
                raise
            await asyncio.sleep(delay)
            delay *= 2


async def on_quality_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query or not update.callback_query.data:
        return
    q = update.callback_query
    try:
        action, token, third = q.data.split("|", 2)
    except ValueError:
        await q.answer("Некорректные данные", show_alert=True)
        return
    if action != "pick" or (token not in PENDING_URLS):
        await q.answer("Сессия не найдена", show_alert=True)
        return
    url = PENDING_URLS.pop(token)
    fmt_override: Optional[str] = None
    quality = "best"
    if third == "best":
        quality = "best"
    elif third == "audio":
        quality = "audio"
    else:
        # индекс варианта из PENDING_CHOICES
        try:
            idx = int(third)
        except Exception:
            await q.answer("Некорректный выбор", show_alert=True)
            return
        choices = PENDING_CHOICES.pop(token, [])
        if not choices or idx < 0 or idx >= len(choices):
            await q.answer("Вариант устарел", show_alert=True)
            return
        label, fmt_override = choices[idx]
        quality = "custom"
    logger.info(f"Выбор качества: {quality} для url={url}")
    await q.answer()
    status = await q.message.reply_text("⬇️ Скачиваю…")
    try:
        try:
            filepath = await asyncio.wait_for(
                asyncio.to_thread(_download_video, url, quality, fmt_override),
                timeout=DOWNLOAD_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                "Скачивание превысило лимит времени. Увеличь DOWNLOAD_TIMEOUT или выбери другое качество."
            )
        filename = os.path.basename(filepath)
        size = os.path.getsize(filepath)
        logger.info(
            f"Начало отправки файла: {filename}, size={size} байт, quality={quality}"
        )

        async def _send():
            if quality == "audio" or FORCE_DOCUMENT or size > 48 * 1024 * 1024:
                with open(filepath, "rb") as base_f:
                    pf = ProgressFile(base_f, size, label=filename)
                    msg = await q.message.reply_document(
                        document=InputFile(pf, filename=filename),
                        caption=filename,
                        read_timeout=TG_READ_TIMEOUT,
                        write_timeout=TG_WRITE_TIMEOUT,
                    )
                    logger.info(f"Отправлено (document): message_id={msg.message_id}")
                    return msg
            else:
                with open(filepath, "rb") as base_f:
                    pf = ProgressFile(base_f, size, label=filename)
                    msg = await q.message.reply_video(
                        video=InputFile(pf, filename=filename),
                        caption=filename,
                        read_timeout=TG_READ_TIMEOUT,
                        write_timeout=TG_WRITE_TIMEOUT,
                    )
                    logger.info(f"Отправлено (video): message_id={msg.message_id}")
                    return msg

        try:
            await _send_with_retries(_send)
            logger.info("Первичная отправка прошла успешно (получен ответ Telegram)")
        except (TimedOut, NetworkError, Exception) as e:
            logger.warning(f"Повторная отправка после ошибки: {e!r}")
            # Последняя попытка принудительно документом
            with open(filepath, "rb") as base_f:
                pf = ProgressFile(base_f, size, label=filename)
                msg = await q.message.reply_document(
                    document=InputFile(pf, filename=filename),
                    caption=filename,
                    read_timeout=TG_READ_TIMEOUT,
                    write_timeout=TG_WRITE_TIMEOUT,
                )
                logger.info(
                    f"Отправлено после фоллбека (document): message_id={msg.message_id}"
                )
        await status.delete()
        logger.info("Сообщение отправлено успешно.")
    except Exception as e:
        msg = str(e)
        logger.error(f"Ошибка при обработке: {msg}", exc_info=True)
        hint = ""
        if any(k in msg.lower() for k in ("login required", "rate-limit", "cookies")):
            hint = (
                "\n\nℹ️ Для Instagram/закрытого контента часто нужен вход. "
                "Экспортируй cookies в формат Netscape и укажи путь через переменную окружения "
                "`COOKIEFILE=/absolute/path/to/cookies.txt`."
            )
        if "превысило лимит времени" in msg.lower() or "timed out" in msg.lower():
            hint += "\n\n⏱️ Можно увеличить DOWNLOAD_TIMEOUT (сек)."
        if (
            "proxy" in msg.lower()
            or "tls" in msg.lower()
            or "certificate" in msg.lower()
        ):
            hint += "\n\n🧭 При сетевых проблемах задай прокси через `PROXY=http://host:port` или `socks5://host:port`."
        await status.edit_text(f"❌ Ошибка: {msg}{hint}")
    finally:
        pass


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Установи BOT_TOKEN в переменных окружения или .env")

    request = HTTPXRequest(
        connect_timeout=60,
        read_timeout=TG_READ_TIMEOUT,
        write_timeout=TG_WRITE_TIMEOUT,
    )
    logger.info("Используется HTTPXRequest backend")
    app = Application.builder().token(BOT_TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(
        telegram.ext.CallbackQueryHandler(on_quality_choice, pattern=r"^pick\|")
    )

    logger.info("Бот запущен. Ожидание сообщений...")
    print("Bot is running… Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
