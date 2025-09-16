import os, re, time, json, subprocess, tempfile, requests, logging, traceback
from pathlib import Path
from yt_dlp import YoutubeDL
import uuid
from typing import List, Dict, Any, Tuple, Optional

try:
    from dotenv import load_dotenv
    from pathlib import Path as _P

    _ROOT = _P(__file__).resolve().parent
    # Try `.env` near this script first, then CWD; fall back to default search
    loaded = False
    for _path in (_ROOT / ".env", _ROOT / "tg-bot-api" / ".env", _P.cwd() / ".env"):
        try:
            if load_dotenv(_path, override=False):
                loaded = True
                break
        except Exception:
            pass
    if not loaded:
        # As a last resort, let python-dotenv search upwards from CWD
        load_dotenv(override=False)
except Exception:
    pass


# --------- Logging ----------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")


# Helper to require env variable and normalize quotes
def require_env(name: str) -> str:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        log.error("Отсутствует обязательная переменная окружения %s", name)
        raise RuntimeError(f"Переменная окружения {name} не задана")
    v = v.strip()
    if (len(v) >= 2) and v[0] == v[-1] and v[0] in ('"', "'"):
        v = v[1:-1]
    return v


# --------- Настройки окружения ----------
BOT_TOKEN = require_env("BOT_TOKEN")
BASE_URL = require_env("BASE_URL")  # локальный Bot API
OUT_DIR = require_env("OUT_DIR")
COOKIES = os.getenv("COOKIES")
os.makedirs(OUT_DIR, exist_ok=True)

log.info("BASE_URL=%s", BASE_URL)
log.info("OUT_DIR=%s", OUT_DIR)
if COOKIES:
    log.info("COOKIES=%s", COOKIES)

# yt-dlp опции — на основе твоего test.py
YDL_OPTS_BASE = {
    "concurrent_fragments": 4,
    "downloader": "aria2c",
    "downloader_args": {
        "aria2c": [
            "--split=16",
            "--max-connection-per-server=16",
            "--min-split-size=1M",
        ]
    },
    "socket_timeout": 30,
    "retries": 10,
    "fragment_retries": 10,
    "file_access_retries": 10,
    "geo_bypass": True,
    # Сразу делаем MP4; если нельзя ремукснуть — сконвертируем
    "merge_output_format": "mp4",
    "postprocessors": [
        {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"},
        {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"},
    ],
    # Предпочитаем H.264, потом разрешение/кадровую
    "format_sort": ["codec:avc1", "res", "fps", "br"],
    # Выходной шаблон
    "outtmpl": os.path.join(OUT_DIR, "%(title)s [%(id)s].%(ext)s"),
    # Не шумим лишним
    "quiet": True,
    "no_warnings": True,
}

if COOKIES and os.path.isfile(COOKIES):
    YDL_OPTS_BASE["cookiefile"] = COOKIES

URL_RE = re.compile(r"https?://\S+")

# token -> (url, choices). choices is a list of (label, format_str)
PENDING: Dict[str, Tuple[str, List[Tuple[str, str]]]] = {}


def ffprobe_meta(path: str):
    """Вернёт (width, height, duration) или (None, None, None)."""
    try:
        out = (
            subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height:format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    path,
                ],
                stderr=subprocess.STDOUT,
            )
            .decode("utf-8", "ignore")
            .strip()
            .splitlines()
        )
        if len(out) >= 3:
            w = int(float(out[0])) if out[0] else None
            h = int(float(out[1])) if out[1] else None
            try:
                dur = int(float(out[2])) if out[2] else None
            except ValueError:
                dur = None
            return w, h, dur
    except Exception:
        pass
    return None, None, None


