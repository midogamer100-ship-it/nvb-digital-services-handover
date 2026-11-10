"""Finite, bounded load test: N customers request statements through the gateway while the script
measures the latency of the core's own login/"me" API before and during the load.

SAFE BY DEFAULT: it refuses any target that is not loopback unless ``--i-am-authorized-for``
repeats the exact target host, it has hard caps (requests, concurrency, duration), it only uses
synthetic demo logins supplied in a passwords file, and it never retries in a loop.

    python scripts/local_stack.py start && python scripts/local_run.py up
    python tests/load/statement_load.py --base-url http://127.0.0.1:18080 \
        --passwords-file .local/run/demo-passwords.json --users customer001,customer002 --requests 20 --concurrency 4
    python scripts/local_run.py worker statements          # in another shell: bounded worker runs

Results are printed as JSON; record them in docs/TESTING.md. Numbers from a laptop are not
capacity figures for the single-node cluster.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import httpx

MAX_REQUESTS, MAX_CONCURRENCY, MAX_SECONDS = 500, 8, 600


def probe(client: httpx.Client, n: int = 20) -> dict:
    """Latency of a core call (GET /api/auth/me) through the gateway."""
    times = []
    for _ in range(n):
        t = time.perf_counter()
        client.get("/api/auth/me").raise_for_status()
        times.append((time.perf_counter() - t) * 1000)
    times.sort()
    return {"p50_ms": round(statistics.median(times), 1), "p95_ms": round(times[int(len(times) * 0.95) - 1], 1),
            "max_ms": round(times[-1], 1)}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", required=True)
    p.add_argument("--passwords-file", required=True, help="JSON {customer: password} of SYNTHETIC demo logins")
    p.add_argument("--users", default="customer001")
    p.add_argument("--requests", type=int, default=20)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--wait-seconds", type=int, default=120, help="how long to wait for completion (bounded)")
    p.add_argument("--i-am-authorized-for", default="", help="exact host of a non-loopback target you may load")
    args = p.parse_args()

    host = urlsplit(args.base_url).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1") and args.i_am_authorized_for != host:
        raise SystemExit(f"Refused: {host} is not loopback. Shared targets need an explicit, reviewed "
                         f"--i-am-authorized-for {host}.")
    if not (1 <= args.requests <= MAX_REQUESTS and 1 <= args.concurrency <= MAX_CONCURRENCY
            and 1 <= args.wait_seconds <= MAX_SECONDS):
        raise SystemExit(f"Refused: caps are {MAX_REQUESTS} requests, concurrency {MAX_CONCURRENCY}, {MAX_SECONDS} s.")
    password = json.load(open(args.passwords_file, encoding="utf-8"))["customer"]

    sessions = []
    for user in args.users.split(","):
        c = httpx.Client(base_url=args.base_url, timeout=30)
        r = c.post("/api/auth/login", json={"username": user, "password": password})
        r.raise_for_status()
        c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        accounts = c.get("/x/statements/v1/accounts").json()["items"]
        sessions.append((c, [a["account_id"] for a in accounts]))
    before = probe(sessions[0][0])

    months = [f"2026-{m:02d}" for m in range(1, 11)]
    jobs = []
    for i in range(args.requests):
        c, accts = sessions[i % len(sessions)]
        jobs.append((c, accts[i % len(accts)], months[(i // max(1, len(accts))) % len(months)]))

    def submit(job):
        c, account_id, period = job
        t = time.perf_counter()
        r = c.post("/x/statements/v1/requests", json={"account_id": account_id, "period": period},
                   headers={"Idempotency-Key": uuid.uuid4().hex})
        return r.status_code, (time.perf_counter() - t) * 1000, r.json().get("id") if r.status_code == 202 else None

    started = time.monotonic()
    with ThreadPoolExecutor(args.concurrency) as pool:
        results = list(pool.map(submit, jobs))
    during = probe(sessions[0][0])
    ids = {(jobs[i][0], rid) for i, (_s, _ms, rid) in enumerate(results) if rid}
    done = {}
    while time.monotonic() - started < args.wait_seconds and len(done) < len(ids):
        for c, rid in ids:
            if rid not in done:
                status = c.get(f"/x/statements/v1/requests/{rid}").json()["status"]
                if status in ("completed", "failed", "cancelled"):
                    done[rid] = status
        time.sleep(1)
    lat = sorted(ms for _s, ms, _id in results)
    print(json.dumps({
        "target": args.base_url, "requests": args.requests, "concurrency": args.concurrency,
        "accepted": sum(1 for s, _m, _i in results if s == 202),
        "rejected": {str(s): sum(1 for x, _m, _i in results if x == s) for s in {s for s, _m, _i in results} - {202}},
        "submit_p50_ms": round(statistics.median(lat), 1), "submit_max_ms": round(lat[-1], 1),
        "core_me_before": before, "core_me_during": during,
        "finished": {s: list(done.values()).count(s) for s in set(done.values())},
        "unfinished_after_wait": len(ids) - len(done), "elapsed_s": round(time.monotonic() - started, 1)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
