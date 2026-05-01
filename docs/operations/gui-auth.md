# GUI authentication

The GUI binds to `127.0.0.1` by default and historically had **zero
auth** — anyone with localhost access could power-cycle blades. This
is fine for single-operator dev mode but unsafe for any multi-tenant
or networked deploy.

Auth is opt-in via environment variables. When unset, the GUI logs a
loud warning at startup but accepts all requests anonymously.

## Enable

1. Generate a bcrypt password hash:

    ```bash
    hmm gui-password-hash
    # password: ********
    # password (again): ********
    # Add to your .env (and never commit):
    # VESSEL_GUI_PASSWORD_HASH=$2b$12$...
    ```

2. Add the line to your `.env` (or secret manager).

3. Restart the GUI. The startup banner will no longer warn.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `VESSEL_GUI_USER` | `admin` | Username for Basic Auth |
| `VESSEL_GUI_PASSWORD_HASH` | _(unset)_ | bcrypt hash; auth disabled when unset |

## How it works

- HTTP Basic Auth on every route
- Username compared with `secrets.compare_digest` (constant-time)
- Password verified with `bcrypt.checkpw` (constant-time)
- Both checks always run regardless of which fails first
  (constant-time defence against username enumeration)
- Health endpoints (`/healthz`, `/readyz`) bypass auth so probes work
- Authenticated username flows into the [audit log](audit-log.md) as
  the `actor` field (instead of `<user>@<host>`)

## Why bcrypt + Basic instead of OIDC

For single-operator chassis management this is enough. Browsers handle
Basic natively (no cookie/session/CSRF surface), bcrypt is
well-understood, rotating credentials = update env + restart.

When you need RBAC / multi-user / SSO, swap `hmm_client.auth` for an
OIDC flow without touching routes — the FastAPI dependency surface
stays the same (`require_auth` returns the username).

## Limitations

- Currently single-user (one credential pair per deploy)
- WebSocket route `/api/blade/{slot}/kvm/ws` doesn't enforce Basic yet
  — Basic doesn't fit cleanly on WS handshakes; needs token-bearing
  query parameter (tracked in #22 follow-up)
- HTTPS termination is the operator's responsibility; deploy behind a
  reverse proxy with a real certificate before exposing beyond
  127.0.0.1
