"""Sample the in-cluster Weather Service with independent HTTP requests."""

import concurrent.futures
import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

URL = "http://weather-agent.weather.svc.cluster.local:8080/ask?city=hangzhou&intent=trend"
INTERVAL = 1.0
TIMEOUT = 2.0


def request(seq, scheduled):
    started = time.time()
    reason = None
    try:
        with build_opener(ProxyHandler({})).open(URL, timeout=TIMEOUT) as response:
            body = json.load(response)
            if not (response.status == 200 and body.get("source") == "synthetic_demo"
                    and body.get("trend") == "up"
                    and [item["id"] for item in body["evidence"]] == ["demo-hz-01", "demo-hz-02"]):
                reason = "unexpected_response"
    except HTTPError as exc:
        reason = f"http_{exc.code}"
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        reason = type(exc).__name__
    return {"seq": seq, "scheduled": scheduled, "started": started, "ended": time.time(),
            "ok": reason is None, "reason": reason}


def main():
    start, start_wall = time.monotonic(), time.time()
    pending = set()
    lock = threading.Lock()

    def emit(sample):
        with lock:
            print(json.dumps(sample), flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for seq in range(1800):
            time.sleep(max(0, start + seq * INTERVAL - time.monotonic()))
            pending = {future for future in pending if not future.done()}
            scheduled = start_wall + seq * INTERVAL
            if len(pending) >= 8:
                emit({"seq": seq, "scheduled": scheduled, "started": time.time(),
                      "ended": time.time(), "ok": False, "reason": "probe_overrun"})
                continue
            future = pool.submit(request, seq, scheduled)
            future.add_done_callback(lambda result: emit(result.result()))
            pending.add(future)


if __name__ == "__main__":
    main()
