#!/usr/bin/env python3
"""Minimal Nano Banana Pro generation via kie.ai API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE = os.getenv("KIE_API_BASE", "https://api.kie.ai").rstrip("/")
API_KEY = os.getenv("KIE_API_KEY", "").strip()
CREATE_URL = f"{API_BASE}/api/v1/jobs/createTask"
STATUS_URL = f"{API_BASE}/api/v1/jobs/recordInfo"
OUTPUT_DIR = Path("outputs")


def die(message: str, code: int = 1) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(code)


def headers() -> dict[str, str]:
    if not API_KEY:
        die("KIE_API_KEY is missing. Put it in .env (see .env.example).")
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
        },
    }
    if image_urls:
        payload["input"]["image_input"] = image_urls

    response = requests.post(CREATE_URL, headers=headers(), json=payload, timeout=60)
    try:
        body = response.json()
    except ValueError:
        die(f"Invalid JSON from createTask (HTTP {response.status_code}): {response.text[:300]}")

    if response.status_code != 200 or body.get("code") != 200:
        die(f"createTask failed: HTTP {response.status_code}, {body}")

    task_id = (body.get("data") or {}).get("taskId")
    if not task_id:
        die(f"No taskId in response: {body}")
    return task_id


def get_task(task_id: str) -> dict:
    response = requests.get(
        STATUS_URL,
        headers=headers(),
        params={"taskId": task_id},
        timeout=60,
    )
    try:
        body = response.json()
    except ValueError:
        die(f"Invalid JSON from recordInfo (HTTP {response.status_code}): {response.text[:300]}")

    if body.get("code") not in (200, 505) and response.status_code != 200:
        die(f"recordInfo failed: HTTP {response.status_code}, {body}")

    data = body.get("data")
    if not isinstance(data, dict):
        die(f"Unexpected recordInfo payload: {body}")
    return data


def wait_for_result(task_id: str, *, timeout_sec: int, interval_sec: float) -> dict:
    started = time.time()
    while True:
        data = get_task(task_id)
        state = data.get("state", "unknown")
        print(f"[{task_id}] state={state}")

        if state == "success":
            return data
        if state == "fail":
            die(f"Generation failed: {data.get('failCode')} {data.get('failMsg')}")

        if time.time() - started > timeout_sec:
            die(f"Timed out after {timeout_sec}s (last state={state})")

        time.sleep(interval_sec)


def extract_urls(data: dict) -> list[str]:
    raw = data.get("resultJson") or "{}"
    if isinstance(raw, dict):
        payload = raw
    else:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            die(f"Cannot parse resultJson: {raw!r}")

    urls = payload.get("resultUrls") or []
    if not urls:
        die(f"No resultUrls in result: {payload}")
    return urls


def download(url: str, dest: Path) -> Path:
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    dest.write_bytes(response.content)
    return dest


def guess_extension(url: str, fallback: str) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        return suffix
    return f".{fallback.lstrip('.')}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an image with Nano Banana Pro (kie.ai)")
    parser.add_argument("prompt", nargs="?", help="Text prompt for generation")
    parser.add_argument("--aspect-ratio", default="1:1", help="e.g. 1:1, 16:9, 9:16")
    parser.add_argument("--resolution", default="1K", choices=["1K", "2K", "4K"])
    parser.add_argument("--format", dest="output_format", default="png", choices=["png", "jpg"])
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        help="Reference image URL (can be repeated, up to 8)",
    )
    parser.add_argument("--timeout", type=int, default=300, help="Max wait time in seconds")
    parser.add_argument("--interval", type=float, default=3.0, help="Polling interval in seconds")
    parser.add_argument("--no-download", action="store_true", help="Only print result URLs")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    prompt = args.prompt or "A simple red apple on a white background, studio photo"
    if args.image and len(args.image) > 8:
        die("image_input supports at most 8 URLs")

    print(f"Creating task: model=nano-banana-pro resolution={args.resolution}")
    task_id = create_task(
        prompt,
        aspect_ratio=args.aspect_ratio,
        resolution=args.resolution,
        output_format=args.output_format,
        image_urls=args.image or None,
    )
    print(f"taskId={task_id}")

    data = wait_for_result(task_id, timeout_sec=args.timeout, interval_sec=args.interval)
    urls = extract_urls(data)
    print("Result URLs:")
    for url in urls:
        print(f"  {url}")

    if args.no_download:
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for index, url in enumerate(urls, start=1):
        ext = guess_extension(url, args.output_format)
        dest = OUTPUT_DIR / f"{task_id}_{index}{ext}"
        download(url, dest)
        print(f"Saved: {dest}")


if __name__ == "__main__":
    main()
