"""Nano Banana Pro (kie.ai) client and reference image helpers."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_SUPPORTED = True
except Exception:  # noqa: BLE001
    HEIF_SUPPORTED = False

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
GENERATED_DIR = BASE_DIR / "generated"
UPLOADS_DIR = BASE_DIR / "uploads"
GENERATED_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

API_BASE = os.getenv("KIE_API_BASE", "https://api.kie.ai").rstrip("/")
API_KEY = os.getenv("KIE_API_KEY", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://62.113.110.123:8080").rstrip("/")
CREATE_URL = f"{API_BASE}/api/v1/jobs/createTask"
STATUS_URL = f"{API_BASE}/api/v1/jobs/recordInfo"

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
ALLOWED_MIMES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
    "image/heic-sequence",
    "image/heif-sequence",
    "application/octet-stream",
}
MAX_REFERENCE_IMAGES = 8
MAX_FILE_BYTES = 15 * 1024 * 1024


def auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }


def create_task(
    prompt: str,
    *,
    aspect_ratio: str,
    resolution: str,
    output_format: str,
    image_urls: list[str] | None = None,
) -> str:
    payload = {
        "model": "nano-banana-pro",
        "input": {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "output_format": output_format,
            "image_input": image_urls or [],
        },
    }
    response = requests.post(CREATE_URL, headers=auth_headers(), json=payload, timeout=60)
    body = response.json()
    if response.status_code != 200 or body.get("code") != 200:
        raise RuntimeError(f"createTask failed: HTTP {response.status_code}, {body}")
    task_id = (body.get("data") or {}).get("taskId")
    if not task_id:
        raise RuntimeError(f"No taskId in response: {body}")
    return task_id


def wait_for_result(task_id: str, *, timeout_sec: int = 300, interval_sec: float = 3.0) -> dict:
    started = time.time()
    while True:
        response = requests.get(
            STATUS_URL,
            headers=auth_headers(),
            params={"taskId": task_id},
            timeout=60,
        )
        body = response.json()
        data = body.get("data") or {}
        state = data.get("state", "unknown")

        if state == "success":
            return data
        if state == "fail":
            raise RuntimeError(f"Generation failed: {data.get('failCode')} {data.get('failMsg')}")
        if time.time() - started > timeout_sec:
            raise TimeoutError(f"Timed out after {timeout_sec}s (last state={state})")
        time.sleep(interval_sec)


def extract_urls(data: dict) -> list[str]:
    raw = data.get("resultJson") or "{}"
    payload = raw if isinstance(raw, dict) else json.loads(raw)
    urls = payload.get("resultUrls") or []
    if not urls:
        raise RuntimeError(f"No resultUrls in result: {payload}")
    return urls


def guess_extension(url: str, fallback: str = "png") -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        return suffix
    return f".{fallback.lstrip('.')}"


def safe_stem(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", text.strip())[:40].strip("-")
    return cleaned or "image"


def is_allowed_upload(filename: str, mime: str) -> bool:
    suffix = Path(filename or "").suffix.lower()
    if suffix in ALLOWED_EXTENSIONS:
        return True
    if mime in ALLOWED_MIMES and mime != "application/octet-stream":
        return True
    if mime == "application/octet-stream" and suffix in {".heic", ".heif"}:
        return True
    return False


def is_heic_upload(filename: str, mime: str) -> bool:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".heic", ".heif"}:
        return True
    return mime in {
        "image/heic",
        "image/heif",
        "image/heic-sequence",
        "image/heif-sequence",
    }


def extension_for_passthrough(filename: str, mime: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    mime_map = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }
    if mime in mime_map:
        return mime_map[mime]
    raise ValueError("Допустимы только JPG, PNG, WEBP или HEIC/HEIF")


def convert_heic_bytes_to_jpeg(data: bytes, dest: Path, *, filename: str = "image.heic") -> None:
    try:
        image = Image.open(BytesIO(data))
        image = ImageOps.exif_transpose(image)
        if image.mode in ("RGBA", "LA"):
            background = Image.new("RGB", image.size, (255, 255, 255))
            alpha = image.getchannel("A") if "A" in image.getbands() else None
            background.paste(image, mask=alpha)
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")
        image.save(dest, format="JPEG", quality=92, optimize=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(
            f"Не удалось конвертировать HEIC/HEIF в JPEG: {filename}. "
            f"{'На сервере нет поддержки HEIC.' if not HEIF_SUPPORTED else ''}"
            f" Детали: {exc}"
        ) from exc


def save_reference_bytes(data: bytes, filename: str, mime: str) -> str:
    """Save one reference image and return a public URL for kie.ai."""
    mime = (mime or "").lower()
    if not is_allowed_upload(filename, mime):
        raise ValueError(
            f"Неверный тип файла: {filename} ({mime or 'unknown'}). "
            "Допустимы JPG, PNG, WEBP, HEIC/HEIF"
        )
    if len(data) <= 0:
        raise ValueError(f"Пустой файл: {filename}")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(
            f"Файл слишком большой (макс. {MAX_FILE_BYTES // (1024 * 1024)} МБ): {filename}"
        )

    stem = f"{int(time.time())}_{uuid.uuid4().hex[:10]}"
    if is_heic_upload(filename, mime):
        out_name = f"{stem}.jpg"
        dest = UPLOADS_DIR / out_name
        convert_heic_bytes_to_jpeg(data, dest, filename=filename)
    else:
        ext = extension_for_passthrough(filename, mime)
        out_name = f"{stem}{ext}"
        dest = UPLOADS_DIR / out_name
        dest.write_bytes(data)

    return f"{PUBLIC_BASE_URL}/uploads/{out_name}"
