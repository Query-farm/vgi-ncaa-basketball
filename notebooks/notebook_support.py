"""Explicit replay/live upstream configuration; analysis stays in notebook SQL."""

import os
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import matplotlib.pyplot as plt
import seaborn as sns
from IPython.display import Markdown, display

ENVIRONMENT = ("NCAAB_ESPN_SITE_URL", "NCAAB_ESPN_WEB_URL", "NCAAB_ESPN_CORE_URL", "NCAAB_NCAA_API_URL")


class ReplayHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        self.server.requests.append(self.path)
        valid = parsed.path == "/apis/site/v2/sports/basketball/mens-college-basketball/summary" and parse_qs(
            parsed.query
        ) == {"event": ["401920686"]}
        body = self.server.document if valid else b'{"error":"unrecorded request"}'
        self.send_response(200 if valid else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def configure(mode, minimum):
    if mode not in ("replay", "live"):
        raise ValueError("DATA_MODE must be 'replay' or 'live'")
    if type(minimum) is not int or not 0 <= minimum <= 60:
        raise ValueError("MIN_MINUTES must be an integer from 0 through 60")
    previous = {key: os.environ.pop(key, None) for key in ENVIRONMENT}
    server = thread = None
    if mode == "replay":
        server = ThreadingHTTPServer(("127.0.0.1", 0), ReplayHandler)
        server.requests = []
        server.document = (Path(__file__).resolve().parent / "recordings/summary.json").read_bytes()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        for key in ENVIRONMENT:
            os.environ[key] = f"http://127.0.0.1:{server.server_port}"
    return server, thread, previous, datetime.now(UTC).isoformat()


def cleanup(server, thread, previous):
    if server:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def prose(text: str) -> None:
    display(Markdown(text))


def theme() -> None:
    sns.set_theme(
        style="whitegrid",
        rc={
            "figure.dpi": 145,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "grid.color": "#E8EDE9",
            "text.color": "#243B2F",
            "axes.labelcolor": "#405047",
            "text.parse_math": False,
            "legend.frameon": False,
        },
    )


def chart(fig, title: str, subtitle: str, source: str) -> None:
    fig.text(0.08, 0.98, title, ha="left", va="top", fontsize=17, fontweight="semibold")
    fig.text(0.08, 0.90, subtitle, ha="left", va="top", fontsize=10, color="#58685F")
    fig.tight_layout(rect=(0.02, 0.11, 1, 0.82))
    fig.text(0.08, 0.03, source, fontsize=8, color="#6C7D73")
    plt.show()
