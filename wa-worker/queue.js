import axios from 'axios';

/**
 * In-memory FIFO retry queue for AI-engine forwarding.
 * Drops oldest entries past `maxSize` to avoid unbounded growth.
 */
export class RetryQueue {
  constructor({ aiUrl, logger, maxSize = 1000, retryIntervalMs = 5_000 }) {
    this.aiUrl = aiUrl.replace(/\/$/, '');
    this.logger = logger;
    this.maxSize = maxSize;
    this.retryIntervalMs = retryIntervalMs;
    this.items = [];
    this.timer = null;
    this.draining = false;
  }

  size() { return this.items.length; }

  enqueue(payload) {
    if (this.items.length >= this.maxSize) {
      const dropped = this.items.shift();
      this.logger.warn('Retry queue full, dropping oldest', { dropped_from: dropped?.from });
    }
    this.items.push({ payload, attempts: 0, enqueuedAt: Date.now() });
    this._ensureTimer();
  }

  _ensureTimer() {
    if (this.timer || this.items.length === 0) return;
    this.timer = setInterval(() => this._drain().catch(() => {}), this.retryIntervalMs);
  }

  _stopTimerIfEmpty() {
    if (this.items.length === 0 && this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  async _drain() {
    if (this.draining) return;
    this.draining = true;
    try {
      while (this.items.length > 0) {
        const head = this.items[0];
        try {
          await axios.post(`${this.aiUrl}/chat`, head.payload, {
            timeout: 30_000,
            headers: { 'Content-Type': 'application/json' },
          });
          this.items.shift();
          this.logger.info('Drained queued message to AI engine', { remaining: this.items.length });
        } catch (err) {
          head.attempts += 1;
          this.logger.debug('Retry failed; will try again', {
            attempts: head.attempts,
            err: err.message,
          });
          break;
        }
      }
    } finally {
      this.draining = false;
      this._stopTimerIfEmpty();
    }
  }

  stop() {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}

/**
 * Token-bucket-ish per-account rate limiter, default 1 msg/sec.
 * Calls to acquire() resolve in arrival order, spaced by `intervalMs`.
 */
export class RateLimiter {
  constructor({ intervalMs = 1000 } = {}) {
    this.intervalMs = intervalMs;
    this.lastAt = 0;
    this.chain = Promise.resolve();
  }

  acquire() {
    this.chain = this.chain.then(() => new Promise((resolve) => {
      const now = Date.now();
      const wait = Math.max(0, this.lastAt + this.intervalMs - now);
      setTimeout(() => {
        this.lastAt = Date.now();
        resolve();
      }, wait);
    }));
    return this.chain;
  }
}
