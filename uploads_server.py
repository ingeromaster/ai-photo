"""Tiny public HTTP server for /uploads (needed by kie.ai image_input URLs)."""

from __future__ import annotations

import logging
import mimetypes
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from kie_client import UPLOADS_DIR

log = logging.getLogger("uploads-server")


class UploadsOnlyHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, uploads_dir: Path, **kwargs):
        self.uploads_dir = uploads_dir.resolve()
        super().__init__(*args, directory=str(self.uploads_dir), **kwargs)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        log.info("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(self.path.split("?", 1)[0])
        if path in {"/", "/health"}:
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        prefix = "/uploads/"
        if not path.startswith(prefix):
            self.send_error(404, "Not Found")
            return

        # Serve file relative to uploads dir
        self.path = "/" + path[len(prefix) :]
        return super().do_GET()

    def guess_type(self, path: str) -> str:
        guessed, _ = mimetypes.guess_type(path)
        return guessed or "application/octet-stream"


def start_uploads_server(host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    handler = partial(UploadsOnlyHandler, uploads_dir=UPLOADS_DIR)
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="uploads-http", daemon=True)
    thread.start()
    log.info("Uploads server listening on http://%s:%s/uploads/", host, port)
    return server
