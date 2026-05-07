#!/usr/bin/env node
/**
 * WhatsApp worker: connects to AdsPower SunBrowser via CDP with stealth.
 *
 * Usage:
 *   node worker.js --user-id=k1c9cdsg --ws-endpoint=ws://127.0.0.1:PORT/... --ai-url=http://localhost:8082 [--config=../config.yaml] [--port=8083]
 */

import path from 'node:path';
import fs from 'node:fs';
import { execSync } from 'node:child_process';
import express from 'express';
import puppeteer from 'puppeteer-extra';
import StealthPlugin from 'puppeteer-extra-plugin-stealth';
import qrcodeTerminal from 'qrcode-terminal';
import qrcodePng from 'qrcode';
import pkg from 'whatsapp-web.js';

import { loadConfig, parseArgs } from './config.js';
import { createLogger } from './logger.js';
import { RetryQueue, RateLimiter } from './queue.js';
import { handleIncoming, handleOutgoing, parseMessage } from './message_handler.js';

const { Client, LocalAuth } = pkg;

// Apply stealth plugin to hide automation traces from WhatsApp
puppeteer.use(StealthPlugin());

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------
const args = parseArgs();
const required = ['user-id', 'ai-url', 'ws-endpoint'];
const missing = required.filter((k) => !args[k]);
if (missing.length) {
  console.error(`Missing required arg(s): ${missing.map((k) => '--' + k).join(', ')}`);
  console.error('Usage: node worker.js --user-id=<id> --ws-endpoint=ws://... --ai-url=http://localhost:8082 [--config=../config.yaml] [--port=8083]');
  process.exit(2);
}

const userId = String(args['user-id']);
const wsEndpoint = String(args['ws-endpoint']);
const aiUrl = String(args['ai-url']);
const configPath = args.config ? String(args.config) : path.resolve('../config.yaml');
const ADSPOWER_CLI = '/usr/bin/adspower-browser';
const PROFILE_ID = userId;

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

logger.info('Starting wa-worker (AdsPower + stealth)', { wsEndpoint, aiUrl, configPath, port, stateDir });

// ---------------------------------------------------------------------------
// Shared services
// ---------------------------------------------------------------------------
const queue = new RetryQueue({ aiUrl, logger, maxSize: 1000, retryIntervalMs: 5000 });
const rateLimiter = new RateLimiter({ intervalMs: 1000 });

let browser = null;
let client = null;
let qrCodeString = null;
let waState = 'INITIALIZING';
let isReady = false;
let httpServer = null;
let shuttingDown = false;

// ---------------------------------------------------------------------------
// Connect to AdsPower SunBrowser via puppeteer-extra (stealth)
// ---------------------------------------------------------------------------
async function connectBrowser() {
  logger.info('Connecting to AdsPower SunBrowser', { wsEndpoint });
  browser = await puppeteer.connect({
    browserWSEndpoint: wsEndpoint,
    defaultViewport: null,
  });
  logger.info('Connected to SunBrowser (stealth enabled)');

  browser.on('disconnected', () => {
    logger.error('SunBrowser disconnected');
    if (!shuttingDown) {
      gracefulExit(1, 'browser-disconnected').catch(() => process.exit(1));
    }
  });

  // Keep browser alive with periodic activity
  setInterval(async () => {
    try {
      const pages = await browser.pages();
      if (pages.length > 0) {
        await pages[0].evaluate('1+1').catch(() => {});
      }
    } catch (_) {}
  }, 15000);

  return browser;
}

// ---------------------------------------------------------------------------
// Initialize whatsapp-web.js Client against AdsPower browser
// ---------------------------------------------------------------------------
async function initWhatsApp() {
  const authStrategy = new LocalAuth({
    clientId: userId,
    dataPath: stateDir,
  });

  client = new Client({
    authStrategy,
    puppeteer: {
      puppeteer,                            // use puppeteer-extra with stealth
      browserWSEndpoint: wsEndpoint,
      defaultViewport: null,
    },
    qrMaxRetries: 5,
    takeoverOnConflict: true,
    takeoverTimeoutMs: 10_000,
  });

  client.on('qr', (qr) => {
    qrCodeString = qr;
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

  logger.info('Initializing WhatsApp client on SunBrowser...');
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
      has_qr: qrCodeString !== null,
    });
  });

  app.get('/qr', async (_req, res) => {
    if (!qrCodeString) {
      return res.status(404).json({ error: 'QR code not available yet', state: waState });
    }
    try {
      const pngBuffer = await qrcodePng.toBuffer(qrCodeString, { type: 'png', width: 400, margin: 2 });
      res.set('Content-Type', 'image/png');
      res.send(pngBuffer);
    } catch (err) {
      logger.error('QR generation error', { err: err.message });
      res.status(500).json({ error: 'QR generation failed' });
    }
  });

  app.get('/qr-data', (_req, res) => {
    res.json({ qr: qrCodeString, available: qrCodeString !== null });
  });

  // Send message to a phone number
  app.post('/api/send', async (req, res) => {
    try {
      const { to, message } = req.body;
      if (!to || !message) {
        return res.status(400).json({ error: 'Missing "to" or "message"' });
      }
      // Format number: add @c.us suffix for WhatsApp
      const chatId = to.includes('@c.us') ? to : `${to}@c.us`;
      const sent = await client.sendMessage(chatId, message);
      logger.info('Message sent', { to: chatId, id: sent.id.id });
      res.json({ success: true, message_id: sent.id.id });
    } catch (err) {
      logger.error('Send error', { err: err.message });
      res.status(500).json({ error: err.message });
    }
  });

  return app;
}

async function startHttp() {
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
      await browser.disconnect();
      logger.info('Disconnected from SunBrowser');
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
