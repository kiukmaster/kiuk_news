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
  const originalTimer = globalThis.setTimeout;
  globalThis.setTimeout = (callback, milliseconds, ...args) => originalTimer(
    callback, milliseconds === 1_000 || milliseconds === 3_000 ? 0 : milliseconds, ...args);
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
    globalThis.setTimeout = originalTimer;
    globalThis.fetch = originalFetch;
    console.error = originalError;
  }
});

test('transient GET failures recover on the third attempt before exactly one POST', async () => {
  const methods = [];
  const waits = [];
  const result = await dispatchScheduled(event(), ENV, async (_, options) => {
    methods.push(options.method);
    if (options.method === 'POST') return new Response(null, { status: 204 });
    return methods.length < 3 ? new Response('private upstream body', { status: 520 }) : emptyRuns();
  }, async milliseconds => waits.push(milliseconds));
  assert.equal(result.status, 'dispatched');
  assert.deepEqual(methods, ['GET', 'GET', 'GET', 'POST']);
  assert.deepEqual(waits, [1_000, 3_000]);
});

test('exhausted transient GET retries fail after three attempts without POST or leaked body', async () => {
  const methods = [];
  const waits = [];
  await assert.rejects(dispatchScheduled(event(), ENV, async (_, options) => {
    methods.push(options.method);
    return new Response(`upstream private ${ENV.GITHUB_TOKEN}`, { status: 503 });
  }, async milliseconds => waits.push(milliseconds)), error => {
    assert.equal(error.code, 'GITHUB_HTTP');
    assert.equal(error.status, 503);
    assert.ok(!String(error.stack).includes(ENV.GITHUB_TOKEN));
    return true;
  });
  assert.deepEqual(methods, ['GET', 'GET', 'GET']);
  assert.deepEqual(waits, [1_000, 3_000]);
});

test('429 Retry-After seconds and HTTP dates are bounded to thirty seconds', async () => {
  const originalNow = Date.now;
  Date.now = () => Date.parse('2026-10-03T10:00:00Z');
  try {
    for (const [retryAfter, expectedDelay] of [
      ['9', 9_000], ['600', 30_000],
      ['Sat, 03 Oct 2026 10:00:08 GMT', 8_000],
      ['Saturday, 03-Oct-26 10:00:08 GMT', 8_000],
      ['Sat Oct  3 10:00:08 2026', 8_000],
      ['Sat, 03 Oct 2026 10:02:00 GMT', 30_000],
      ['Sat, 03 Oct 2026 09:59:00 GMT', 1_000],
      ['0', 1_000], ['-1', 1_000], ['1.5', 1_000], ['invalid', 1_000],
    ]) {
      const waits = [];
      let calls = 0;
      const result = await dispatchScheduled(event(), ENV, async () => {
        calls += 1;
        return calls === 1
          ? new Response(null, { status: 429, headers: { 'Retry-After': retryAfter } })
          : json({ workflow_runs: [{ display_title: planPublication(event()).displayTitle }] });
      }, async milliseconds => waits.push(milliseconds));
      assert.equal(result.status, 'duplicate');
      assert.equal(calls, 2);
      assert.deepEqual(waits, [expectedDelay], retryAfter);
    }
  } finally {
    Date.now = originalNow;
  }
});

test('GET transport failures retry but sanitized failures never expose authorization', async () => {
  const methods = [];
  const waits = [];
  const result = await dispatchScheduled(event(), ENV, async (_, options) => {
    methods.push(options.method);
    if (methods.length < 3) throw new Error(`transport private ${ENV.GITHUB_TOKEN}`);
    return json({ workflow_runs: [{ display_title: planPublication(event()).displayTitle }] });
  }, async milliseconds => waits.push(milliseconds));
  assert.equal(result.status, 'duplicate');
  assert.deepEqual(methods, ['GET', 'GET', 'GET']);
  assert.deepEqual(waits, [1_000, 3_000]);
});

