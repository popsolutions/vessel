# Multi-chassis target switcher

Vessel can drive any number of E9000 chassis from a single GUI
instance. The login screen carries a target dropdown; switching
chassis mid-session is a single click on the host pill in the
header.

![Top-left target pill + login dropdown](../screenshots/target-switcher.png)

## Where the target list comes from

The list shown in the `/login` dropdown is a **merge of three
sources** (in this order, deduplicated):

1. **`VESSEL_TARGETS` env var** — comma-separated, set in `.env`.
2. **Persisted file** at `~/.config/vessel/targets.json` — grown
   automatically when an operator logs in successfully to a host
   typed into the "+ add new chassis…" field.
3. **Built-in defaults** (`192.168.1.30`, `192.168.1.20`) — only
   used when both of the above are empty, so the dropdown is never
   blank.

## Adding a new chassis from the GUI

![Custom target input revealed under the dropdown](../screenshots/add-new-chassis.png)

1. Open `/login` (you'll land here automatically when there's no
   active session).
2. In the **Target chassis** dropdown, pick **`+ add new chassis…`**
   at the bottom of the list. A text input appears.
3. Type the IP or hostname of the new chassis (e.g. `192.168.1.40`
   or `chassis-lab-3.example.internal`).
4. Fill in **Username** and **Password** for that chassis's HMM.
5. Click **Sign in**.

If the HMM accepts the credentials:

- A session is established for that chassis.
- The host is appended to `~/.config/vessel/targets.json`.
- The next time you open `/login`, the host is in the dropdown
  without any restart.

If the login fails, **nothing is persisted**. The form re-renders
with the HMM's actual error (`Incorrect user name or password`,
`account locked`, etc.) and the typed host is not added to the
file.

## Adding a chassis from `.env`

For permanent, pre-seeded entries that you want to share across
machines or commit into config management, edit the env var:

```env
VESSEL_TARGETS=192.168.1.30,192.168.1.20,192.168.1.40
```

The first entry is the **pre-selected default** in the dropdown.
Restart the GUI for the new value to take effect.

The env list takes precedence over the persisted file for ordering
and deduplication — hosts already in `VESSEL_TARGETS` are never
duplicated into the JSON file.

## Removing a chassis

There's no UI for removal yet. Edit the JSON file directly:

```bash
$EDITOR ~/.config/vessel/targets.json
```

The shape is plain:

```json
{
  "targets": [
    "192.168.1.30",
    "192.168.1.20",
    "192.168.1.40"
  ]
}
```

Delete the line you no longer want and save. No restart required —
the file is re-read on every `/login` GET.

To remove an entry that came in via `VESSEL_TARGETS`, edit `.env`
instead and restart.

## Switching mid-session

The pill in the top-left of every page (`▾ 192.168.1.30`) is a link
to `/login?next=<current path>`. Picking a different target
re-prompts for credentials and returns you to the page you were
on. Sessions are per-chassis: you must re-authenticate when you
switch.

## Credentials per chassis

Each chassis can have its own HMM credentials. Nothing about the
chassis credentials lives in `.env` once you're using the GUI —
they're typed at the login form and held only in the in-memory
session.

The legacy `HMM_USER` / `HMM_PASSWORD` env vars are still honoured
by:

- The Typer **CLI** (`hmm list`, `hmm power`, `hmm snapshot`) —
  these read straight from `.env` since the CLI has no session.
- The GUI login form's **pre-filled** Username field, as a default
  for the first chassis you sign in to.

## Validation

Operator-typed hosts are validated for shape before any HMM round
trip:

- IPv4, IPv6 (`ipaddress.ip_address`)
- DNS hostnames per RFC 1123 (labels A-Z 0-9 `-`, max 63 chars each,
  total ≤253 chars)

Strings like `https://evil.example.com/admin` or `"; rm -rf /"` are
rejected with `target ... is not a valid IP or hostname` before
they ever reach the HTTP client.

## Threat model

The persisted file holds **hostnames only** — no credentials. Stealing
it gives you nothing you couldn't get from a network scan. Sessions
hold credentials in process memory; they don't touch disk and
disappear on restart.

If you need to harden further:

- Set `VESSEL_TARGETS_FILE=/etc/vessel/targets.json` and run Vessel
  as a service account that owns that path.
- Run behind a reverse proxy with mTLS or SSO.
- Don't bind the GUI to `0.0.0.0`; keep it on `127.0.0.1` and tunnel
  in.
