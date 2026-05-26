# Setup scripts

One-shot installers for running the AI Mesh **server** persistently.

## Linux (Debian/Ubuntu, systemd)

Creates a `meshd` system user, installs Python deps in a venv, optionally
issues a Let's Encrypt cert, writes `/etc/systemd/system/ai-mesh.service`,
and starts it.

```bash
# Interactive — prompts for TLS mode + domain
curl -fsSL https://raw.githubusercontent.com/Superfish1000/AI-Mesh/main/scripts/setup-linux.sh -o setup-linux.sh
bash setup-linux.sh

# Non-interactive
DOMAIN=mesh.example.com EMAIL=admin@example.com TLS_MODE=letsencrypt \
  bash setup-linux.sh
```

Env vars:

| Var | Default | Notes |
|---|---|---|
| `INSTALL_DIR`  | `/opt/ai-mesh`     | Root for repo + venv |
| `SERVICE_USER` | `meshd`            | System user the daemon runs as |
| `PORT`         | `443`              | Bound via `CAP_NET_BIND_SERVICE` |
| `TLS_MODE`     | *(prompt)*         | `letsencrypt` / `self-signed` / `provided` / `none` |
| `DOMAIN`       | *(prompt)*         | Required for `letsencrypt` |
| `EMAIL`        | *(prompt)*         | Required for `letsencrypt` |
| `CERT_FILE`    | *(prompt)*         | Required for `provided` |
| `KEY_FILE`     | *(prompt)*         | Required for `provided` |
| `REPO_URL`     | this repo          | Override to install from a fork |

Requires: root (or `sudo`), `apt-get`, systemd as PID 1.

After install, the one-time setup URL appears in:

```bash
journalctl -u ai-mesh -n 30
```

## Windows (Server / 10 / 11)

Installs deps in a venv, generates a self-signed cert, opens the firewall,
and registers a Scheduled Task that runs the server as `SYSTEM` at boot.

```powershell
# Run elevated (Administrator)
Set-ExecutionPolicy -Scope Process Bypass
.\setup-windows.ps1
```

Parameters:

| Param | Default | Notes |
|---|---|---|
| `-InstallDir` | `C:\ai-mesh`            | Root for repo + venv |
| `-Port`       | `8443`                  | TCP port (no privileged-port quirks on Windows) |
| `-TlsMode`    | `self-signed`           | `self-signed` / `provided` / `none` |
| `-CertFile`   | *(empty)*               | Required when `-TlsMode provided` |
| `-KeyFile`    | *(empty)*               | Required when `-TlsMode provided` |
| `-TaskName`   | `AI Mesh Server`        | Scheduled task display name |

Requires: Administrator, Python 3.11+, git for Windows.

Because the Scheduled Task runs headless as SYSTEM, the first-run setup
token isn't visible on a console. The script prints two ways to retrieve
it — easiest is to stop the task, run uvicorn in the foreground once to
grab the URL, then re-start the task.
