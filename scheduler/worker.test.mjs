import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import worker, { CRON_SLOTS, SchedulerError, dispatchScheduled, planPublication } from './worker.mjs';

const ENV = {
  GITHUB_TOKEN: 'test-secret-do-not-print',
  GITHUB_REPOSITORY: 'kiukmaster/kiuk_news',
  GITHUB_REF: 'main',
};
const event = (cron = '7 3 * * *', iso = '2026-10-01T03:07:00Z') => ({
  cron, scheduledTime: Date.parse(iso),
});
const json = value => new Response(JSON.stringify(value), { status: 200 });
const emptyRuns = () => json({ workflow_runs: [] });

for (const [cron, iso, expected] of [
  ['7 20 * * *', '2026-09-30T20:07:00Z', '2026-10-01T06:00:00+09:00'],
  ['7 3 * * *', '2026-10-01T03:07:00Z', '2026-10-01T13:00:00+09:00'],
  ['7 9 * * *', '2026-10-01T09:07:00Z', '2026-10-01T19:00:00+09:00'],
  ['7 20 * * *', '2026-12-31T20:07:00Z', '2027-01-01T06:00:00+09:00'],
]) {
  test(`UTC cron ${cron} preserves KST publication date ${expected}`, () => {
    const plan = planPublication(event(cron, iso));
    assert.equal(plan.publishAt, expected);
    assert.equal(plan.slot, CRON_SLOTS[cron]);
    assert.equal(plan.displayTitle, `News ${plan.slot} ${expected}`);
  });
}

test('late cron delivery uses scheduledTime instead of current date', async () => {
  const originalNow = Date.now;
  Date.now = () => Date.parse('2026-10-03T15:00:00Z');
  try {
    const calls = [];
    await dispatchScheduled(event(), ENV, async (url, options) => {
      calls.push({ url, options });
      return options.method === 'GET' ? emptyRuns() : new Response(null, { status: 204 });
    });
    assert.equal(JSON.parse(calls[1].options.body).inputs.publish_at,
      '2026-10-01T13:00:00+09:00');
  } finally {
    Date.now = originalNow;
  }
});

test('dispatch posts only configured repository, workflow, ref, and full KST target', async () => {
  const calls = [];
  const result = await dispatchScheduled(event(), ENV, async (url, options) => {
    calls.push({ url, options });
    return options.method === 'GET' ? emptyRuns() : new Response(null, { status: 204 });
  });
  assert.equal(result.status, 'dispatched');
  assert.equal(calls.length, 2);
  const list = new URL(calls[0].url);
  assert.equal(list.origin, 'https://api.github.com');
  assert.equal(list.pathname, '/repos/kiukmaster/kiuk_news/actions/workflows/update-news.yml/runs');
  assert.equal(list.searchParams.get('event'), 'workflow_dispatch');
  assert.equal(list.searchParams.get('branch'), 'main');
  assert.equal(list.searchParams.get('per_page'), '20');
  assert.equal(calls[1].url, 'https://api.github.com/repos/kiukmaster/kiuk_news/actions/workflows/update-news.yml/dispatches');
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    ref: 'main', inputs: { mode: 'collect', scheduled_slot: '13:00',
      publish_at: '2026-10-01T13:00:00+09:00' },
  });
  for (const { options } of calls) {
    assert.equal(options.redirect, 'manual');
    assert.ok(options.signal instanceof AbortSignal);
    assert.equal(options.headers.Authorization, `Bearer ${ENV.GITHUB_TOKEN}`);
  }
});

test('already accepted full target is skipped on repeat or uncertain ack delivery', async () => {
  let calls = 0;
  const plan = planPublication(event());
  const result = await dispatchScheduled(event(), ENV, async (_, options) => {
    calls += 1;
    assert.equal(options.method, 'GET');
    return json({ workflow_runs: [{ display_title: plan.displayTitle, status: 'queued' }] });
  });
  assert.equal(result.status, 'duplicate');
  assert.equal(calls, 1);
});

test('same slot from a different date does not suppress current target', async () => {
  const methods = [];
  await dispatchScheduled(event(), ENV, async (_, options) => {
    methods.push(options.method);
    return options.method === 'GET'
      ? json({ workflow_runs: [{ display_title: 'News 13:00 2026-09-30T13:00:00+09:00' }] })
      : new Response(null, { status: 204 });
  });
  assert.deepEqual(methods, ['GET', 'POST']);
});

test('401 stops before mutation and does not expose response or token', async () => {
  let calls = 0;
  await assert.rejects(dispatchScheduled(event(), ENV, async () => {
    calls += 1;
    return new Response(`private response ${ENV.GITHUB_TOKEN}`, { status: 401 });
  }), error => {
    assert.equal(error.message, 'GitHub HTTP 401');
    assert.equal(error.status, 401);
    assert.ok(!String(error.stack).includes(ENV.GITHUB_TOKEN));
    assert.ok(!JSON.stringify(error).includes('private response'));
    return true;
  });
  assert.equal(calls, 1);
});

test('redirect is rejected without following or forwarding authorization', async () => {
  let calls = 0;
  await assert.rejects(dispatchScheduled(event(), ENV, async (_, options) => {
    calls += 1;
    assert.equal(options.redirect, 'manual');
    return new Response(null, { status: 302, headers: { Location: 'https://outside.invalid/steal' } });
  }), /GitHub HTTP 302/);
  assert.equal(calls, 1);
});

