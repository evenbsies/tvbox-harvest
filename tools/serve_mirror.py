"""Serve the repository's data/ directory for local gateway testing.

Single-threaded ``python -m http.server`` gets interrupted by the Worker's concurrent fetches and
reports "Network connection lost", which looks like a gateway bug. Hence ThreadingMixIn.
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import sys


class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args, **kwargs):  # keep the console readable
        return


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    root = sys.argv[1] if len(sys.argv) > 1 else "data"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8899
    print(f"serving {root} on http://127.0.0.1:{port}", flush=True)
    Server(("127.0.0.1", port), functools.partial(Handler, directory=root)).serve_forever()


if __name__ == "__main__":
    main()