def make_thumbnail(video_path: str) -> str | None:
    """Делает jpeg-миниатюру на ~1.0с. Возвращает путь или None."""
    try:
        tmpdir = tempfile.gettempdir()
        base = os.path.splitext(os.path.basename(video_path))[0]
        out = os.path.join(tmpdir, f"{base}.jpg")
        subprocess.check_call(
            [
                "ffmpeg",
                "-y",
                "-ss",
                "1.0",
                "-i",
                video_path,
                "-vframes",
                "1",
                "-q:v",
                "2",
                out,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
    except Exception:
        pass
    return None


def _probe_mp4_choices(url: str) -> List[Tuple[str, str]]:
    """Return list of (label, format_str) for MP4-only video options.
    Label is like "2160p", "1440p", "1080p" (no extra letters).
    """
    log.info("Пробую получить доступные mp4 форматы: %s", url)
    opts = dict(YDL_OPTS_BASE)
    opts.update(
        {
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
    )
    # ensure we don't force aria2c for probing
    opts.pop("downloader", None)
    opts.pop("downloader_args", None)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    log.debug("Заголовок: %s | id: %s", info.get("title"), info.get("id"))
    formats: List[Dict[str, Any]] = info.get("formats", [])

    # pick best audio (m4a/aac preferred) for pairing with video-only mp4
    audio_candidates = [
        f
        for f in formats
        if (f.get("vcodec") in (None, "none"))
        and (f.get("acodec") not in (None, "none"))
    ]

    def _aud_score(f: Dict[str, Any]):
        ext = (f.get("ext") or "").lower()
        pref = 2 if ext in ("m4a", "mp4", "aac") else 1
        return (pref, f.get("abr") or 0)

    best_audio = max(audio_candidates, key=_aud_score) if audio_candidates else None

    # collect mp4 video formats
    candidates: List[Dict[str, Any]] = [
        f
        for f in formats
        if (f.get("vcodec") not in (None, "none"))
        and ((f.get("ext") or "").lower() == "mp4")
    ]

    # group by height and choose best by fps, then tbr
    by_h: Dict[int, Dict[str, Any]] = {}
    for f in candidates:
        h = int(f.get("height") or 0)
        cur = by_h.get(h)
        if not cur:
            by_h[h] = f
            continue

        # prefer higher fps, then higher total bitrate
        def key(ff):
            return (int(ff.get("fps") or 0), int(ff.get("tbr") or 0))

        if key(f) > key(cur):
            by_h[h] = f

    choices: List[Tuple[str, str]] = []
    for h, f in by_h.items():
        if h <= 0:
            continue
        v_id = str(f.get("format_id"))
        # if video has no audio, pair with best audio
        if (f.get("acodec") in (None, "none")) and best_audio:
            a_id = str(best_audio.get("format_id"))
            fmt = f"{v_id}+{a_id}"
        else:
            fmt = v_id
        label = f"{h}p"
        choices.append((label, fmt))

    # sort by height desc and ensure unique labels
    choices.sort(key=lambda x: int(x[0].rstrip("p") or 0), reverse=True)
    seen = set()
    uniq: List[Tuple[str, str]] = []
    for lbl, fmt in choices:
        if lbl in seen:
            continue
        seen.add(lbl)
        uniq.append((lbl, fmt))

    # fallback
    if not uniq:
        uniq.append(("best", "bv*+ba/best"))
    log.info("Найдено вариантов mp4: %d", len(uniq))
    return uniq


def ydl_download(url: str, format_override: Optional[str] = None) -> Path:
    """Скачивает видео лучшего доступного MP4 (со звуком), возвращает путь к файлу."""
    opts = dict(YDL_OPTS_BASE)

    if format_override:
        opts["format"] = format_override
    else:
        opts["format"] = (
            "bestvideo[ext=mp4][height<=2160]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/best"
        )
    t0 = time.time()
    used_fmt = format_override or "auto"
    log.info("Начинаю скачивание | формат=%s | url=%s", used_fmt, url)
    progress_msgs = {"last": 0}

    def _phook(d):
        try:
            if d.get("status") == "downloading":
                now = time.time()
                if now - progress_msgs["last"] >= 5:
                    log.info(
                        "Скачивание: %s | %s at %s/s | ETA %s",
                        d.get("_percent_str", "?"),
                        d.get("_downloaded_bytes_str", "?"),
                        d.get("_speed_str", "?"),
                        d.get("_eta_str", "?"),
                    )
                    progress_msgs["last"] = now
            elif d.get("status") == "finished":
                log.info("Скачивание завершено, начинаю постобработку…")
        except Exception:
            pass

    opts.setdefault("progress_hooks", []).append(_phook)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # yt_dlp возвращает итоговый путь тут:
        out_path = ydl.prepare_filename(info)
        # если был merge/convert — расширение может стать mp4
        out = Path(os.path.splitext(out_path)[0] + ".mp4")
        if not out.exists():
            # fallback: что реально было записано
            guessed = Path(out_path)
            if guessed.exists():
                out = guessed
            else:
                # попробуем из info
                if "requested_downloads" in info and info["requested_downloads"]:
                    out = Path(info["requested_downloads"][0].get("filepath", out_path))
        sz = out.stat().st_size if out.exists() else 0
        log.info(
            "Готов файл: %s (%.2f MB) за %.1f c",
            out.name,
            sz / 1024 / 1024,
            time.time() - t0,
        )
        return out


def send_video(chat_id: int, path: Path):
    """Отправка видео через локальный Bot API (как в upload.py, но без прогресса)."""
    log.info("Отправка видео в Telegram: %s", path)
    w, h, dur = ffprobe_meta(str(path))
    thumb_path = make_thumbnail(str(path))
    thumb_file = None
    try:
        with open(path, "rb") as video_file:
            files = {
                "video": (path.name, video_file, "video/mp4"),
            }
            if thumb_path and os.path.isfile(thumb_path):
                thumb_file = open(thumb_path, "rb")
                files["thumbnail"] = (
                    os.path.basename(thumb_path),
                    thumb_file,
                    "image/jpeg",
                )

            data = {
                "chat_id": str(chat_id),
                "caption": path.name,
                "supports_streaming": "true",
            }
            if w:
                data["width"] = int(w)
            if h:
                data["height"] = int(h)
            if dur:
                data["duration"] = int(dur)
            log.info(
                "HTTP POST sendVideo … (width=%s height=%s dur=%s thumb=%s)",
                w,
                h,
                dur,
                bool(thumb_path),
            )
            r = requests.post(
                f"{BASE_URL}/bot{BOT_TOKEN}/sendVideo",
                data=data,
                files=files,
                timeout=1800,
            )
            log.info("Ответ Bot API: %s", r.status_code)
            return r.status_code, r.text
    finally:
        if thumb_file is not None:
            try:
                thumb_file.close()
            except Exception:
                pass
        if thumb_path and os.path.isfile(thumb_path):
            try:
                os.remove(thumb_path)
                log.debug("Удалил временную миниатюру %s", thumb_path)
            except Exception as cleanup_err:
                log.warning(
                    "Не удалось удалить миниатюру %s: %s", thumb_path, cleanup_err
                )


def send_message(chat_id: int, text: str):
    try:
        log.debug("sendMessage → %s", text[:120])
        requests.post(
            f"{BASE_URL}/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": str(chat_id), "text": text},
            timeout=30,
        )
    except Exception:
        pass


def get_updates(offset=None, timeout=30):
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    log.debug("getUpdates(offset=%s, timeout=%s)", offset, timeout)
    r = requests.get(
        f"{BASE_URL}/bot{BOT_TOKEN}/getUpdates", params=params, timeout=timeout + 5
    )
    log.debug(
        "getUpdates ok=%s, items=%s",
        r.ok,
        (len(r.json().get("result", [])) if r.ok else "?"),
    )
    return r.json()


def handle_update(upd: dict):
    log.debug("handle_update: keys=%s", list(upd.keys()))
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

    log.info("Сообщение от %s: %s", chat_id, (text[:200] if text else "<no text>"))
    m = URL_RE.search(text)
    if not m:
        log.info("URL не найден в сообщении")
        send_message(chat_id, "Пришли ссылку на видео (YouTube и др.).")
        return

    url = m.group(0)
    log.info("URL: %s", url)
    # Probe choices and show inline buttons
    try:
        choices = _probe_mp4_choices(url)
    except Exception as e:
        log.exception("Ошибка при получении качеств")
        send_message(chat_id, f"Не удалось получить качества: {type(e).__name__}: {e}")
        return

    token = uuid.uuid4().hex[:12]
    PENDING[token] = (url, choices)
    # Build inline keyboard (max 12 buttons, rows of 3)
    kb_rows: List[List[Dict[str, str]]] = []
    for i, (lbl, _fmt) in enumerate(choices[:12]):
        if i % 3 == 0:
            kb_rows.append([])
        kb_rows[-1].append({"text": lbl, "callback_data": f"pick|{token}|{i}"})
    reply_markup = json.dumps({"inline_keyboard": kb_rows}, ensure_ascii=False)
    try:
        requests.post(
            f"{BASE_URL}/bot{BOT_TOKEN}/sendMessage",
            data={
                "chat_id": str(chat_id),
                "text": "Выбери качество:",
                "reply_markup": reply_markup,
            },
            timeout=30,
        )
        log.info("Показаны варианты качества (%d)", len(choices))
    except Exception:
        pass


def handle_callback(upd: dict):
    log.debug("handle_callback: data=%s", upd.get("callback_query", {}).get("data"))
    q = upd.get("callback_query")
    if not q:
        return
    data = q.get("data") or ""
    chat_id = q["message"]["chat"]["id"]
    msg_id = q["message"]["message_id"]
    try:
        action, token, idx_str = data.split("|", 2)
    except ValueError:
        return
    if action != "pick" or token not in PENDING:
        return
    url, choices = PENDING.pop(token)
    try:
        idx = int(idx_str)
    except Exception:
        return
    if not (0 <= idx < len(choices)):
        return
    label, fmt = choices[idx]
    log.info("Выбрано качество: %s (fmt=%s)", label, fmt)

    # acknowledge button
    try:
        requests.post(
            f"{BASE_URL}/bot{BOT_TOKEN}/answerCallbackQuery",
            data={"callback_query_id": q["id"], "text": f"Качество: {label}"},
            timeout=15,
        )
    except Exception:
        pass
    # edit message to show selection
    try:
        requests.post(
            f"{BASE_URL}/bot{BOT_TOKEN}/editMessageText",
            data={
                "chat_id": str(chat_id),
                "message_id": msg_id,
                "text": f"⬇️ Скачиваю {label}…",
            },
            timeout=30,
        )
    except Exception:
        pass

    # download with selected format, then upload
    try:
        log.info("Старт скачивания выбранного качества…")
        p = ydl_download(url, format_override=fmt)
        if p and p.exists():
            try:
                requests.post(
                    f"{BASE_URL}/bot{BOT_TOKEN}/editMessageText",
                    data={
                        "chat_id": str(chat_id),
                        "message_id": msg_id,
                        "text": "📤 Загрузка в Telegram…",
                    },
                    timeout=30,
                )
            except Exception:
                pass
            code = None
            body = ""
            try:
                code, body = send_video(chat_id, p)
            finally:
                try:
                    if p.exists():
                        p.unlink()
                        log.info("Удалил файл после отправки: %s", p)
                except Exception as cleanup_err:
                    log.warning("Не удалось удалить файл %s: %s", p, cleanup_err)
            if code == 200:
                # Успех: удаляем служебное сообщение, не пишем «Готово»
                try:
                    requests.post(
                        f"{BASE_URL}/bot{BOT_TOKEN}/deleteMessage",
                        data={"chat_id": str(chat_id), "message_id": msg_id},
                        timeout=30,
                    )
                    log.info("Отправка завершена, служебное сообщение удалено")
                except Exception:
                    log.error("Ошибка при удалении служебного сообщения")
            else:
                # Ошибка: показываем её в том же сообщении
                log.error("Ошибка отправки видео: %s %s", code, body[:500])
                try:
                    requests.post(
                        f"{BASE_URL}/bot{BOT_TOKEN}/editMessageText",
                        data={
                            "chat_id": str(chat_id),
                            "message_id": msg_id,
                            "text": f"❌ Ошибка отправки: {code}\n{body[:500]}",
                        },
                        timeout=30,
                    )
                except Exception:
                    log.error("Ошибка при отправке сообщения об ошибке отправки видео")
        else:
            log.error("Не удалось скачать файл для отправки")
            requests.post(
                f"{BASE_URL}/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": str(chat_id), "text": "Не удалось скачать файл 😕"},
                timeout=30,
            )
    except Exception as e:
        log.exception("Ошибка при скачивании или отправке видео")
        requests.post(
            f"{BASE_URL}/bot{BOT_TOKEN}/editMessageText",
            data={
                "chat_id": str(chat_id),
                "message_id": msg_id,
                "text": f"❌ Ошибка: {type(e).__name__}: {e}",
            },
            timeout=30,
        )


def main():
    log.info("Бот запущен. Жду сообщения…")
    last_update_id = None
    while True:
        try:
            data = get_updates(
                offset=(last_update_id + 1) if last_update_id else None, timeout=25
            )
            if not data.get("ok"):
                log.error("getUpdates error: %s", data)
                time.sleep(2)
                continue
            for upd in data.get("result", []):
                last_update_id = upd["update_id"]
                if "callback_query" in upd:
                    handle_callback(upd)
                else:
                    handle_update(upd)
        except KeyboardInterrupt:
            print("Остановлено пользователем.")
            break
        except Exception as e:
            log.exception("Loop error")
            time.sleep(2)


if __name__ == "__main__":
    main()
