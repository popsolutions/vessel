# GUI authentication

The Vessel web UI uses a **session-based login** keyed off real HMM
credentials. There is no separate gate password to manage: every
person who can sign in to the chassis can sign in to the GUI, and
each chassis can use its own user/password.

Sessions live in process memory only (no disk, no database) and
expire after 8 hours.

![Vessel login screen](../screenshots/login.png)

## How it works

1. The operator opens `http://127.0.0.1:8765/`.
2. Every protected route redirects to `/login` (HTML pages) or returns
   `401` (`/api/*` endpoints). `/healthz`, `/readyz`, and the login
   flow itself are public.
3. The login form asks for:
   - **Target chassis** — picked from the dropdown (see
     [Multi-chassis target switcher](multi-chassis.md))
   - **Username** and **password** for that chassis's HMM
4. Vessel calls `HMMWebClient.login()` against the chosen target.
    - On success, a random opaque session id is stored server-side
      and set as the `vessel_sid` cookie (httpOnly, samesite=lax,
      8-hour TTL).
    - On failure, the form is re-rendered with the HMM's actual error
      (`Incorrect user name or password`, `account locked`, etc.).
5. Subsequent requests look up the session by cookie and inject
   `host` / `user` / `password` into every chassis call.

## Switching chassis mid-session

The pill in the top-left header (e.g. `▾ 192.168.1.30`) is a link
back to `/login?next=<current path>`. Picking a different target
re-prompts for credentials and returns you to the page you were on.

## Logging out

The **logout** button in the top-right header POSTs to `/logout`,
which destroys the server-side session and clears the cookie.

## Audit log

The authenticated username flows into the [audit log](audit-log.md)
as the `actor` field. When a route is reached without a session
(only the public ones permit that), the actor is recorded as
`anonymous`.

## Security posture

- The GUI binds to `127.0.0.1` by default — same machine only.
- Session ids are 32-byte random tokens (`secrets.token_urlsafe(32)`).
- Cookie is httpOnly + samesite=lax.
- Plaintext passwords live only in process memory for the session's
  lifetime; they're never written to disk.
- Login attempts that fail against the HMM are logged at WARNING. The
  HMM itself locks an account after a few bad attempts for 5 minutes
  — that lockout is upstream and unchanged.
- WebSocket route `/api/blade/{slot}/kvm/ws` reads the session cookie
  off the upgrade request, so KVM is gated by the same login.

## Limitations

- **No multi-user / RBAC.** Anyone with valid HMM credentials gets
  full chassis control. If you need finer-grained permissions, gate
  the GUI behind a reverse proxy that enforces SSO and let the HMM
  remain single-credential downstream.
- **In-memory sessions don't survive a restart.** That's intentional
  for a single-operator local tool. If you deploy multi-instance,
  swap `hmm_client.gui.session` for a backing store (Redis, JWT) —
  the surface is small.
- **HTTPS termination is the operator's responsibility.** Deploy
  behind a reverse proxy with a real certificate before exposing the
  GUI beyond `127.0.0.1`.

## Environment variables

| Env var | Default | Purpose |
|---|---|---|
| `VESSEL_TARGETS` | `192.168.1.30,192.168.1.20` | Comma-separated chassis hosts shown in the dropdown |
| `VESSEL_TARGETS_FILE` | `~/.config/vessel/targets.json` | Where the GUI persists chassis added via "+ add new chassis" |

See [Multi-chassis target switcher](multi-chassis.md) for the full
target-list mechanics.

## Legacy: bcrypt + HTTP Basic

Earlier Vessel releases gated the GUI with a bcrypt-hashed password
stored in `VESSEL_GUI_PASSWORD_HASH` and `VESSEL_GUI_USER`. That
module (`hmm_client.auth`) still ships for the CLI helper:

```bash
hmm gui-password-hash
```

It is no longer wired into the FastAPI app; the session login above
replaces it. The env vars are ignored by the GUI runtime and can be
removed from your `.env`.
