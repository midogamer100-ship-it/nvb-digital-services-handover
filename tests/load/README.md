# Load tests (finite, bounded, guarded)

`statement_load.py` submits a capped number of statement requests through the gateway with capped
concurrency and reports the latency of a core call before and during the load, plus how many
requests finished. It **refuses non-loopback targets** unless `--i-am-authorized-for <host>`
repeats the exact host, has hard caps (500 requests, concurrency 8, 10 minutes) and uses only
synthetic demo logins from a passwords file.

Running it against the shared cluster is a reviewed activity (runbook step 16): agree the window
and the numbers with infra first, watch the core VMs, and record results in `docs/TESTING.md`.
No cluster load test has been run by the authors.
