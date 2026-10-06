// Cloudflare's scheduledTime is the original UTC event time, even on late delivery.
const KST_OFFSET_MS = 9 * 60 * 60 * 1000;
const REQUEST_TIMEOUT_MS = 20_000;
const GET_MAX_ATTEMPTS = 3;
const GET_RETRY_DELAYS_MS = [1_000, 3_000];
const MAX_RETRY_AFTER_MS = 30_000;
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const GITHUB_API = 'https://api.github.com';
const WORKFLOW_FILE = 'update-news.yml';
const UTC_HOUR_SLOTS = Object.freeze({ 20: '06:00', 3: '13:00', 9: '19:00' });

const CRON_PLANS = Object.freeze({
  '7 20 * * *': { slot: '06:00', leadMinutes: 53, recovery: false },
  '7 3 * * *': { slot: '13:00', leadMinutes: 53, recovery: false },
  '7 9 * * *': { slot: '19:00', leadMinutes: 53, recovery: false },
  // Group the three daily slots in two triggers to stay within Workers Free's
  // five-cron account limit. scheduledTime's UTC hour selects the KST slot.
  '27 3,9,20 * * *': { leadMinutes: 33, recovery: true },
  '47 3,9,20 * * *': { leadMinutes: 13, recovery: true },
});
export const CRON_SLOTS = Object.freeze(Object.fromEntries(
  Object.entries(CRON_PLANS).filter(([, plan]) => plan.slot)
    .map(([cron, plan]) => [cron, plan.slot])
));

export class SchedulerError extends Error {
  constructor(code, status = null, retryAfterMs = null) {
    const messages = {
      SCHEDULE_INVALID: 'Invalid scheduled event',
      CONFIG_INVALID: 'Invalid scheduler configuration',
      TOKEN_MISSING: 'GITHUB_TOKEN secret is required',
      GITHUB_HTTP: `GitHub HTTP ${status}`,
      GITHUB_TIMEOUT: 'GitHub request timed out',
      GITHUB_REQUEST_FAILED: 'GitHub request failed',
      GITHUB_RESPONSE_INVALID: 'Invalid GitHub response format',
    };
    super(messages[code] || 'Scheduler failed');
    this.name = 'SchedulerError';
    this.code = code;
    this.status = status;
    this.retryAfterMs = retryAfterMs;
  }
}

export function planPublication(event) {
  const cron = typeof event?.cron === 'string'
    ? event.cron.trim().replace(/\s+/g, ' ') : '';
  const config = CRON_PLANS[cron];
  const timestamp = event?.scheduledTime;
  if (!config || !Number.isFinite(timestamp) || timestamp < 0) {
    throw new SchedulerError('SCHEDULE_INVALID');
  }
  const prepared = new Date(timestamp);
  const [minuteText, hourText] = cron.split(' ');
  const minute = Number(minuteText);
  const allowedHours = hourText.split(',').map(Number);
  const hour = prepared.getUTCHours();
  const slot = config.slot || UTC_HOUR_SLOTS[hour];
  if (!Number.isFinite(prepared.getTime()) || !allowedHours.includes(hour)
      || prepared.getUTCMinutes() !== minute || !slot) {
    throw new SchedulerError('SCHEDULE_INVALID');
  }
  prepared.setUTCSeconds(0, 0);
  const localTarget = new Date(prepared.getTime() + config.leadMinutes * 60_000 + KST_OFFSET_MS);
  const publishAt = `${localTarget.toISOString().slice(0, 19)}+09:00`;
  return { slot, publishAt, displayTitle: `News ${slot} ${publishAt}`,
    recovery: config.recovery };
}

function configuration(env) {
  const token = typeof env?.GITHUB_TOKEN === 'string' ? env.GITHUB_TOKEN.trim() : '';
  if (!token) throw new SchedulerError('TOKEN_MISSING');
  const repository = env.GITHUB_REPOSITORY;
  const ref = env.GITHUB_REF;
  if (typeof repository !== 'string'
      || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(repository)
      || typeof ref !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.\/-]{0,199}$/.test(ref)) {
    throw new SchedulerError('CONFIG_INVALID');
  }
  return { token, repository, ref };
}

function retryAfterMilliseconds(value) {
  if (typeof value !== 'string' || !value.trim()) return null;
  const text = value.trim();
  let delay;
  if (/^\d+$/.test(text)) {
    delay = Number(text) * 1_000;
  } else if (/^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)/i.test(text)) {
    // HTTP dates start with a weekday. Do not treat negative/fractional seconds
    // or arbitrary numeric strings as dates accepted by Date.parse().
    // Obsolete HTTP asctime dates omit GMT but still mean UTC, including when
    // the same parser is tested on a computer with a non-UTC timezone.
    delay = Date.parse(/GMT$/i.test(text) ? text : `${text} GMT`) - Date.now();
  } else {
    return null;
  }
  return Number.isFinite(delay) ? Math.min(MAX_RETRY_AFTER_MS, Math.max(0, delay)) : null;
}

