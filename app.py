#!/usr/bin/env python3
"""
app.py — NewsDigest web server
Serves the digest as an HTML page and JSON API.

Usage:
  python app.py               # start on http://localhost:5050
  python app.py --port 8080   # custom port

Endpoints:
  GET  /              HTML digest page
  GET  /api/digest    JSON digest (for iPhone app)
  GET  /api/status    Operational status
  POST /api/run       Trigger a new fetch in the background
"""

import argparse
import json
import logging
import threading
from pathlib import Path

from flask import Flask, jsonify, make_response, request

from digest import (
    CONFIG_PATH,
    DATA_DIR,
    LATEST_PATH,
    build_digest,
    build_html_report,
    load_config,
)

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)

_CONFIG   = load_config()
_run_lock = threading.Lock()
_running  = False   # visible to /api/status


def _cors(response):
    """Add CORS header so the future iOS app can call the API."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


# ── HTML view ──────────────────────────────────────────────────────────────────

@app.route("/")
def view_digest():
    if LATEST_PATH.exists():
        try:
            digest = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
        except Exception:
            digest = {"generated_at": "", "topics": []}
    else:
        digest = {"generated_at": "", "topics": []}

    html = build_html_report(digest)
    resp = make_response(html, 200)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


# ── JSON API ───────────────────────────────────────────────────────────────────

@app.route("/api/digest")
def api_digest():
    if not LATEST_PATH.exists():
        resp = jsonify({"error": "No digest available yet.", "generated_at": None})
        resp.status_code = 404
        return _cors(resp)
    try:
        digest = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        resp = jsonify({"error": str(e)})
        resp.status_code = 500
        return _cors(resp)
    return _cors(jsonify(digest))


@app.route("/api/status")
def api_status():
    generated_at = None
    if LATEST_PATH.exists():
        try:
            data = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
            generated_at = data.get("generated_at")
        except Exception:
            pass

    resp = jsonify({
        "status":           "ok",
        "latest_digest_at": generated_at,
        "running":          _running,
        "topics":           _CONFIG.get("topics", []),
    })
    return _cors(resp)


@app.route("/api/config", methods=["GET"])
def api_config_get():
    global_max = _CONFIG.get("max_articles_per_topic", 5)
    feeds = _CONFIG.get("rss_feeds", [])
    resp = jsonify({
        "max_articles_per_topic": global_max,
        "topics":                 _CONFIG.get("topics", []),
        "schedule_time":          _CONFIG.get("schedule", {}).get("time", "07:00"),
        "feeds": [
            {"name": f.get("name", ""), "max_articles": f.get("max_articles", global_max)}
            for f in feeds
        ],
    })
    return _cors(resp)


@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.get_json(silent=True) or {}
    changed = False

    if "max_articles_per_topic" in data:
        val = int(data["max_articles_per_topic"])
        if val < 1:
            return _cors(jsonify({"error": "min 1"})), 400
        _CONFIG["max_articles_per_topic"] = val
        changed = True

    if "feed_max_articles" in data:
        updates = data["feed_max_articles"]  # {feed_name: n}
        for feed in _CONFIG.get("rss_feeds", []):
            name = feed.get("name", "")
            if name in updates:
                val = int(updates[name])
                if val >= 1:
                    feed["max_articles"] = val
                    changed = True

    if changed:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CONFIG_PATH)
        logging.info("Config updated: %s", data)

    return _cors(jsonify({"status": "ok", "max_articles_per_topic": _CONFIG["max_articles_per_topic"]}))


@app.route("/api/run", methods=["POST"])
def api_run():
    global _running
    if not _run_lock.acquire(blocking=False):
        resp = jsonify({"status": "busy", "message": "A digest fetch is already in progress."})
        resp.status_code = 409
        return _cors(resp)

    _running = True

    def _worker():
        global _running
        try:
            build_digest(_CONFIG)
        except Exception as e:
            logging.error("Background digest failed: %s", e)
        finally:
            _running = False
            _run_lock.release()

    threading.Thread(target=_worker, daemon=True).start()
    resp = jsonify({"status": "started", "message": "Digest fetch started in background."})
    return _cors(resp)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="NewsDigest web server")
    ap.add_argument("--port", "-p", type=int, default=5050, help="Port (default: 5050)")
    ap.add_argument("--host",        default="0.0.0.0",    help="Host (default: 0.0.0.0)")
    args = ap.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.info("Starting NewsDigest on http://localhost:%d", args.port)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
