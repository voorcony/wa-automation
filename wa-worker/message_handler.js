import axios from 'axios';

/**
 * Extract a normalized representation of a whatsapp-web.js Message.
 * @param {import('whatsapp-web.js').Message} msg
 */
export function parseMessage(msg) {
  const isGroup = typeof msg.from === 'string' && msg.from.endsWith('@g.us');
  return {
    id: msg.id?._serialized ?? msg.id?.id ?? null,
    sender: msg.from,
    author: msg.author ?? msg.from,
    to: msg.to,
    body: msg.body ?? '',
    type: msg.type,
    timestamp: msg.timestamp ?? Math.floor(Date.now() / 1000),
    isGroup,
    fromMe: !!msg.fromMe,
    hasMedia: !!msg.hasMedia,
  };
}

/**
 * Forward an incoming WA message to the AI engine, enqueueing on failure.
 *
 * @param {object} ctx
 * @param {import('whatsapp-web.js').Message} msg
 */
export async function handleIncoming(ctx, msg) {
  const { client, aiUrl, accountId, logger, queue, rateLimiter } = ctx;
  const parsed = parseMessage(msg);

  if (parsed.fromMe) {
    logger.debug('Skipping own message', { id: parsed.id });
    return;
  }

  const payload = {
    account_id: accountId,
    from: parsed.sender,
    body: parsed.body,
    timestamp: parsed.timestamp,
    is_group: parsed.isGroup,
    message_id: parsed.id,
    type: parsed.type,
  };

  logger.info('Incoming message', { from: parsed.sender, isGroup: parsed.isGroup, len: parsed.body.length });

  try {
    const res = await axios.post(`${aiUrl.replace(/\/$/, '')}/chat`, payload, {
      timeout: 30_000,
      headers: { 'Content-Type': 'application/json' },
    });
    logger.info('AI engine accepted message', { status: res.status });

    const reply = res.data?.reply ?? res.data?.message ?? null;
    if (reply && typeof reply === 'string' && reply.trim().length > 0) {
      await handleOutgoing({ client, logger, rateLimiter }, parsed.sender, reply);
    }
    return res.data;
  } catch (err) {
    const status = err.response?.status;
    logger.warn('AI engine call failed; queuing message', {
      err: err.message,
      status,
      queueSize: queue.size() + 1,
    });
    queue.enqueue(payload);
    return null;
  }
}

/**
 * Send a WA message with a small typing-indicator delay, respecting per-account rate limits.
 *
 * @param {{ client: import('whatsapp-web.js').Client, logger: any, rateLimiter: { acquire: () => Promise<void> } }} ctx
 * @param {string} to chatId, e.g. "1234567890@c.us"
 * @param {string} text
 */
export async function handleOutgoing(ctx, to, text) {
  const { client, logger, rateLimiter } = ctx;
  if (!to || typeof text !== 'string' || text.length === 0) {
    throw new Error('handleOutgoing requires "to" and non-empty "text"');
  }

  await rateLimiter.acquire();

  let chat = null;
  try {
    chat = await client.getChatById(to);
    await chat.sendStateTyping();
    const delay = Math.min(2500, 400 + Math.min(text.length, 200) * 15);
    await new Promise((r) => setTimeout(r, delay));
  } catch (e) {
    logger.debug('Could not set typing indicator', { err: e.message });
  }

  try {
    const sent = await client.sendMessage(to, text);
    logger.info('Sent message', { to, len: text.length, id: sent?.id?._serialized });
    if (chat) {
      try { await chat.clearState(); } catch { /* ignore */ }
    }
    return sent;
  } catch (err) {
    logger.error('Failed to send message', { to, err: err.message });
    throw err;
  }
}
