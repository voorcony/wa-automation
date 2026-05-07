import fs from 'node:fs';
import path from 'node:path';
import YAML from 'yaml';

const ENV_VAR_RE = /\$\{([A-Z0-9_]+)(?::-([^}]*))?\}/g;

function interpolateEnv(value) {
  if (typeof value !== 'string') return value;
  return value.replace(ENV_VAR_RE, (_, name, fallback) => {
    const envVal = process.env[name];
    if (envVal !== undefined && envVal !== '') return envVal;
    return fallback ?? '';
  });
}

function walk(obj) {
  if (Array.isArray(obj)) return obj.map(walk);
  if (obj && typeof obj === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(obj)) out[k] = walk(v);
    return out;
  }
  return interpolateEnv(obj);
}

export function loadConfig(configPath) {
  const abs = path.resolve(configPath);
  if (!fs.existsSync(abs)) {
    throw new Error(`Config file not found: ${abs}`);
  }
  const raw = fs.readFileSync(abs, 'utf8');
  const parsed = YAML.parse(raw);
  return walk(parsed);
}

export function parseArgs(argv = process.argv.slice(2)) {
  const args = {};
  for (const item of argv) {
    if (!item.startsWith('--')) continue;
    const eq = item.indexOf('=');
    if (eq === -1) {
      args[item.slice(2)] = true;
    } else {
      args[item.slice(2, eq)] = item.slice(eq + 1);
    }
  }
  return args;
}
