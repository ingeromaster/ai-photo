#!/usr/bin/env python3
"""Minimal web UI + API for Nano Banana Pro (kie.ai)."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
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
    "application/octet-stream",  # iOS sometimes sends HEIC this way
}
ALLOWED_RESOLUTIONS = {"1K", "2K", "4K"}
ALLOWED_ASPECT_RATIOS = {
    "1:1",
    "2:3",
    "3:2",
    "3:4",
    "4:3",
    "4:5",
    "5:4",
    "9:16",
    "16:9",
    "21:9",
    "auto",
}
MAX_REFERENCE_IMAGES = 8
MAX_FILE_BYTES = 15 * 1024 * 1024  # practical cap (kie allows up to 30MB)

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = MAX_REFERENCE_IMAGES * MAX_FILE_BYTES


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
    # octet-stream is allowed only for HEIC/HEIF filenames
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


def convert_heic_to_jpeg(storage, dest: Path) -> None:
    """Decode HEIC/HEIF (iPhone) and save as JPEG."""
    try:
        image = Image.open(storage.stream)
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
            f"Не удалось конвертировать HEIC/HEIF в JPEG: {storage.filename}. "
            f"{'На сервере нет поддержки HEIC.' if not HEIF_SUPPORTED else ''}"
            f" Детали: {exc}"
        ) from exc


def save_reference_uploads(files) -> list[str]:
    """Save uploaded references and return public URLs for kie.ai.

    HEIC/HEIF are converted to JPEG; JPG/PNG/WEBP are stored unchanged.
    """
    if not files:
        return []
    if len(files) > MAX_REFERENCE_IMAGES:
        raise ValueError(f"Можно прикрепить не больше {MAX_REFERENCE_IMAGES} фото")

    urls: list[str] = []
    for storage in files:
        if not storage or not storage.filename:
            continue
        mime = (storage.mimetype or "").lower()
        if not is_allowed_upload(storage.filename, mime):
            raise ValueError(
                f"Неверный тип файла: {storage.filename} ({mime or 'unknown'}). "
                "Допустимы JPG, PNG, WEBP, HEIC/HEIF"
            )

        storage.stream.seek(0, os.SEEK_END)
        size = storage.stream.tell()
        storage.stream.seek(0)
        if size <= 0:
            raise ValueError(f"Пустой файл: {storage.filename}")
        if size > MAX_FILE_BYTES:
            raise ValueError(
                f"Файл слишком большой (макс. {MAX_FILE_BYTES // (1024 * 1024)} МБ): {storage.filename}"
            )

        stem = f"{int(time.time())}_{uuid.uuid4().hex[:10]}"
        if is_heic_upload(storage.filename, mime):
            filename = f"{stem}.jpg"
            dest = UPLOADS_DIR / filename
            convert_heic_to_jpeg(storage, dest)
        else:
            ext = extension_for_passthrough(storage.filename, mime)
            filename = f"{stem}{ext}"
            dest = UPLOADS_DIR / filename
            storage.save(dest)

        urls.append(f"{PUBLIC_BASE_URL}/uploads/{filename}")

    return urls


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/generated/<path:filename>")
def generated_file(filename: str):
    return send_from_directory(GENERATED_DIR, filename, as_attachment=False)


@app.get("/uploads/<path:filename>")
def uploaded_file(filename: str):
    return send_from_directory(UPLOADS_DIR, filename, as_attachment=False)


@app.get("/download/<path:filename>")
def download_file(filename: str):
    return send_from_directory(GENERATED_DIR, filename, as_attachment=True)


@app.post("/api/generate")
def api_generate():
    if not API_KEY:
        return jsonify({"ok": False, "error": "KIE_API_KEY is missing on server"}), 500

    # Support JSON (no files) and multipart (with reference images)
    if request.content_type and "multipart/form-data" in request.content_type:
        prompt = (request.form.get("prompt") or "").strip()
        aspect_ratio = request.form.get("aspect_ratio") or "1:1"
        resolution = request.form.get("resolution") or "1K"
        output_format = request.form.get("output_format") or "png"
        files = request.files.getlist("images")
    else:
        payload = request.get_json(silent=True) or {}
        prompt = (payload.get("prompt") or "").strip()
        aspect_ratio = payload.get("aspect_ratio") or "1:1"
        resolution = payload.get("resolution") or "1K"
        output_format = payload.get("output_format") or "png"
        files = []

    if not prompt:
        return jsonify({"ok": False, "error": "Введите промпт"}), 400
    if len(prompt) > 10000:
        return jsonify({"ok": False, "error": "Промпт слишком длинный"}), 400
    if resolution not in ALLOWED_RESOLUTIONS:
        return jsonify({"ok": False, "error": "Неверное качество (нужно 1K, 2K или 4K)"}), 400
    if aspect_ratio not in ALLOWED_ASPECT_RATIOS:
        return jsonify({"ok": False, "error": "Неверный формат кадра"}), 400

    try:
        image_urls = save_reference_uploads(files)
        task_id = create_task(
            prompt,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            output_format=output_format,
            image_urls=image_urls,
        )
        data = wait_for_result(task_id)
        urls = extract_urls(data)

        saved = []
        for index, url in enumerate(urls, start=1):
            ext = guess_extension(url, output_format)
            filename = f"{int(time.time())}_{safe_stem(prompt)}_{uuid.uuid4().hex[:8]}_{index}{ext}"
            dest = GENERATED_DIR / filename
            image_response = requests.get(url, timeout=120)
            image_response.raise_for_status()
            dest.write_bytes(image_response.content)
            saved.append(
                {
                    "filename": filename,
                    "view_url": f"/generated/{filename}",
                    "download_url": f"/download/{filename}",
                    "source_url": url,
                }
            )

        return jsonify(
            {
                "ok": True,
                "taskId": task_id,
                "reference_urls": image_urls,
                "images": saved,
            }
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 - return error to UI for test page
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
