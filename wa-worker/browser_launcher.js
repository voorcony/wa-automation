#!/usr/bin/env node
/**
 * Standalone browser launcher for headless servers.
 *
 * Launches a Chromium instance (the Puppeteer-downloaded one, NOT SunBrowser
 * which is broken on this headless server) and exposes:
 *   - The CDP WebSocket endpoint for wa-worker.js to connect to
 *   - A QR code PNG endpoint for the dashboard
 *
 * Usage:
 *   node browser_launcher.js --user-id=k1c9cdsg [--port=9222] [--chrome-path=...]
 *
 * The QR code is served at http://localhost:<port>/qr/<user-id>.png
 * The status is at       http://localhost:<port>/status/<user-id>
 */

import path from 'node:path';
import fs from 'node:fs';
import http from 'node:http';
import url from 'node:url';
import { spawn } from 'node:child_process';

const CHROME_PATH = process.env.CHROME_PATH ||
  '/home/ubuntu/.cache/puppeteer/chrome/linux-146.0.7680.31/chrome-linux64/chrome';

const args = {};
process.argv.slice(2).forEach((a) => {
  if (a.startsWith('--')) {
    const [k, v] = a.slice(2).split('=');
    args[k] = v ?? true;
  }
});

const userId = args['user-id'] || 'k1c9cdsg';
const proxyHost = 'gate.rola.vip';
const proxyPort = '2000';
const proxyUser = 'gyd602_5-country-us-state-ca';
const proxyPass = 'C0eLGm';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let chromeProcess = null;
let wsEndpoint = null;
let qrBase64 = null;
let browserReady = false;
let qrReady = false;

// ---------------------------------------------------------------------------
// 1. Launch Chromium in headless + remote debugging mode
// ---------------------------------------------------------------------------
function launchChrome() {
  const dataDir = path.resolve(`./data/chrome-profiles/${userId}`);
  fs.mkdirSync(dataDir, { recursive: true });

  const proxyUrl = `socks5://${proxyUser}:${proxyPass}@${proxyHost}:${proxyPort}`;

  const chromeArgs = [
    '--headless=new',
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-gpu',
    '--disable-dev-shm-usage',
    `--user-data-dir=${dataDir}`,
    '--remote-debugging-port=0',      // auto-assign port
    '--disable-background-timer-throttling',
    '--disable-backgrounding-occluded-windows',
    '--disable-renderer-backgrounding',
    '--no-first-run',
    '--no-default-browser-check',
    '--mute-audio',
    '--hide-scrollbars',
    '--window-position=0,0',
    '--disable-background-mode',
    `--proxy-server=${proxyUrl}`,
    'https://web.whatsapp.com',
  ];

  console.log(`[browser-launcher] Launching Chrome: ${CHROME_PATH}`);
  console.log(`[browser-launcher] Proxy: ${proxyUrl}`);

  chromeProcess = spawn(CHROME_PATH, chromeArgs, {
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env },
  });

  chromeProcess.stdout.on('data', (d) => process.stdout.write(`[chrome:stdout] ${d}`));
  chromeProcess.stderr.on('data', (d) => {
    const text = d.toString();
    process.stderr.write(`[chrome:stderr] ${text}`);

    // Parse DevTools listening port from stderr
    const match = text.match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)/);
    if (match) {
      const port = match[1];
      wsEndpoint = `ws://127.0.0.1:${port}/devtools/browser/${userId}`;
      browserReady = true;
      console.log(`[browser-launcher] Chrome ready! CDP: ${wsEndpoint}`);
    }

    // Parse QR code from WhatsApp Web page
    const qrMatch = text.match(/data:image\/png;base64,([A-Za-z0-9+/=]+)/);
    if (qrMatch) {
      qrBase64 = qrMatch[1];
      qrReady = true;
      console.log(`[browser-launcher] QR code captured!`);
    }
  });

  chromeProcess.on('exit', (code, signal) => {
    console.log(`[browser-launcher] Chrome exited: code=${code} signal=${signal}`);
    chromeProcess = null;
    browserReady = false;
  });
}

// ---------------------------------------------------------------------------
// 2. HTTP server for dashboard integration
// ---------------------------------------------------------------------------
const serverPort = Number(args.port || 19222);

function handleRequest(req, res) {
  const parsed = url.parse(req.url, true);
  const pathname = parsed.pathname;

  // CORS
  res.setHeader('Access-Control-Allow-Origin', '*');

  if (pathname === `/status/${userId}`) {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      user_id: userId,
      browser_ready: browserReady,
      qr_ready: qrReady,
      ws_endpoint: wsEndpoint,
      has_qr: qrBase64 !== null,
    }));
  } else if (pathname === `/qr/${userId}.png`) {
    if (!qrBase64) {
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'QR not yet available', ready: qrReady, browser_ready: browserReady }));
      return;
    }
    const img = Buffer.from(qrBase64, 'base64');
    res.writeHead(200, {
      'Content-Type': 'image/png',
      'Content-Length': img.length,
    });
    res.end(img);
  } else if (pathname === `/ws-endpoint/${userId}`) {
    if (!wsEndpoint) {
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'Browser not ready' }));
      return;
    }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ws_endpoint: wsEndpoint }));
  } else {
    res.writeHead(200, { 'Content-Type': 'text/plain' });
    res.end(`browser-launcher for ${userId}\nAvailable:\n  GET /status/${userId}\n  GET /qr/${userId}.png\n  GET /ws-endpoint/${userId}\n`);
  }
}

const server = http.createServer(handleRequest);
server.listen(serverPort, '0.0.0.0', () => {
  console.log(`[browser-launcher] HTTP server on http://0.0.0.0:${serverPort}`);
});

// ---------------------------------------------------------------------------
// Cleanup
// ---------------------------------------------------------------------------
function shutdown() {
  console.log('[browser-launcher] Shutting down...');
  if (chromeProcess) {
    chromeProcess.kill('SIGTERM');
    setTimeout(() => {
      if (chromeProcess) chromeProcess.kill('SIGKILL');
    }, 5000);
  }
  server.close(() => process.exit(0));
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);

// ---------------------------------------------------------------------------
// Go
// ---------------------------------------------------------------------------
launchChrome();
