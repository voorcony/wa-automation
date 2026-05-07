#!/usr/bin/env node
/**
 * WhatsApp worker: connects to an EXISTING Chromium browser via puppeteer
 * (browserWSEndpoint) and bridges WhatsApp Web messages to an AI engine.
 *
 * Usage:
 *   node worker.js \
 *     --user-id=<id> \
 *     --ws-endpoint=ws://127.0.0.1:9222/devtools/browser/xxxx \
 *     --ai-url=http://localhost:8082 \
 *     --config=../config.yaml \
 *     [--port=8083]
 */

import path from 'node:path';
import fs from 'node:fs';
import express from 'express';
import puppeteer from 'puppeteer';
import qrcodeTerminal from 'qrcode-terminal';
import pkg from 'whatsapp-web.js';

import { loadConfig, parseArgs } from './config.js';
import { createLogger } from './logger.js';
import { RetryQueue, RateLimiter } from './queue.js';
import { handleIncoming, handleOutgoing, parseMessage } from './message_handler.js';

const { Client, LocalAuth } = pkg;

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------
const args = parseArgs();
const required = ['user-id', 'ws-endpoint', 'ai-url'];
const missing = required.filter((k) => !args[k]);
if (missing.length) {
  console.error(`Missing required arg(s): ${missing.map((k) => '--' + k).join(', ')}`);
  console.error('Usage: node worker.js --user-id=<id> --ws-endpoint=ws://... --ai-url=http://... [--config=../config.yaml] [--port=8083]');
  process.exit(2);
}

const userId = String(args['user-id']);
const wsEndpoint = String(args['ws-endpoint']);
const aiUrl = String(args['ai-url']);
const configPath = args.config ? String(args.config) : path.resolve('../config.yaml');

// ---------------------------------------------------------------------------
// Config + logger
// ---------------------------------------------------------------------------
let config = {};
try {
  config = loadConfig(configPath);
} catch (err) {
  console.error(`[wa-worker] Failed to load config: ${err.message}`);
  process.exit(2);
}

const logLevel = (config?.app?.log_level || 'INFO').toString().toLowerCase();
const logger = createLogger(userId, logLevel);

const port = Number(args.port ?? config?.wa_worker?.port ?? 8083);
const host = String(config?.wa_worker?.host ?? '0.0.0.0');
const stateDir = path.resolve(config?.wa_worker?.state_dir ?? './data/wa-sessions');
fs.mkdirSync(stateDir, { recursive: true });

logger.info('Starting wa-worker', { wsEndpoint, aiUrl, configPath, port, stateDir });

// ---------------------------------------------------------------------------
// Shared services
// ---------------------------------------------------------------------------
const queue = new RetryQueue({ aiUrl, logger, maxSize: 1000, retryIntervalMs: 5000 });
const rateLimiter = new RateLimiter({ intervalMs: 1000 });

let browser = null;
let client = null;
let waState = 'INITIALIZING';
let isReady = false;
let httpServer = null;
let shuttingDown = false;

// ---------------------------------------------------------------------------
// Connect to existing Chromium
// ---------------------------------------------------------------------------
async function connectBrowser() {
  logger.info('Connecting to existing Chromium', { wsEndpoint });
  browser = await puppeteer.connect({
    browserWSEndpoint: wsEndpoint,
    defaultViewport: null,
  });
  logger.info('Connected to Chromium');

  browser.on('disconnected', () => {
    logger.error('Chromium browser disconnected');
    if (!shuttingDown) {
      gracefulExit(1, 'browser-disconnected').catch(() => process.exit(1));
    }
  });
  return browser;
}

// ---------------------------------------------------------------------------
// Initialize whatsapp-web.js Client against existing browser
// ---------------------------------------------------------------------------
async function initWhatsApp() {
  const authStrategy = new LocalAuth({
    clientId: userId,
    dataPath: stateDir,
  });

  client = new Client({
    authStrategy,
    puppeteer: {
      browserWSEndpoint: wsEndpoint,
      defaultViewport: null,
    },
    qrMaxRetries: 5,
    takeoverOnConflict: true,
    takeoverTimeoutMs: 10_000,
  });

  client.on('qr', (qr) => {
    logger.info('QR received - scan with your phone');
    qrcodeTerminal.generate(qr, { small: true });
  });

  client.on('loading_screen', (percent, message) => {
    logger.info('Loading WhatsApp', { percent, message });
  });

  client.on('authenticated', () => {
    logger.info('WhatsApp authenticated');
    waState = 'AUTHENTICATED';
  });

  client.on('auth_failure', (msg) => {
    logger.error('Authentication failure', { msg });
    waState = 'AUTH_FAILURE';
  });

  client.on('ready', () => {
    isReady = true;
    waState = 'READY';
    logger.info(`WhatsApp ready for account: ${userId}`);
  });

  client.on('change_state', (state) => {
    waState = String(state);
    logger.info('WA state changed', { state });
  });

  client.on('disconnected', (reason) => {
    isReady = false;
    waState = 'DISCONNECTED';
    logger.warn('WhatsApp disconnected', { reason });
  });

  // Use message_create so we also see messages sent from this device,
  // but skip own messages when forwarding to the AI.
  client.on('message_create', async (msg) => {
    try {
      if (msg.fromMe) {
        const parsed = parseMessage(msg);
        logger.debug('Outgoing message_create', { to: parsed.to, len: parsed.body.length });
        return;
      }
      await handleIncoming(
        { client, aiUrl, accountId: userId, logger, queue, rateLimiter },
        msg
      );
    } catch (err) {
      logger.error('message_create handler error', { err: err.message, stack: err.stack });
    }
  });

  client.on('error', (err) => {
    logger.error('Client error', { err: err?.message ?? String(err) });
  });

  logger.info('Initializing WhatsApp client...');
  await client.initialize();
}

