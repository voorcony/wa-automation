#!/bin/bash
# WhatsApp Health Check — Windows + Linux hybrid
# Run via cron: */2 * * * *

WA_DIR=/home/ubuntu/wa-automation
WORKER_PORT=8083
WINDOWS_IP=101.33.123.45
SSH_USER=administrator
SSH_PASS=ZHOUjiahao1!
ADSPOWER_API="http://$WINDOWS_IP:50325"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# 1. Ensure Windows browser is running — get fresh CDP endpoint
BROWSER_RESP=$(curl -sf --connect-timeout 5 "$ADSPOWER_API/api/v1/browser/start?user_id=k1c9cdsg" 2>/dev/null)
CDP_WS=$(echo "$BROWSER_RESP" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    if d.get('code')==0:
        print(d['data']['ws']['puppeteer'])
except: pass
" 2>/dev/null)

if [ -z "$CDP_WS" ]; then
    log "CRITICAL: Cannot start Windows browser"
    exit 1
fi

# Extract port from ws://127.0.0.1:PORT/...
CDP_PORT=$(echo "$CDP_WS" | sed 's|ws://127.0.0.1:\([0-9]*\)/.*|\1|')

# 2. Ensure SSH tunnel is running
TUNNEL_ALIVE=$(curl -sf --connect-timeout 3 http://127.0.0.1:$CDP_PORT/json/version > /dev/null 2>&1 && echo 1 || echo 0)

if [ "$TUNNEL_ALIVE" = "0" ]; then
    log "SSH tunnel DOWN (port $CDP_PORT) — restarting"
    pkill -f "ssh.*$CDP_PORT" 2>/dev/null || true
    pkill -f "ssh.*64214" 2>/dev/null || true
    pkill -f "ssh.*60739" 2>/dev/null || true
    sleep 2
    sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no \
        -o ServerAliveInterval=30 \
        -L $CDP_PORT:127.0.0.1:$CDP_PORT \
        -N "$SSH_USER@$WINDOWS_IP" > /tmp/ssh-tunnel.log 2>&1 &
    log "SSH tunnel started on port $CDP_PORT"
    sleep 4
fi

# 3. Verify tunnel
TUNNEL_OK=$(curl -sf --connect-timeout 3 http://127.0.0.1:$CDP_PORT/json/version > /dev/null 2>&1 && echo 1 || echo 0)
if [ "$TUNNEL_OK" = "0" ]; then
    log "Tunnel still DOWN after restart"
    exit 1
fi

# 4. Ensure worker is running with correct endpoint
WORKER_ALIVE=$(curl -sf http://localhost:$WORKER_PORT/status > /dev/null 2>&1 && echo 1 || echo 0)
WORKER_WS=$(curl -sf http://localhost:$WORKER_PORT/status 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    print(d.get('state',''))
except: pass
" 2>/dev/null)

if [ "$WORKER_ALIVE" = "0" ] || [ -z "$WORKER_WS" ]; then
    log "Worker DOWN — restarting with CDP: $CDP_PORT"
    pkill -f 'worker.js' 2>/dev/null || true
    sleep 2
    cd $WA_DIR/wa-worker && nohup node worker.js \
        --user-id=k1c9cdsg \
        --ws-endpoint="$CDP_WS" \
        --ai-url=http://localhost:8082 \
        --config=../config.yaml \
        --port=$WORKER_PORT > /tmp/wa-worker.log 2>&1 &
    log "Worker restarted with CDP: $CDP_WS"
    sleep 5
fi

STATE=$(curl -sf http://localhost:$WORKER_PORT/status 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    print(f'{d[\"state\"]} | connected={d[\"connected\"]}')
except: print('unknown')
" 2>/dev/null)

log "OK | CDP=$CDP_PORT | Worker=$STATE"
