# wa-worker

Node.js worker that bridges an existing Chromium browser (e.g. one launched by
AdsPower) to an AI engine via [`whatsapp-web.js`](https://wwebjs.dev/).

## Install

```bash
cd wa-worker
npm install
```

## Run

```bash
node worker.js \
  --user-id=acct_001 \
  --ws-endpoint=ws://127.0.0.1:9222/devtools/browser/<id> \
  --ai-url=http://localhost:8082 \
  --config=../config.yaml \
  --port=8083
```

The worker connects to the **existing** Chromium via `puppeteer.connect({ browserWSEndpoint })`
— it does **not** launch its own browser. The same `browserWSEndpoint` is forwarded into
`whatsapp-web.js` so the same browser is reused.

## HTTP API

| Method | Path        | Body                      | Description                             |
| ------ | ----------- | ------------------------- | --------------------------------------- |
| GET    | `/status`   | —                         | `{ user_id, connected, state, queue_size }` |
| GET    | `/healthz`  | —                         | basic liveness                          |
| POST   | `/send`     | `{ to, message }`         | send a WA message (rate-limited)        |
| POST   | `/shutdown` | —                         | gracefully shut the worker down         |

## Behavior

- QR codes are printed to the console with `qrcode-terminal`.
- Sessions persist on disk under `wa_worker.state_dir` (default `./data/wa-sessions`)
  via `LocalAuth`, keyed by `--user-id`.
- Incoming messages (excluding own) are POSTed to `{ai-url}/chat` as
  `{ account_id, from, body, timestamp, is_group, message_id, type }`.
- If the AI engine is down the payload is queued in memory and retried every 5 s.
- All outgoing sends go through a 1 msg/sec rate limiter per account.
