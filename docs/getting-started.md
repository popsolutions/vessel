# Getting started

## Install

=== "From source (recommended for now)"

    ```bash
    git clone https://github.com/popsolutions/vessel
    cd vessel
    uv sync
    ```

    `hmm` is now on your PATH inside the venv.

=== "Container"

    ```bash
    docker run --rm -p 8765:8765 \
      -e HMM_HOST=192.168.1.30 \
      -e HMM_USER=root \
      -e HMM_PASSWORD=... \
      ghcr.io/popsolutions/vessel:latest hmm gui
    ```

## Configure chassis

Create `.env` in the project root:

```bash
# CLI defaults + login-form pre-fill (each chassis can still use its own
# user/password through the GUI login form).
HMM_HOST=192.168.1.30
HMM_USER=root
HMM_PASSWORD=changeme

# GUI target dropdown — one or more chassis hosts, comma-separated.
# New chassis can also be added live from the /login form.
VESSEL_TARGETS=192.168.1.30,192.168.1.20

# Optional but recommended
VESSEL_AUDIT_LOG=/var/log/vessel/audit.log
```

`.env` is gitignored.

## First commands

```bash
# Read-only inventory
hmm list

# Snapshot everything (safe to run any time)
hmm snapshot

# Power blade 3 off (with confirm)
hmm power 3 off
```

## Run the GUI

```bash
hmm gui
# → http://127.0.0.1:8765
```

First request lands on `/login`. Pick a target from the dropdown
and authenticate with the HMM credentials for that chassis — see
[GUI auth](operations/gui-auth.md). To drive several chassis from
the same install, see
[Multi-chassis target switcher](operations/multi-chassis.md).

## Telegram notifications (optional)

Long-running ops can ping the operator on their phone:

```bash
export VESSEL_TG_BOT_TOKEN=...    # from @BotFather
export VESSEL_TG_CHAT_ID=...

hmm notify "test"
```
