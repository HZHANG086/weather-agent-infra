"""Small, evidence-grounded weather service using synthetic graph data."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# Each city links to observations, which in turn link to a demo rule.
CITIES = {
    "hangzhou": ("demo-hz-01", "demo-hz-02"),
    "beijing": ("demo-bj-01", "demo-bj-02"),
}
OBSERVATIONS = {
    "demo-hz-01": {"at": "2026-01-01T08:00:00Z", "rain_mm": 2},
    "demo-hz-02": {"at": "2026-01-01T09:00:00Z", "rain_mm": 12},
    "demo-bj-01": {"at": "2026-01-01T08:00:00Z", "rain_mm": 3},
    "demo-bj-02": {"at": "2026-01-01T09:00:00Z", "rain_mm": 0},
}
ALERT_RULE = {"id": "demo-rain-10", "threshold_mm": 10}


def answer(city: str, intent: str) -> dict:
    keys = CITIES.get(city.strip().lower(), ())
    ids = keys[-1:] if intent == "alert" else keys[-2:]
    base = {"city": city, "intent": intent, "source": "synthetic_demo"}
    if intent not in {"alert", "trend"} or not ids or (intent == "trend" and len(ids) < 2):
        return {**base, "status": "insufficient_evidence", "evidence": []}

    records = [{"id": key, **OBSERVATIONS[key]} for key in ids]
    last = records[-1]["rain_mm"]
    if intent == "alert":
        result = {
            "demo_alert": last >= ALERT_RULE["threshold_mm"],
            "rain_mm": last,
            "rule": ALERT_RULE,
        }
    else:
        delta = last - records[0]["rain_mm"]
        result = {"rain_change_mm": delta, "trend": "up" if delta > 0 else "down" if delta < 0 else "flat"}
    return {**base, "status": "ok", "evidence": records, **result}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urlsplit(self.path)
        status = 200
        if url.path in {"/healthz", "/readyz"}:
            payload = {"status": "ok"}
        elif url.path == "/ask":
            query = parse_qs(url.query)
            city = query.get("city", [""])[0]
            intent = query.get("intent", [""])[0]
            if not city or intent not in {"trend", "alert"}:
                status, payload = 400, {"error": "provide city and intent=trend|alert"}
            else:
                payload = answer(city, intent)
        else:
            status, payload = 404, {"error": "not found"}
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    with ThreadingHTTPServer((args.host, args.port), Handler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