// ---------------------------------------------------------------------------
// HTTP control plane
// ---------------------------------------------------------------------------
function buildHttpApp() {
  const app = express();
  app.use(express.json({ limit: '1mb' }));

  app.get('/status', (_req, res) => {
    res.json({
      user_id: userId,
      connected: isReady,
      state: waState,
      queue_size: queue.size(),
    });
  });

  app.get('/healthz', (_req, res) => {
    res.json({ ok: true, ready: isReady });
  });

  app.post('/send', async (req, res) => {
    if (!isReady || !client) {
      return res.status(503).json({ ok: false, error: 'WhatsApp client not ready' });
    }
    const { to, message } = req.body ?? {};
    if (!to || typeof message !== 'string' || message.length === 0) {
      return res.status(400).json({ ok: false, error: 'Body must be { to: string, message: non-empty string }' });
    }
    try {
      const sent = await handleOutgoing({ client, logger, rateLimiter }, to, message);
      return res.json({
        ok: true,
        id: sent?.id?._serialized ?? null,
        to,
        timestamp: sent?.timestamp ?? Math.floor(Date.now() / 1000),
      });
    } catch (err) {
      logger.error('POST /send failed', { err: err.message });
      return res.status(500).json({ ok: false, error: err.message });
    }
  });

  app.post('/shutdown', async (_req, res) => {
    res.json({ ok: true, shutting_down: true });
    setImmediate(() => gracefulExit(0, 'http-shutdown').catch(() => process.exit(0)));
  });

  // eslint-disable-next-line no-unused-vars
  app.use((err, _req, res, _next) => {
    logger.error('HTTP error', { err: err.message });
    res.status(500).json({ ok: false, error: err.message });
  });

  return app;
}

function startHttp() {
  return new Promise((resolve, reject) => {
    const app = buildHttpApp();
    httpServer = app.listen(port, host, () => {
      logger.info(`HTTP control plane listening on http://${host}:${port}`);
      resolve(httpServer);
    });
    httpServer.on('error', reject);
  });
}

// ---------------------------------------------------------------------------
// Graceful shutdown
// ---------------------------------------------------------------------------
async function gracefulExit(code, reason) {
  if (shuttingDown) return;
  shuttingDown = true;
  logger.info('Shutting down', { reason, code });

  queue.stop();

  if (httpServer) {
    await new Promise((r) => httpServer.close(() => r()));
    logger.info('HTTP server closed');
  }

  if (client) {
    try {
      await client.destroy();
      logger.info('WA client destroyed');
    } catch (err) {
      logger.warn('Error destroying WA client', { err: err.message });
    }
  }

  if (browser) {
    try {
      // disconnect, do NOT close — the browser is owned by the parent process (e.g. AdsPower)
      await browser.disconnect();
      logger.info('Disconnected from Chromium');
    } catch (err) {
      logger.warn('Error disconnecting browser', { err: err.message });
    }
  }

  setTimeout(() => process.exit(code), 250).unref();
}

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => {
    gracefulExit(0, sig).catch(() => process.exit(0));
  });
}

process.on('unhandledRejection', (reason) => {
  logger.error('unhandledRejection', { reason: reason?.message ?? String(reason) });
});
process.on('uncaughtException', (err) => {
  logger.error('uncaughtException', { err: err.message, stack: err.stack });
  gracefulExit(1, 'uncaughtException').catch(() => process.exit(1));
});

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
(async () => {
  try {
    await connectBrowser();
    await startHttp();
    await initWhatsApp();
  } catch (err) {
    logger.error('Fatal startup error', { err: err.message, stack: err.stack });
    await gracefulExit(1, 'startup-error');
  }
})();