test('GET timeout retries use a fresh signal for each attempt and stop after recovery', async () => {
  const originalTimer = globalThis.setTimeout;
  const signals = [];
  const waits = [];
  globalThis.setTimeout = (callback, milliseconds) => originalTimer(
    callback, milliseconds === 20_000 ? 1 : milliseconds);
  try {
    const result = await dispatchScheduled(event(), ENV, async (_, { signal }) => {
      signals.push(signal);
      if (signals.length === 1) {
        return new Promise((_, reject) => signal.addEventListener('abort', () => (
          reject(new Error(`timeout private ${ENV.GITHUB_TOKEN}`))
        )));
      }
      return json({ workflow_runs: [{ display_title: planPublication(event()).displayTitle }] });
    }, async milliseconds => waits.push(milliseconds));
    assert.equal(result.status, 'duplicate');
    assert.equal(signals.length, 2);
    assert.notEqual(signals[0], signals[1]);
    assert.equal(signals[0].aborted, true);
    assert.equal(signals[1].aborted, false);
    assert.deepEqual(waits, [1_000]);
  } finally {
    globalThis.setTimeout = originalTimer;
  }
});

test('GET authorization, not-found, redirect and invalid responses fail without retry', async () => {
  for (const makeResponse of [
    () => new Response(null, { status: 401 }),
    () => new Response(null, { status: 403, headers: { 'Retry-After': '1' } }),
    () => new Response(null, { status: 404 }),
    () => new Response(null, { status: 302 }),
    () => new Response('invalid JSON', { status: 200 }),
    () => json({ unexpected: true }),
  ]) {
    const methods = [];
    const waits = [];
    await assert.rejects(dispatchScheduled(event(), ENV, async (_, options) => {
      methods.push(options.method);
      return makeResponse();
    }, async milliseconds => waits.push(milliseconds)), SchedulerError);
    assert.deepEqual(methods, ['GET']);
    assert.deepEqual(waits, []);
  }
});

test('POST transient HTTP failures or transport failures are never automatically retried', async () => {
  for (const failPost of [
    () => new Response(null, { status: 429, headers: { 'Retry-After': '1' } }),
    () => new Response(null, { status: 520, headers: { 'Retry-After': '1' } }),
    () => { throw new Error(`uncertain POST ${ENV.GITHUB_TOKEN}`); },
  ]) {
    const methods = [];
    const waits = [];
    await assert.rejects(dispatchScheduled(event(), ENV, async (_, options) => {
      methods.push(options.method);
      return options.method === 'GET' ? emptyRuns() : failPost();
    }, async milliseconds => waits.push(milliseconds)), error => {
      assert.ok(error instanceof SchedulerError);
      assert.ok(!String(error.stack).includes(ENV.GITHUB_TOKEN));
      return true;
    });
    assert.deepEqual(methods, ['GET', 'POST']);
    assert.deepEqual(waits, []);
  }
});

test('uncertain POST timeout stops after one POST without a retry delay', async () => {
  const originalTimer = globalThis.setTimeout;
  const methods = [];
  const waits = [];
  globalThis.setTimeout = (callback, milliseconds) => originalTimer(
    callback, milliseconds === 20_000 ? 1 : milliseconds);
  try {
    await assert.rejects(dispatchScheduled(event(), ENV, async (_, options) => {
      methods.push(options.method);
      if (options.method === 'GET') return emptyRuns();
      return new Promise((_, reject) => options.signal.addEventListener('abort', () => (
        reject(new Error(`uncertain timeout ${ENV.GITHUB_TOKEN}`))
      )));
    }, async milliseconds => waits.push(milliseconds)), error => {
      assert.equal(error.code, 'GITHUB_TIMEOUT');
      assert.ok(!String(error.stack).includes(ENV.GITHUB_TOKEN));
      return true;
    });
    assert.deepEqual(methods, ['GET', 'POST']);
    assert.deepEqual(waits, []);
  } finally {
    globalThis.setTimeout = originalTimer;
  }
});