test('failed POST is attempted once and never blindly retried', async () => {
  const methods = [];
  await assert.rejects(dispatchScheduled(event(), ENV, async (_, options) => {
    methods.push(options.method);
    return options.method === 'GET' ? emptyRuns() : new Response('untrusted detail', { status: 503 });
  }), /GitHub HTTP 503/);
  assert.deepEqual(methods, ['GET', 'POST']);
});

test('uncertain POST transport error is sanitized, then a later delivery checks runs', async () => {
  const methods = [];
  await assert.rejects(dispatchScheduled(event(), ENV, async (_, options) => {
    methods.push(options.method);
    if (options.method === 'GET') return emptyRuns();
    throw new Error(`request authorization ${ENV.GITHUB_TOKEN}`);
  }), error => {
    assert.equal(error.message, 'GitHub request failed');
    assert.ok(!String(error.stack).includes(ENV.GITHUB_TOKEN));
    return true;
  });
  assert.deepEqual(methods, ['GET', 'POST']);
  const result = await dispatchScheduled(event(), ENV, async () => json({
    workflow_runs: [{ display_title: planPublication(event()).displayTitle }],
  }));
  assert.equal(result.status, 'duplicate');
});

test('HTTP deadline is 20 seconds and abort errors are sanitized', async () => {
  const originalTimer = globalThis.setTimeout;
  let deadline;
  globalThis.setTimeout = (callback, milliseconds) => {
    deadline = milliseconds;
    return originalTimer(callback, 1);
  };
  try {
    await assert.rejects(dispatchScheduled(event(), ENV, async (_, { signal }) => (
      new Promise((_, reject) => signal.addEventListener('abort', () => (
        reject(new Error(`aborted secret ${ENV.GITHUB_TOKEN}`))
      )))
    )), error => {
      assert.equal(error.code, 'GITHUB_TIMEOUT');
      assert.ok(!String(error.stack).includes(ENV.GITHUB_TOKEN));
      return true;
    });
    assert.equal(deadline, 20_000);
  } finally {
    globalThis.setTimeout = originalTimer;
  }
});

test('bad run-list JSON stops dispatch without exposing body', async () => {
  await assert.rejects(dispatchScheduled(event(), ENV,
    async () => new Response(`invalid ${ENV.GITHUB_TOKEN}`, { status: 200 })), error => {
    assert.equal(error.code, 'GITHUB_RESPONSE_INVALID');
    assert.ok(!String(error.stack).includes(ENV.GITHUB_TOKEN));
    return true;
  });
  await assert.rejects(dispatchScheduled(event(), ENV, async () => json({ unexpected: true })),
    /Invalid GitHub response format/);
});

test('missing secret and invalid repository are rejected without HTTP requests', async () => {
  let calls = 0;
  const fetch = async () => { calls += 1; return emptyRuns(); };
  await assert.rejects(dispatchScheduled(event(), { ...ENV, GITHUB_TOKEN: '' }, fetch), /secret is required/);
  await assert.rejects(dispatchScheduled(event(), { ...ENV, GITHUB_REPOSITORY: 'https://outside.invalid' }, fetch),
    /Invalid scheduler configuration/);
  assert.equal(calls, 0);
});

test('unknown cron, invalid epoch, and mismatched schedule timestamps fail closed', () => {
  for (const invalid of [event('bad cron'), { ...event(), scheduledTime: NaN },
    { ...event(), scheduledTime: 9e20 },
    event('7 3 * * *', '2026-10-01T09:07:00Z')]) {
    assert.throws(() => planPublication(invalid), SchedulerError);
  }
});

test('subminute scheduled timestamps are normalized to the KST publication boundary', () => {
  assert.equal(planPublication(event('7 3 * * *', '2026-10-01T03:07:01.123Z')).publishAt,
    '2026-10-01T13:00:00+09:00');
});

test('worker has only a scheduled handler and configuration has no public URLs', async () => {
  assert.deepEqual(Object.keys(worker), ['scheduled']);
  const content = await readFile(new URL('./wrangler.jsonc', import.meta.url), 'utf8');
  assert.match(content, /"workers_dev": false/);
  assert.match(content, /"preview_urls": false/);
  assert.ok(!content.includes(ENV.GITHUB_TOKEN));
  for (const cron of Object.keys(CRON_SLOTS)) assert.ok(content.includes(`"${cron}"`));
});

test('scheduled handler logs only sanitized failure messages', async () => {
  const originalFetch = globalThis.fetch;
  const originalError = console.error;
  const messages = [];
  globalThis.fetch = async () => { throw new Error(`private transport ${ENV.GITHUB_TOKEN}`); };
  console.error = message => messages.push(message);
  try {
    await assert.rejects(worker.scheduled(event(), ENV), /GitHub request failed/);
    assert.deepEqual(messages, ['News scheduler failed: GitHub request failed']);
    assert.ok(!messages.join('\n').includes(ENV.GITHUB_TOKEN));
  } finally {
    globalThis.fetch = originalFetch;
    console.error = originalError;
  }
});
