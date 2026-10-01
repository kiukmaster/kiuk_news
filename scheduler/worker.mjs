// Cloudflare's scheduledTime is the original UTC event time, even on late delivery.
const PREPARATION_LEAD_MS = 53 * 60 * 1000;
const KST_OFFSET_MS = 9 * 60 * 60 * 1000;
const REQUEST_TIMEOUT_MS = 20_000;
const GITHUB_API = 'https://api.github.com';
const WORKFLOW_FILE = 'update-news.yml';

export const CRON_SLOTS = Object.freeze({
  '7 20 * * *': '06:00',
  '7 3 * * *': '13:00',
  '7 9 * * *': '19:00',
});

export class SchedulerError extends Error {
  constructor(code, status = null) {
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
  }
}

export function planPublication(event) {
  const cron = typeof event?.cron === 'string'
    ? event.cron.trim().replace(/\s+/g, ' ') : '';
  const slot = CRON_SLOTS[cron];
  const timestamp = event?.scheduledTime;
  if (!slot || !Number.isFinite(timestamp) || timestamp < 0) {
    throw new SchedulerError('SCHEDULE_INVALID');
  }
  const prepared = new Date(timestamp);
  const [minute, hour] = cron.split(' ').map(Number);
  if (!Number.isFinite(prepared.getTime()) || prepared.getUTCHours() !== hour
      || prepared.getUTCMinutes() !== minute) {
    throw new SchedulerError('SCHEDULE_INVALID');
  }
  prepared.setUTCSeconds(0, 0);
  const localTarget = new Date(prepared.getTime() + PREPARATION_LEAD_MS + KST_OFFSET_MS);
  const publishAt = `${localTarget.toISOString().slice(0, 19)}+09:00`;
  return { slot, publishAt, displayTitle: `News ${slot} ${publishAt}` };
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
      throw new SchedulerError('GITHUB_HTTP', response.status);
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

export async function dispatchScheduled(event, env, fetchImpl = globalThis.fetch) {
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
  const query = new URLSearchParams({ event: 'workflow_dispatch', branch: ref, per_page: '20' });
  const recent = await githubRequest(`${workflow}/runs?${query}`, {
    method: 'GET', headers,
  }, fetchImpl, 200, true);
  if (!Array.isArray(recent?.workflow_runs)) {
    throw new SchedulerError('GITHUB_RESPONSE_INVALID');
  }
  // Every delivery checks the original full timestamp before mutating GitHub.
  // This also covers a prior dispatch accepted without a received HTTP ack.
  if (recent.workflow_runs.some(run => run?.display_title === plan.displayTitle)) {
    return { status: 'duplicate', ...plan };
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
