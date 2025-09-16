# send_local_file.py
import os
import requests
import mimetypes
from pathlib import Path
import subprocess
import tempfile

CHUNK_SIZE = 2 * 1024 * 1024  # 2 MB

try:
    # Optional: load .env if present
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN") or "ВСТАВЬ_ТОКЕН_БОТА"
CHAT_ID = os.getenv("CHAT_ID") or "7978672112"  # свой user/chat/group id
FILEPATH = (
    os.getenv("FILEPATH")
    or "download/Building The ＂Dream Setup＂  (MacBook M4 Pro Unboxing + 34” ProArt) [pfQm2VCAa6Y].mp4"
)

BASE = f"http://127.0.0.1:8081/bot{BOT_TOKEN}"  # локальный Bot API


def ffprobe_meta(path: str):
    """Return (width, height, duration) via ffprobe; fall back to (None, None, None) if unavailable."""
    try:
        cmd = [
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
        ]
        out = (
            subprocess.check_output(cmd, stderr=subprocess.STDOUT)
            .decode("utf-8", "ignore")
            .strip()
            .splitlines()
        )
        # Expected lines: width, height, duration
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
    """Create a JPEG thumbnail at ~1s using ffmpeg; return path or None."""
    try:
        tmpdir = tempfile.gettempdir()
        base = os.path.splitext(os.path.basename(video_path))[0]
        out = os.path.join(tmpdir, f"{base}.jpg")
        cmd = [
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
        ]
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
    except Exception:
        pass
    return None


def main():
    if not os.path.isfile(FILEPATH):
        raise SystemExit(f"Файл не найден: {FILEPATH}")

    path = Path(FILEPATH)
    ext = path.suffix.lower()
    mime, _ = mimetypes.guess_type(str(path))
    is_video = (mime or "").startswith("video/")
    is_mp4 = ext == ".mp4"

    if is_video and is_mp4:
        w, h, dur = ffprobe_meta(FILEPATH)
        thumb_path = make_thumbnail(FILEPATH)
        files = {
            "video": (path.name, open(FILEPATH, "rb"), "video/mp4"),
        }
        if thumb_path:
            files["thumbnail"] = (
                os.path.basename(thumb_path),
                open(thumb_path, "rb"),
                "image/jpeg",
            )
        data = {
            "chat_id": CHAT_ID,
            "caption": path.name,
            "supports_streaming": True,
        }
        if w:
            data["width"] = int(w)
        if h:
            data["height"] = int(h)
        if dur:
            data["duration"] = int(dur)
        url = f"{BASE}/sendVideo"
    else:
        files = {
            "document": (path.name, open(FILEPATH, "rb"), "application/octet-stream")
        }
        data = {"chat_id": CHAT_ID, "caption": path.name}
        url = f"{BASE}/sendDocument"

    r = requests.post(url, data=data, files=files, timeout=1800)
    print(r.status_code, r.text)


if __name__ == "__main__":
    main()
