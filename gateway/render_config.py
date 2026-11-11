"""Render gateway/nginx.conf.template from GATEWAY_* environment variables (stdlib only).

Used by the container entrypoint and by the local test stack, so both run the same routing rules.
Fails closed:

* every required variable must be set (no silent defaults for upstreams or the public origin);
* an ``https://`` upstream REQUIRES a CA bundle and gets certificate + hostname verification
  (``proxy_ssl_verify on``, ``proxy_ssl_name``, SNI). There is no option to turn verification off;
* a plain ``http://`` upstream is refused unless ``GATEWAY_ALLOW_INSECURE_UPSTREAMS=true``
  (laptop / disposable tests only).

    python render_config.py --output /tmp/nginx/nginx.conf
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

TEMPLATE = Path(__file__).with_name("nginx.conf.template")


class ConfigError(SystemExit):
    pass


def _need(env: dict, name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"gateway configuration error: {name} is not set")
    return value


def _path(p: str) -> str:
    return p.replace("\\", "/")


def upstream(env: dict, url_var: str, ca_var: str, name_var: str | None, indent: str) -> tuple[str, str]:
    """Return (proxy_pass target, TLS directives) for one upstream."""
    url = _need(env, url_var).rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.path not in ("", "/") \
            or parts.username or parts.query:
        raise ConfigError(f"gateway configuration error: {url_var} must be scheme://host[:port] with no path")
    if parts.scheme == "http":
        if env.get("GATEWAY_ALLOW_INSECURE_UPSTREAMS", "").lower() != "true":
            raise ConfigError(f"gateway configuration error: {url_var} uses http:// without "
                              "GATEWAY_ALLOW_INSECURE_UPSTREAMS=true")
        return url, ""
    ca = _need(env, ca_var)
    if not Path(ca).is_file():
        raise ConfigError(f"gateway configuration error: {ca_var} does not point at a readable CA bundle")
    tls_name = (env.get(name_var, "").strip() if name_var else "") or parts.hostname
    if not re.fullmatch(r"[A-Za-z0-9.-]+", tls_name):
        raise ConfigError(f"gateway configuration error: invalid TLS name for {url_var}")
    directives = "\n".join(indent + line for line in (
        "proxy_ssl_verify on;",
        "proxy_ssl_verify_depth 3;",
        f"proxy_ssl_trusted_certificate {_path(ca)};",
        f"proxy_ssl_name {tls_name};",
        "proxy_ssl_server_name on;",
        "proxy_ssl_protocols TLSv1.2 TLSv1.3;",
        "proxy_ssl_session_reuse on;",
    ))
    return url, directives


def render(env: dict) -> str:
    origin = _need(env, "GATEWAY_PUBLIC_ORIGIN").rstrip("/")
    o = urlsplit(origin)
    if o.scheme not in ("http", "https") or not o.hostname or o.path:
        raise ConfigError("gateway configuration error: GATEWAY_PUBLIC_ORIGIN must be scheme://host[:port]")
    if o.scheme == "http" and env.get("GATEWAY_ALLOW_INSECURE_UPSTREAMS", "").lower() != "true":
        raise ConfigError("gateway configuration error: GATEWAY_PUBLIC_ORIGIN must be https://")
    indent = " " * 12
    core, core_tls = upstream(env, "GATEWAY_CORE_UPSTREAM", "GATEWAY_CORE_CA_FILE", "GATEWAY_CORE_TLS_NAME", indent)
    stm, stm_tls = upstream(env, "GATEWAY_STATEMENTS_UPSTREAM", "GATEWAY_INTERNAL_CA_FILE", None, indent)
    loans, loans_tls = upstream(env, "GATEWAY_LOANS_UPSTREAM", "GATEWAY_INTERNAL_CA_FILE", None, indent)
    if bool(stm_tls) != bool(loans_tls):
        raise ConfigError("gateway configuration error: statements and loans upstreams must both be https or both http")

    cert, key = env.get("GATEWAY_TLS_CERT_FILE", "").strip(), env.get("GATEWAY_TLS_KEY_FILE", "").strip()
    listen = _need(env, "GATEWAY_LISTEN")
    tls_server = ""
    if cert or key:
        if not (Path(cert).is_file() and Path(key).is_file()):
            raise ConfigError("gateway configuration error: GATEWAY_TLS_CERT_FILE / GATEWAY_TLS_KEY_FILE not readable")
        listen += " ssl"
        tls_server = "\n".join(" " * 8 + line for line in (
            f"ssl_certificate {_path(cert)};", f"ssl_certificate_key {_path(key)};",
            "ssl_protocols TLSv1.2 TLSv1.3;", "ssl_session_cache shared:gw:5m;"))
    elif env.get("GATEWAY_ALLOW_INSECURE_UPSTREAMS", "").lower() != "true":
        raise ConfigError("gateway configuration error: GATEWAY_TLS_CERT_FILE / GATEWAY_TLS_KEY_FILE are required")

    run_dir = _path(_need(env, "GATEWAY_RUN_DIR"))
    values = {
        "WORKER_PROCESSES": env.get("GATEWAY_WORKER_PROCESSES", "2"),
        "RUN_DIR": run_dir,
        "ERROR_LOG": _path(env.get("GATEWAY_ERROR_LOG", "/dev/stderr")),
        "ACCESS_LOG": _path(env.get("GATEWAY_ACCESS_LOG", "/dev/stdout")),
        "MIME_TYPES": _path(env.get("GATEWAY_MIME_TYPES", "/etc/nginx/mime.types")),
        "LOGIN_RATE": env.get("GATEWAY_LOGIN_RATE", "30r/m"),
        "LOGIN_BURST": env.get("GATEWAY_LOGIN_BURST", "20"),
        "PUBLIC_ORIGIN": origin,
        "LISTEN": listen,
        "TLS_SERVER_DIRECTIVES": tls_server,
        "CORE_UPSTREAM": core, "CORE_TLS_DIRECTIVES": core_tls,
        "STATEMENTS_UPSTREAM": stm, "LOANS_UPSTREAM": loans, "INTERNAL_TLS_DIRECTIVES": stm_tls,
        "PORTAL_ROOT": _path(_need(env, "GATEWAY_PORTAL_ROOT")),
    }
    for name in ("WORKER_PROCESSES", "LOGIN_RATE", "LOGIN_BURST"):
        if not re.fullmatch(r"[0-9a-z/]+", values[name]):
            raise ConfigError(f"gateway configuration error: invalid GATEWAY_{name}")
    text = TEMPLATE.read_text(encoding="utf-8")
    for key_, value in values.items():
        text = text.replace(f"@@{key_}@@", value)
    left = re.findall(r"@@[A-Z_]+@@", "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#")))
    if left:
        raise ConfigError(f"gateway configuration error: unresolved placeholders {sorted(set(left))}")
    return text


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True)
    args = p.parse_args()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(render(dict(os.environ)).encode("utf-8"))
    for sub in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"):
        (Path(os.environ["GATEWAY_RUN_DIR"]) / sub).mkdir(parents=True, exist_ok=True)
    print(f"gateway configuration written to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
