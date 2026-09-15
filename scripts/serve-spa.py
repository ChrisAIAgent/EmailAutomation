"""Loopback-only static server with SPA fallback for bundled TACWork Web."""

from __future__ import annotations

import argparse
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


class SpaHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        relative = urlsplit(self.path).path.lstrip("/")
        candidate = Path(self.directory or ".") / relative
        if relative and not candidate.exists():
            relative = "index.html"
        if relative in ("", "index.html"):
            index = (Path(self.directory or ".") / "index.html").read_text(encoding="utf-8")
            bootstrap = (
                "<script>"
                f"localStorage.setItem('openwork.server.token',{json.dumps(self.server.client_token)});"
                f"localStorage.setItem('openwork.server.urlOverride',{json.dumps(self.server.server_url)});"
                f"localStorage.setItem('openwork.server.port',{json.dumps(str(self.server.server_port))});"
                f"localStorage.setItem('openwork.server.active',{json.dumps(self.server.opencode_url)});"
                f"localStorage.setItem('openwork.server.list',JSON.stringify([{json.dumps(self.server.opencode_url)}]));"
                "</script>"
            )
            payload = index.replace("</head>", bootstrap + "</head>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5173)
    parser.add_argument("--client-token", default="email-automation-local-v1")
    parser.add_argument("--opencode-url", default="http://127.0.0.1:8787/opencode")
    args = parser.parse_args()
    root = Path(args.root).resolve(strict=True)
    if not (root / "index.html").is_file():
        raise SystemExit(f"index.html not found under {root}")
    os.chdir(root)
    server = ThreadingHTTPServer((args.host, args.port), SpaHandler)
    server.client_token = args.client_token
    server.opencode_url = args.opencode_url
    server.server_url = args.opencode_url.removesuffix("/opencode").rstrip("/")
    server.server_port = args.opencode_url.rsplit(":", 1)[-1].split("/", 1)[0]
    print(f"TACWork Web listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
