"""Browser identity for extension APIs.

The browser's core session cookie (``nvb_session``) reaches an extension API through the
same-origin gateway. It is verified against the core on EVERY request via the Core Adapter
(introspection): no identity/role header is ever trusted, and the extension never reads the
core's session table. State-changing requests additionally need ``X-CSRF-Token``, which the
core validates for that session.
"""

from __future__ import annotations

import logging

from nvb_platform.adapter_client import AdapterCallError, Identity

log = logging.getLogger("nvb.webauth")
SESSION_COOKIE = "nvb_session"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class AuthError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


def resolve_identity(adapter, request, *, request_id: str | None, include_accounts: bool = True) -> Identity:
    """Return the verified caller or raise AuthError (401/403/503). Mutation = any unsafe method."""
    mutation = request.method.upper() not in SAFE_METHODS
    session = request.cookies.get(SESSION_COOKIE)
    if not session or len(session) > 128:
        raise AuthError(401, "authentication_required", "Authentication required.")
    csrf = request.headers.get(CSRF_HEADER) if mutation else None
    if mutation and (not csrf or len(csrf) > 128):
        raise AuthError(403, "csrf_invalid", "Missing or invalid CSRF token.")
    try:
        ident = adapter.resolve(session, csrf, request_id, include_accounts)
    except AdapterCallError as exc:
        log.warning("identity check failed", extra={"code": exc.code})
        raise AuthError(503, "identity_unavailable", "Identity service unavailable; retry later.") from None
    if ident is None:
        raise AuthError(401, "authentication_required", "Authentication required.")
    if mutation and ident.csrf_valid is not True:
        raise AuthError(403, "csrf_invalid", "Missing or invalid CSRF token.")
    return ident
