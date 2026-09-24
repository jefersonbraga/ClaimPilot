/* Browser regression checks. Requires Playwright in the test environment only.
 * Run: node --test tests/test_ui_browser.cjs
 * Optional: PLAYWRIGHT_CHROMIUM_EXECUTABLE, PYTHON (default: .venv/bin/python).
 * Starts its own mock-provider server with an isolated temporary audit database.
 */
const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { mkdtempSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join, resolve } = require('node:path');
const { chromium } = require('playwright');
let browser, server, base, temp;
const errors = [];

before(async () => {
  temp = mkdtempSync(join(tmpdir(), 'claimpilot-browser-'));
  server = spawn(process.env.PYTHON || '.venv/bin/python', ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '0'], {
    cwd: resolve(__dirname, '..'),
    env: { ...process.env, LLM_PROVIDER: 'mock', AUDIT_DB_PATH: join(temp, 'audit.db'), RATE_LIMIT_PER_MINUTE: '1000', DAILY_MAX_ANALYSES: '10000' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('Mock server did not start')), 15000);
    server.once('error', reject);
    server.stderr.on('data', chunk => {
      const match = chunk.toString().match(/Uvicorn running on (http:\/\/127\.0\.0\.1:\d+)/);
      if (match) { base = match[1]; clearTimeout(timeout); resolve(); }
    });
  });
  browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}) });
});
after(async () => {
  await browser?.close();
  if (server && server.exitCode === null) {
    await new Promise(resolve => { server.once('exit', resolve); server.kill('SIGTERM'); });
  }
  if (temp) rmSync(temp, { recursive: true, force: true });
  assert.deepEqual(errors, [], 'No uncaught browser errors');
});
async function page(t, options = {}) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, ...options });
  t.after(() => context.close());
  const p = await context.newPage();
  p.on('pageerror', e => errors.push(e.message));
  await p.goto(base);
  await p.waitForSelector('.scenario[aria-checked="true"]');
  return p;
}
async function analyze(p) {
  await p.locator('#analyze').click();
  await p.waitForFunction(() => !document.querySelector('#analyze').disabled);
}
const status = (p, key) => p.locator(`[data-step="${key}"] .node-state`).textContent();

test('All scenarios show the recorded branch, including conditional retrieval', async t => {
  const p = await page(t);
  const cases = {
    'ambiguous-emergency': 'Human review', 'emergency-confirmed': 'Approve', 'obvious-approval': 'Approve',
    'missing-authorization': 'Deny', 'policy-conflict': 'Human review', 'possible-duplicate': 'Human review',
    'high-value': 'Human review', incomplete: 'Human review',
  };
  for (const [id, verdict] of Object.entries(cases)) {
    await p.locator(`.scenario[data-id="${id}"]`).click();
    await analyze(p);
    assert.match(await p.locator('#verdict').textContent(), new RegExp(verdict));
    assert.match(await status(p, verdict === 'Human review' ? 'review' : 'decide'), /Executed/);
    assert.match(await status(p, verdict === 'Human review' ? 'decide' : 'review'), /Not taken/);
    assert.match(await status(p, 'audit'), /Recorded/);
    if (id === 'obvious-approval') assert.match(await status(p, 'refine'), /Skipped/);
    if (id === 'possible-duplicate') {
      assert.match(await status(p, 'refine'), /Executed/);
      await p.locator('[data-step="refine"] button').click();
      assert.match(await p.locator('#flow-detail').textContent(), /POL-DUP-006/);
    }
    if (id === 'emergency-confirmed') assert.equal(await p.locator('#compare').isVisible(), true);
  }
});

test('Missing identifiers bypass investigation and preserve the audit path', async t => {
  const p = await page(t);
  await p.locator('#claim-editor summary').click();
  const claim = JSON.parse(await p.locator('#claim-json').inputValue());
  delete claim.member_id;
  await p.locator('#claim-json').fill(JSON.stringify(claim));
  await analyze(p);
  for (const key of ['retrieve', 'tools', 'refine', 'llm', 'risk', 'decide']) assert.match(await status(p, key), /Not taken/);
  assert.match(await status(p, 'review'), /Executed/);
  assert.match(await status(p, 'audit'), /Recorded/);
  assert.equal(await p.locator('.flow-edge.early.visited').count(), 1);
});

test('Replay can pause and restart, and edits reset the diagram without hiding prior results', async t => {
  const p = await page(t);
  await analyze(p);
  await p.locator('#flow-play').click();
  await p.waitForSelector('.replay-current');
  await p.locator('#flow-play').click();
  assert.match(await p.locator('#flow-status').textContent(), /paused/);
  const current = await p.locator('.replay-current').getAttribute('data-step');
  await p.waitForTimeout(1200);
  assert.equal(await p.locator('.replay-current').getAttribute('data-step'), current);
  await p.locator('#flow-restart').click();
  assert.equal(await p.locator('.replay-current').getAttribute('data-step'), 'validate');
  await p.locator('.scenario[data-id="emergency-confirmed"]').click();
  assert.match(await p.locator('#flow-status').textContent(), /Conceptual/);
  assert.equal(await p.locator('#flow-play').isDisabled(), true);
  assert.match(await p.locator('#result-context').textContent(), /Previous execution/);
  assert.equal(await p.locator('#results').isVisible(), true);
  assert.equal(await p.locator('.replay-current').count(), 0);
});

