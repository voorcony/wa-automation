import winston from 'winston';

const { combine, timestamp, printf, colorize, errors, splat } = winston.format;

const textFormat = printf(({ level, message, timestamp: ts, userId, ...meta }) => {
  const metaStr = Object.keys(meta).length ? ` ${JSON.stringify(meta)}` : '';
  const tag = userId ? ` [${userId}]` : '';
  return `${ts} ${level}${tag} ${message}${metaStr}`;
});

export function createLogger(userId, level = 'info') {
  return winston.createLogger({
    level,
    defaultMeta: userId ? { userId } : {},
    format: combine(
      errors({ stack: true }),
      splat(),
      timestamp({ format: 'YYYY-MM-DD HH:mm:ss.SSS' }),
      colorize(),
      textFormat
    ),
    transports: [new winston.transports.Console()],
  });
}
