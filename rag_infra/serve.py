"""Deterministic HTTP backend for exercising the evaluation platform."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rag_infra.core import fixture_reply


def send(handler, status, payload):
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def answer_post(handler, profile):
    if handler.path != "/rag/answer":
        return send(handler, 404, {"error": "not found"})
    try:
        size = int(handler.headers["Content-Length"])
        if not 1 <= size <= 64 * 1024:
            raise ValueError("invalid request length")
        reply = fixture_reply(json.loads(handler.rfile.read(size)), profile)
    except (KeyError, TypeError, ValueError, AttributeError):
        return send(handler, 400, {"error": "expected case_id, question and documents"})
    return send(handler, 200, reply)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/readyz":
            return send(self, 200, {"status": "ok"})
        return send(self, 404, {"error": "not found"})

    def do_POST(self):
        return answer_post(self, self.server.rag_profile)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--rag-profile", choices=["guarded", "regressed"], default="guarded")
    args = parser.parse_args()
    with ThreadingHTTPServer((args.host, args.port), Handler) as server:
        server.rag_profile = args.rag_profile
        server.serve_forever()


if __name__ == "__main__":
    main()
