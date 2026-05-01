"""HTTP Basic Auth for the FastAPI GUI.

Closes the gap from `SECURITY.md` "Hardening Checklist": today, anyone
with localhost access to the GUI can power-cycle blades and mount
ISOs. With auth enabled, browsers prompt once for credentials and the
authenticated username flows into the audit log.

## Configuration

Auth is **opt-in** to preserve the dev-mode workflow (zero-config GUI
on `http://127.0.0.1:8765`). Enable in production by setting:

    VESSEL_GUI_USER             Username (default: "admin").
    VESSEL_GUI_PASSWORD_HASH    bcrypt hash, e.g. "$2b$12$..."

Generate a hash with:

    hmm gui-password-hash

(or any other bcrypt tool — the format is standard).

When `VESSEL_GUI_PASSWORD_HASH` is unset/empty, `is_enabled()` returns
False and the GUI accepts any request unauthenticated. The startup
banner logs a clear warning so operators don't ship to production
without enabling.

## Why bcrypt + Basic instead of OIDC

- Single operator chassis-management is the common case; no need for
  multi-tenant identity provider plumbing.
- Browsers handle Basic natively, no cookie/session/CSRF surface.
- bcrypt is well-understood; rotating credentials = update env + restart.

When you need RBAC / multi-user / SSO, swap this module for an OIDC
flow without touching the routes — the FastAPI dependency surface
stays the same (`require_auth` returns the username).
"""

from __future__ import annotations

import logging
import os
import secrets

import bcrypt
from fastapi import HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

log = logging.getLogger(__name__)

_security = HTTPBasic(auto_error=False)


def _gui_user() -> str:
    return os.environ.get("VESSEL_GUI_USER", "admin").strip() or "admin"


def _gui_hash() -> bytes | None:
    h = os.environ.get("VESSEL_GUI_PASSWORD_HASH", "").strip()
    return h.encode("utf-8") if h else None


def is_enabled() -> bool:
    """True when VESSEL_GUI_PASSWORD_HASH parses as a valid bcrypt hash."""
    h = _gui_hash()
    if h is None:
        return False
    # bcrypt hashes start with $2a$/$2b$/$2y$. Cheap sanity check —
    # avoids handing operator's literal plaintext to bcrypt.checkpw.
    return h[:4] in (b"$2a$", b"$2b$", b"$2y$")


def hash_password(plain: str) -> str:
    """Generate a bcrypt hash suitable for VESSEL_GUI_PASSWORD_HASH."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: bytes) -> bool:
    """Constant-time bcrypt verify; never raises."""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed)
    except (ValueError, TypeError):
        return False


def require_auth(
    creds: HTTPBasicCredentials | None = None,
) -> str:
    """FastAPI dependency: return authenticated username or raise 401.

    When auth is disabled (env not set), returns "anonymous" so callers
    can still log who did what — they just can't tell different
    operators apart yet.

    The actual `Depends(_security)` wiring happens in `gui/app.py` —
    we accept the parsed credentials here as a plain arg so the unit
    tests can exercise the logic without a TestClient.
    """
    if not is_enabled():
        return "anonymous"

    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    expected_user = _gui_user()
    pwd_hash = _gui_hash()
    assert pwd_hash is not None  # is_enabled() guaranteed it

    user_ok = secrets.compare_digest(creds.username, expected_user)
    pwd_ok = verify_password(creds.password, pwd_hash)
    # Always run both checks for constant-time behaviour against
    # username-enumeration timing attacks.
    if not (user_ok and pwd_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Basic"},
        )

    return creds.username


# Re-export the FastAPI dependency factory so route definitions can
# write `Depends(security)` cleanly.
security = _security


__all__ = [
    "hash_password",
    "is_enabled",
    "require_auth",
    "security",
    "verify_password",
]
