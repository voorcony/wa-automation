#!/usr/bin/env bash
#
# deploy.sh - Deploy the WA automation system to a remote Ubuntu server.
#
# Usage:
#   ./deploy.sh
#
# Requirements (local):
#   - sshpass
#   - rsync
#   - ssh
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REMOTE_HOST="43.154.181.44"
REMOTE_USER="ubuntu"
REMOTE_PASS='ZHOUjiahao1!'
REMOTE_DIR="/home/${REMOTE_USER}/wa-automation"
LOCAL_DIR="${HOME}/wa-automation"

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=15)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m   %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[error]\033[0m  %s\n' "$*" >&2; }

die() {
    err "$*"
    exit 1
}

# Run a command on the remote host. Stdin is forwarded so we can pipe scripts.
remote_exec() {
    sshpass -p "${REMOTE_PASS}" ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${REMOTE_HOST}" "$@"
}

# Run a multi-line bash script on the remote host with `set -euo pipefail`.
remote_bash() {
    local script="$1"
    sshpass -p "${REMOTE_PASS}" ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${REMOTE_HOST}" \
        "bash -s" <<EOF
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export SUDO_PASS='${REMOTE_PASS}'
sudo() { echo "\${SUDO_PASS}" | command sudo -S -p '' "\$@"; }
${script}
EOF
}

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
preflight() {
    log "Running local pre-flight checks..."

    for bin in sshpass rsync ssh; do
        if ! command -v "${bin}" >/dev/null 2>&1; then
            die "Required local tool '${bin}' not found. Install with: sudo apt-get install -y ${bin}"
        fi
    done

    [[ -d "${LOCAL_DIR}" ]] || die "Local directory not found: ${LOCAL_DIR}"

    log "Testing SSH connectivity to ${REMOTE_USER}@${REMOTE_HOST}..."
    if ! remote_exec "echo 'ssh ok'" >/dev/null 2>&1; then
        die "Unable to SSH into ${REMOTE_USER}@${REMOTE_HOST}. Check credentials/network."
    fi
    log "SSH OK."
}

# ---------------------------------------------------------------------------
# Step 2: install system dependencies
# ---------------------------------------------------------------------------
install_system_deps() {
    log "Installing system dependencies on remote (redis, nodejs 18+, python3-pip, wget, curl)..."
    remote_bash '
        sudo apt-get update -y

        sudo apt-get install -y --no-install-recommends \
            ca-certificates curl wget gnupg lsb-release \
            redis-server python3 python3-pip python3-venv \
            rsync git build-essential

        # Ensure Node.js 18+. Install NodeSource repo if needed.
        need_node=1
        if command -v node >/dev/null 2>&1; then
            cur_major=$(node -p "process.versions.node.split(\".\")[0]" 2>/dev/null || echo 0)
            if [ "${cur_major}" -ge 18 ] 2>/dev/null; then
                need_node=0
                echo "Node.js $(node -v) already installed."
            fi
        fi
        if [ "${need_node}" -eq 1 ]; then
            echo "Installing Node.js 20.x from NodeSource..."
            curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
            sudo apt-get install -y nodejs
        fi

        sudo systemctl enable --now redis-server || sudo systemctl enable --now redis || true

        echo "--- versions ---"
        node --version
        npm --version
        python3 --version
        pip3 --version
        redis-server --version | head -n1
    '
}

# ---------------------------------------------------------------------------
# Step 3 & 4: ensure remote dir + rsync code
# ---------------------------------------------------------------------------
sync_code() {
    log "Ensuring remote directory exists: ${REMOTE_DIR}"
    remote_exec "mkdir -p '${REMOTE_DIR}'"

    log "Rsyncing local code to remote (excluding node_modules, __pycache__, .git)..."
    sshpass -p "${REMOTE_PASS}" rsync \
        -az --delete \
        -e "ssh ${SSH_OPTS[*]}" \
        --exclude='node_modules' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.git' \
        --exclude='.venv' \
        --exclude='venv' \
        "${LOCAL_DIR}/" \
        "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"
    log "Code sync complete."
}

# ---------------------------------------------------------------------------
# Steps 5-7: install per-component dependencies
# ---------------------------------------------------------------------------
install_app_deps() {
    log "Installing application dependencies on remote..."
    remote_bash "
        cd '${REMOTE_DIR}'

        if [ -f wa-worker/package.json ]; then
            echo '--- npm install (wa-worker) ---'
            cd '${REMOTE_DIR}/wa-worker'
            npm install --no-audit --no-fund
        else
            echo 'wa-worker/package.json not found, skipping npm install.'
        fi

        if [ -f '${REMOTE_DIR}/ai_engine/requirements.txt' ]; then
            echo '--- pip install (ai_engine) ---'
            cd '${REMOTE_DIR}/ai_engine'
            pip3 install --break-system-packages -r requirements.txt || \
                pip3 install -r requirements.txt
        else
            echo 'ai_engine/requirements.txt not found, skipping.'
        fi

        if [ -f '${REMOTE_DIR}/orchestrator/requirements.txt' ]; then
            echo '--- pip install (orchestrator) ---'
            cd '${REMOTE_DIR}/orchestrator'
            pip3 install --break-system-packages -r requirements.txt || \
                pip3 install -r requirements.txt
        else
            echo 'orchestrator/requirements.txt not found, skipping.'
        fi
    "
}

# ---------------------------------------------------------------------------
# Step 8: install AdsPower
# ---------------------------------------------------------------------------
install_adspower() {
    log "Installing AdsPower Global on remote..."
    remote_bash '
        if command -v adspower_global >/dev/null 2>&1 || [ -d /opt/AdsPower ] || [ -d /opt/adspower-global-service ]; then
            echo "AdsPower appears to already be installed; reinstalling to ensure latest version."
        fi

        wget -O /tmp/AdsPower.deb https://www.adspower.net/download/linux/AdsPower_Global_x86_64.deb

        # dpkg may fail due to missing deps; apt-get install -f resolves them.
        sudo dpkg -i /tmp/AdsPower.deb || true
        sudo apt-get install -f -y

        # Re-run dpkg in case -f only fixed deps without configuring our package.
        sudo dpkg -i /tmp/AdsPower.deb

        echo "AdsPower install finished."
    '
}

# ---------------------------------------------------------------------------
# Step 9: systemd services
# ---------------------------------------------------------------------------
install_systemd_services() {
    log "Installing systemd service units (adspower, orchestrator)..."

    # Detect AdsPower binary path on the remote so the unit file is correct.
    local adspower_bin
    adspower_bin=$(remote_exec "
        for c in \
            /opt/AdsPower/adspower_global \
            /opt/AdsPower/AdsPower \
            /opt/adspower-global-service/adspower_global \
            /usr/bin/adspower_global \
            /usr/local/bin/adspower_global; do
            if [ -x \"\$c\" ]; then echo \"\$c\"; exit 0; fi
        done
        # fallback: search
        found=\$(command -v adspower_global 2>/dev/null || true)
        if [ -n \"\$found\" ]; then echo \"\$found\"; exit 0; fi
        found=\$(find /opt /usr/local/bin /usr/bin -maxdepth 4 -type f -iname 'adspower*' -executable 2>/dev/null | head -n1 || true)
        echo \"\$found\"
    " | tr -d '\r' | tail -n1)

    if [[ -z "${adspower_bin}" ]]; then
        warn "Could not auto-detect AdsPower binary path. Defaulting to /opt/AdsPower/adspower_global"
        warn "You may need to edit /etc/systemd/system/adspower.service after deployment."
        adspower_bin="/opt/AdsPower/adspower_global"
    else
        log "Detected AdsPower binary: ${adspower_bin}"
    fi

    # Detect orchestrator entry point.
    local orch_cmd
    orch_cmd=$(remote_exec "
        if [ -f '${REMOTE_DIR}/orchestrator/main.py' ]; then
            echo 'python3 main.py'
        elif [ -f '${REMOTE_DIR}/orchestrator/app.py' ]; then
            echo 'python3 app.py'
        elif [ -f '${REMOTE_DIR}/orchestrator/run.py' ]; then
            echo 'python3 run.py'
        elif [ -f '${REMOTE_DIR}/orchestrator/__main__.py' ]; then
            echo 'python3 -m orchestrator'
        else
            echo ''
        fi
    " | tr -d '\r' | tail -n1)

    if [[ -z "${orch_cmd}" ]]; then
        warn "No orchestrator entry point detected. Defaulting to 'python3 main.py'."
        warn "Edit /etc/systemd/system/orchestrator.service ExecStart after you add the entry point."
        orch_cmd="python3 main.py"
    else
        log "Detected orchestrator entry point: ${orch_cmd}"
    fi

    local adspower_unit
    adspower_unit=$(cat <<UNIT
[Unit]
Description=AdsPower Global (headless)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${REMOTE_USER}
Environment=DISPLAY=:0
Environment=HEADLESS=1
ExecStart=${adspower_bin} --headless --no-sandbox
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT
)

    local orchestrator_unit
    orchestrator_unit=$(cat <<UNIT
[Unit]
Description=WA Automation Orchestrator
After=network-online.target redis-server.service adspower.service
Wants=network-online.target
Requires=redis-server.service

[Service]
Type=simple
User=${REMOTE_USER}
WorkingDirectory=${REMOTE_DIR}/orchestrator
Environment=PYTHONUNBUFFERED=1
Environment=WA_AUTOMATION_HOME=${REMOTE_DIR}
ExecStart=/usr/bin/env ${orch_cmd}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT
)

    # Install both unit files via a heredoc-driven remote script.
    remote_bash "
        cat <<'ADSPOWER_UNIT' | sudo tee /etc/systemd/system/adspower.service >/dev/null
${adspower_unit}
ADSPOWER_UNIT

        cat <<'ORCH_UNIT' | sudo tee /etc/systemd/system/orchestrator.service >/dev/null
${orchestrator_unit}
ORCH_UNIT

        sudo systemctl daemon-reload
        sudo systemctl enable adspower.service orchestrator.service
        echo 'systemd unit files installed and enabled (not started yet).'
    "
}

# ---------------------------------------------------------------------------
# Final instructions
# ---------------------------------------------------------------------------
print_instructions() {
    cat <<EOF

============================================================
 Deployment complete.
============================================================

Remote host : ${REMOTE_USER}@${REMOTE_HOST}
Remote dir  : ${REMOTE_DIR}

To start the system:

  ssh ${REMOTE_USER}@${REMOTE_HOST}

  # 1. Make sure Redis is running
  sudo systemctl status redis-server

  # 2. Start AdsPower (headless)
  sudo systemctl start adspower
  sudo systemctl status adspower

  # 3. Start the orchestrator (the main entry point)
  sudo systemctl start orchestrator
  sudo systemctl status orchestrator

  # 4. Tail logs
  sudo journalctl -u adspower -f
  sudo journalctl -u orchestrator -f

  # 5. Run the wa-worker manually (or wrap in pm2 / its own service)
  cd ${REMOTE_DIR}/wa-worker && node worker.js

To stop services:
  sudo systemctl stop orchestrator adspower

To redeploy after local changes, just re-run:
  ./deploy.sh

If the orchestrator service fails because no entry point exists yet,
edit /etc/systemd/system/orchestrator.service (ExecStart=) and run:
  sudo systemctl daemon-reload && sudo systemctl restart orchestrator

============================================================
EOF
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    log "Starting deployment to ${REMOTE_USER}@${REMOTE_HOST}"
    preflight
    install_system_deps
    sync_code
    install_app_deps
    install_adspower
    install_systemd_services
    print_instructions
    log "All done."
}

main "$@"
