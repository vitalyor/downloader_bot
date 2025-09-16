from yt_dlp import YoutubeDL
import time
from typing import List, Dict, Any, Optional
import os


url = "https://www.instagram.com/reels/DOk9ag9COH1/"

COOKIE_PATH = "/Users/vitalyor/Downloads/downloader/cookies/inst_cookies.txt"
if not os.path.isfile(COOKIE_PATH):
    raise SystemExit(
        f"Файл cookies не найден: {COOKIE_PATH}. Экспортируй куки в Netscape-формате и попробуй снова."
    )


# --- helpers for quality selection ---
def human_size(n: Optional[int]) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def pick_quality(url: str, base_opts: Dict[str, Any]) -> str:
    # 1) вытащим форматы без скачивания
    probe_opts = dict(base_opts)
    probe_opts.update(
        {
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
        }
    )
    last_err = None
    for attempt in range(1, 4):  # до 3 попыток через прокси
        try:
            print(f"[probe] попытка {attempt}… proxy={probe_opts.get('proxy')}")
            with YoutubeDL(probe_opts) as y:
                info = y.extract_info(url, download=False)
            break
        except Exception as e:
            last_err = e
            print(f"[probe] ошибка: {type(e).__name__}: {e}")
            if attempt < 3:
                time.sleep(2 * attempt)
            else:
                raise

    formats: List[Dict[str, Any]] = info.get("formats", [])

    # выберем лучший аудио-формат (предпочтение m4a/aac, затем по abr)
    audio_candidates = [
        f
        for f in formats
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]

    def audio_score(f):
        ext = (f.get("ext") or "").lower()
        pref = 2 if ext in ("m4a", "mp4", "aac") else 1
        return (pref, f.get("abr") or 0)

    best_audio = max(audio_candidates, key=audio_score) if audio_candidates else None

    # подготовим варианты для пользователя
    options = []  # each: {label, format_str, size}
    for f in formats:
        if (f.get("ext") or "").lower() != "mp4":
            continue
        if f.get("vcodec") in (None, "none"):
            # это чисто аудио — пропустим, мы предлагаем видео-варианты
            continue
        height = f.get("height")
        fps = f.get("fps")
        vext = f.get("ext")
        v_id = f.get("format_id")
        acodec = f.get("acodec")
        vcodec = f.get("vcodec")
        tbr = f.get("tbr")
        size = f.get("filesize") or f.get("filesize_approx")

        if acodec and acodec != "none":
            # прогрессивный поток (со звуком)
            label = f"{height}p{'' if not fps else f'{int(fps)}fps '}({vext}) — prog — ~{human_size(size)}"
            options.append(
                {
                    "label": label,
                    "format_str": str(v_id),
                    "size": size or 0,
                }
            )
        else:
            # только видео — попробуем спарить с лучшим аудио
            if best_audio:
                a_id = best_audio.get("format_id")
                asize = (
                    best_audio.get("filesize") or best_audio.get("filesize_approx") or 0
                )
                total = (size or 0) + asize
                label = f"{height}p{'' if not fps else f'{int(fps)}fps '}({vext}) + bestaudio({best_audio.get('ext')}) — ~{human_size(total)}"
                options.append(
                    {
                        "label": label,
                        "format_str": f"{v_id}+{a_id}",
                        "size": total,
                    }
                )
            else:
                label = f"{height}p{'' if not fps else f'{int(fps)}fps '}({vext}) — video-only — ~{human_size(size)}"
                options.append(
                    {
                        "label": label,
                        "format_str": str(v_id),
                        "size": size or 0,
                    }
                )

    # уберём дубликаты по format_str и отсортируем по высоте/размеру
    seen = set()
    uniq = []
    for opt in options:
        if opt["format_str"] in seen:
            continue
        seen.add(opt["format_str"])
        uniq.append(opt)

    def height_from_label(lbl: str) -> int:
        # ищем число до 'p'
        try:
            part = lbl.split("p", 1)[0]
            digits = "".join(ch for ch in part if ch.isdigit())
            return int(digits) if digits else 0
        except Exception:
            return 0

    uniq.sort(
        key=lambda o: (height_from_label(o["label"]), o.get("size", 0)), reverse=True
    )

    # если mp4-вариантов не нашли — вернём безопасный дефолт
    if not uniq:
        print("Не нашли MP4-видеоформатов. Беру лучший доступный (bv*+ba/best).")
        return "bv*+ba/best"

    # покажем список
    print("Доступные качества:")
    for i, opt in enumerate(uniq, 1):
        print(f"  {i}. {opt['label']}  [fmt: {opt['format_str']}]")

    # выбор пользователя
    while True:
        sel = input(f"Выбери номер качества (1–{len(uniq)}), Enter для 1: ").strip()
        if sel == "":
            choice = 1
            break
        if sel.isdigit():
            n = int(sel)
            if 1 <= n <= len(uniq):
                choice = n
                break
        print("Некорректный выбор. Попробуй ещё раз.")

    return uniq[choice - 1]["format_str"]


ydl_opts = {
    "proxy": "http://proxy_user:cDi8s3rLZYeFHkU5@195.181.242.80:30960",
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
    "prefer_ipv4": True,
    "nocheckcertificate": True,
    "retries": 10,
    "fragment_retries": 10,
    "file_access_retries": 10,
    "cookiefile": COOKIE_PATH,
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
    },
    "extractor_args": {
        "instagram": {
            "locale": ["en_US"],
            "prefer_authorized": ["True"],
        }
    },
    "geo_bypass": True,
    "merge_output_format": "mp4",
    "postprocessors": [
        {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"},
        {
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        },
    ],
}

# ydl_opts = {
#     "proxy": "socks5h://proxy_user:cDi8s3rLZYeFHkU5@195.181.242.80:30960",
#     "concurrent_fragments": 4,
#     "http_chunk_size": 32 * 1024 * 1024,
#     "downloader": "aria2c",
#     "downloader_args": {
#         "aria2c": [
#             "--split=16",
#             "--max-connection-per-server=16",
#             "--min-split-size=1M"
#         ]
#     },
#     "socket_timeout": 30,
#     "retries": 10,
#     "fragment_retries": 10,
#     "file_access_retries": 10,
#     "cookies": "/Users/vitalyor/Downloads/downloader/cookies/inst_cookies.txt",
#     "geo_bypass": True,
# }


# сначала дадим пользователю выбрать качество и запишем его в опции
selected_format = pick_quality(url, ydl_opts)
ydl_opts["format"] = selected_format
print(f"\nВыбран формат: {selected_format}\n")

with YoutubeDL(ydl_opts) as ydl:
    start_time = time.time()
    ydl.download([url])
    end_time = time.time()
    duration = end_time - start_time
    print(f"Время скачивания: {duration:.2f} сек")
