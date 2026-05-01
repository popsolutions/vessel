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
HMM_HOST=192.168.1.30
HMM_USER=root
HMM_PASSWORD=changeme

# Optional but recommended
VESSEL_AUDIT_LOG=/var/log/vessel/audit.log
VESSEL_GUI_PASSWORD_HASH=$2b$12$...    # see GUI auth
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

For production deployment, set `VESSEL_GUI_PASSWORD_HASH` first
(see [GUI auth](operations/gui-auth.md)) — the GUI logs a loud
warning at startup when auth is disabled.

## Telegram notifications (optional)

Long-running ops can ping the operator on their phone:

```bash
export VESSEL_TG_BOT_TOKEN=...    # from @BotFather
export VESSEL_TG_CHAT_ID=...

hmm notify "test"
```