test('Replay failure keeps recommendation and retry performs no second analysis', async t => {
  const p = await page(t);
  let posts = 0;
  p.on('request', r => { if (r.method() === 'POST' && r.url().endsWith('/claims/analyze')) posts++; });
  await p.route('**/executions/*', r => r.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"Replay unavailable"}' }));
  await analyze(p);
  assert.equal(await p.locator('#decision').isVisible(), true);
  assert.equal(await p.locator('#replay-unavailable').isVisible(), true);
  assert.equal(await p.locator('#replay-btn').isDisabled(), true);
  await p.unroute('**/executions/*');
  await p.locator('#retry-replay').click();
  await p.waitForFunction(() => document.querySelector('#replay-unavailable').hidden);
  assert.equal(posts, 1);
  assert.match(await status(p, 'audit'), /Recorded/);
  await p.locator('#replay-btn').click();
  assert.equal(await p.locator('#replay').isVisible(), true);
});

test('Only one in-flight analysis; changing inputs during the request never marks them executed', async t => {
  const p = await page(t);
  let release, posts = 0;
  const gate = new Promise(resolve => { release = resolve; });
  await p.route('**/claims/analyze', async route => { posts++; await gate; await route.continue(); });
  await p.locator('#analyze').click();
  await p.waitForFunction(() => document.querySelector('#analyze').disabled);
  await p.keyboard.press('Control+Enter');
  assert.match(await p.locator('#flow-status').textContent(), /^Investigating/);
  assert.equal(await p.locator('#flow .visited').count(), 0);
  await p.locator('.scenario[data-id="emergency-confirmed"]').click();
  release();
  await p.waitForFunction(() => !document.querySelector('#analyze').disabled);
  assert.equal(posts, 1);
  assert.match(await p.locator('#flow-status').textContent(), /Conceptual/);
  assert.match(await p.locator('#result-context').textContent(), /Previous execution/);
  assert.match(await p.locator('#verdict').textContent(), /Human review/);
});

test('Invalid JSON, validation, rate limiting and server failure retain recoverable UI', async t => {
  const p = await page(t);
  await p.locator('#claim-editor summary').click();
  await p.locator('#claim-json').fill('{');
  await p.locator('#analyze').click();
  assert.match(await p.locator('#error').textContent(), /invalid/);
  await p.locator('#reset-claim').click();
  for (const code of [422, 429, 500]) {
    const detail = code === 422 ? [{loc:['body','amount'], msg:'Invalid amount'}] : 'Test failure';
    await p.route('**/claims/analyze', r => r.fulfill({ status: code, contentType:'application/json', body: JSON.stringify({detail}) }));
    await analyze(p);
    assert.equal(await p.locator('#error').isVisible(), true);
    assert.equal(await p.locator('#analyze').isEnabled(), true);
    await p.unroute('**/claims/analyze');
  }
});

test('Responsive layout, keyboard selection and manual reduced-motion replay', async t => {
  const p = await page(t, { reducedMotion: 'reduce' });
  assert.equal(await p.locator('#claim-editor').getAttribute('open'), null);
  await p.locator('[data-step="llm"] button').focus();
  await p.keyboard.press('Enter');
  assert.match(await p.locator('#flow-detail').textContent(), /One structured model call/);
  await analyze(p);
  await p.locator('#flow-play').click();
  assert.match(await p.locator('#flow-status').textContent(), /manual, reduced motion/);
  const current = await p.locator('.replay-current').getAttribute('data-step');
  await p.waitForTimeout(1200);
  assert.equal(await p.locator('.replay-current').getAttribute('data-step'), current);
  await p.locator('#flow-play').click();
  assert.notEqual(await p.locator('.replay-current').getAttribute('data-step'), current);
  for (const width of [1440, 1024, 768, 390, 320]) {
    await p.setViewportSize({ width, height: 900 });
    assert.equal(await p.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `No overflow at ${width}px`);
    assert.equal(await p.locator('#flow-edges').isVisible(), width > 760);
  }
});

test('Live progress follows the streamed workflow events while an analysis runs', async t => {
  const p = await page(t);
  await p.locator('.scenario[data-id="ambiguous-emergency"]').click();
  const seen = [];
  await p.locator('#analyze').click();
  while (await p.locator('#analyze').isDisabled()) {
    const running = await p.locator('#flow .live-running').evaluateAll(ns => ns.map(n => n.dataset.step));
    for (const s of running) if (seen[seen.length - 1] !== s) seen.push(s);
    if (running.length) assert.match(await p.locator('#flow-status').textContent(), /^Investigating · live/);
    await p.waitForTimeout(40);
  }
  // Order is the real node order; the escalation branch is taken, so "decide" never runs.
  assert.deepEqual(seen.filter(s => ['retrieve', 'tools', 'refine', 'llm', 'risk', 'audit'].includes(s)),
                   ['retrieve', 'tools', 'refine', 'llm', 'risk', 'audit']);
  assert.ok(!seen.includes('decide'));
  assert.equal(await p.locator('#flow .live-running, #flow .live-pending').count(), 0);
  assert.match(await status(p, 'review'), /Executed/);
});
