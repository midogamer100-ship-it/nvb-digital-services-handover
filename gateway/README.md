# gateway

nginx same-origin gateway for the portal, the core session API and the extension APIs.

* `nginx.conf.template` — routing allowlist and header rules (read the comment block at the top).
* `render_config.py` — renders the template from `GATEWAY_*` variables; fails closed (https
  upstreams require a CA file and get certificate + hostname verification; there is no switch to
  disable verification).
* `entrypoint.sh` / `Containerfile` — image (build context = repository root).

Design and limitations: `docs/DESIGN.md` §2. Variables: `docs/CONFIGURATION.md`. Tests with real
nginx: `tests/gateway`.