async function githubRequest(url, options, fetchImpl, expectedStatus, readJson = false) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetchImpl(url, {
      ...options,
      redirect: 'manual',
      signal: controller.signal,
    });
    if (response.status !== expectedStatus) {
      // Never inspect or report error bodies, which may include sensitive data.
      const retryAfterMs = options.method === 'GET'
        ? retryAfterMilliseconds(response.headers.get('Retry-After')) : null;
      throw new SchedulerError('GITHUB_HTTP', response.status, retryAfterMs);
    }
    if (!readJson) return null;
    try {
      return await response.json();
    } catch (_) {
      if (controller.signal.aborted) throw new SchedulerError('GITHUB_TIMEOUT');
      throw new SchedulerError('GITHUB_RESPONSE_INVALID');
    }
  } catch (error) {
    if (error instanceof SchedulerError) throw error;
    // Transport exceptions can contain URLs, request headers, or secret values.
    throw new SchedulerError(controller.signal.aborted
      ? 'GITHUB_TIMEOUT' : 'GITHUB_REQUEST_FAILED');
  } finally {
    clearTimeout(timer);
  }
}

async function githubGet(url, headers, fetchImpl, sleepImpl) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await githubRequest(url, { method: 'GET', headers }, fetchImpl, 200, true);
    } catch (error) {
      const retryable = error instanceof SchedulerError && (
        error.code === 'GITHUB_TIMEOUT' || error.code === 'GITHUB_REQUEST_FAILED'
        || (error.code === 'GITHUB_HTTP' && (error.status === 429
          || (error.status >= 500 && error.status <= 599)))
      );
      if (!retryable || attempt + 1 >= GET_MAX_ATTEMPTS) throw error;
      const delay = Math.max(GET_RETRY_DELAYS_MS[attempt], error.retryAfterMs ?? 0);
      await sleepImpl(delay);
    }
  }
}

export async function dispatchScheduled(event, env, fetchImpl = globalThis.fetch, sleepImpl = sleep) {
  const plan = planPublication(event);
  const { token, repository, ref } = configuration(env);
  const workflow = `${GITHUB_API}/repos/${repository}/actions/workflows/${WORKFLOW_FILE}`;
  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    'User-Agent': 'kiuk-news-scheduler/1.0',
    'Cache-Control': 'no-cache',
  };
  const query = new URLSearchParams({ event: 'workflow_dispatch', branch: ref, per_page: '100' });
  const recent = await githubGet(`${workflow}/runs?${query}`, headers, fetchImpl, sleepImpl);
  if (!Array.isArray(recent?.workflow_runs)) {
    throw new SchedulerError('GITHUB_RESPONSE_INVALID');
  }
  // Every delivery checks the original full timestamp before mutating GitHub.
  // This also covers a prior dispatch accepted without a received HTTP ack.
  const matches = recent.workflow_runs.filter(run => run?.display_title === plan.displayTitle);
  if (matches.length && !plan.recovery) {
    return { status: 'duplicate', ...plan };
  }
  if (matches.length > 1) {
    // Ambiguous existing runs: never create a third run or rerun one blindly.
    return { status: 'duplicate', ...plan };
  }
  if (matches.length === 1) {
    const run = matches[0];
    if (run.status !== 'completed'
        || !['failure', 'cancelled', 'timed_out'].includes(run.conclusion)) {
      return { status: 'duplicate', ...plan };
    }
    if (!Number.isSafeInteger(run.id) || run.id <= 0
        || !Number.isSafeInteger(run.run_attempt) || run.run_attempt <= 0) {
      throw new SchedulerError('GITHUB_RESPONSE_INVALID');
    }
    if (run.run_attempt >= 3) return { status: 'retry-limit', ...plan };
    // Re-run the same original run, preserving its full publication timestamp.
    // POST is intentionally never retried after an uncertain outcome.
    await githubRequest(`${GITHUB_API}/repos/${repository}/actions/runs/${run.id}/rerun`, {
      method: 'POST', headers,
    }, fetchImpl, 201);
    return { status: 'rerun', ...plan };
  }
  await githubRequest(`${workflow}/dispatches`, {
    method: 'POST',
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify({ ref, inputs: {
      mode: 'collect', scheduled_slot: plan.slot, publish_at: plan.publishAt,
    } }),
  }, fetchImpl, 204);
  return { status: 'dispatched', ...plan };
}

export default {
  async scheduled(event, env) {
    try {
      const result = await dispatchScheduled(event, env);
      console.log(`News scheduler ${result.status}: ${result.slot} ${result.publishAt}`);
    } catch (error) {
      const safe = error instanceof SchedulerError
        ? error : new SchedulerError('GITHUB_REQUEST_FAILED');
      console.error(`News scheduler failed: ${safe.message}`);
      throw safe;
    }
  },
};
