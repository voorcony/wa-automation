#!/bin/bash
# Start Chrome + wa-worker reliably
# Run with: nohup bash start_wa.sh > /tmp/start_wa.log 2>&1 &

set -e

WA_DIR=/home/ubuntu/wa-automation/wa-worker
CHROME=/home/ubuntu/.cache/puppeteer/chrome/linux-146.0.7680.31/chrome-linux64/chrome

rm -rf "$WA_DIR/data/chrome-profiles/k1c9cdsg"
mkdir -p "$WA_DIR/data/chrome-profiles/k1c9cdsg" "$WA_DIR/data/wa-sessions"

# Start Chrome
$CHROME \
  --headless=new --no-sandbox --disable-setuid-sandbox --disable-gpu --disable-dev-shm-usage \
  --user-data-dir="$WA_DIR/data/chrome-profiles/k1c9cdsg" \
  --remote-debugging-port=19223 \
  --disable-background-timer-throttling --no-first-run --no-default-browser-check \
  --mute-audio --hide-scrollbars --disable-background-mode \
  > /tmp/chrome-stdout.log 2>/tmp/chrome-stderr.log &

# Wait for Chrome to be ready
for i in $(seq 1 10); do
    if grep -q 'DevTools listening' /tmp/chrome-stderr.log 2>/dev/null; then
        echo "Chrome ready after ${i}s"
        break
    fi
    sleep 1
done

WS=$(grep -oP 'ws://127.0.0.1:\d+/devtools/browser/[a-f0-9-]+' /tmp/chrome-stderr.log)
echo "CDP: $WS"

# Start WA Worker
cd "$WA_DIR"
node worker.js \
  --user-id=k1c9cdsg \
  --ws-endpoint="$WS" \
  --ai-url=http://localhost:8082 \
  --config=../config.yaml \
  --port=8083 > /tmp/wa-worker.log 2>&1 &

echo "WA Worker started"
echo "Chrome: PID $(pgrep -f 'chrome.*remote-debugging-port=19223' | head -1)"
echo "Worker: PID $(pgrep -f 'node worker.js' | head -1)"
