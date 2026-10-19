#!/usr/bin/env node
// Per-item links (chat / issues / related) in refresh.js.
//
// The Python suite (queue-minisite/test_item_links.py) pins the server
// render and the /api/queue payload. This file pins the SPA half that
// replaces it on the first 5s tick:
//
//   - linksRow() / linksList() render Chat, Issues and a folded Links list
//     with target=_blank rel=noopener on every anchor, in every section;
//   - items without links render no links markup;
//   - a morphdom merge keeps an open Links disclosure open.
//
// Usage:   node links.test.js
// Exit 0 on success, 1 on any failure.

'use strict';

const path = require('path');
const fs = require('fs');

const NODE_MODULES = process.env.QM_NODE_MODULES ||
  '/tmp/queue-minisite-test/node_modules';
const { JSDOM } = require(path.join(NODE_MODULES, 'jsdom'));

const STATIC_DIR = path.dirname(path.resolve(__filename));
const morphdomSrc = fs.readFileSync(
  path.join(STATIC_DIR, 'vendor', 'morphdom-2.7.4.min.js'), 'utf8');
const refreshSrc = fs.readFileSync(path.join(STATIC_DIR, 'refresh.js'), 'utf8');

const A = 'target="_blank" rel="noopener noreferrer"';
const CHAT = 'https://chat.example/#/room/!abc:example.org';
const ISSUE = 'https://git.example/org/foo/issues/12';
const PR = 'https://git.example/org/foo/pulls/34';

const dom = new JSDOM(`<!doctype html><html><head></head><body>
  <div class="meta" id="topbar-meta"></div>
  <main id="queue-root"></main>
</body></html>`, { runScripts: 'dangerously', url: 'https://queue.test/' });
for (const src of [morphdomSrc, refreshSrc]) {
  const s = dom.window.document.createElement('script');
  s.textContent = src;
  dom.window.document.head.appendChild(s);
}
const { document } = dom.window;
const refresh = dom.window.__queueRefresh;

let failures = 0;
function assert(label, cond, detail) {
  if (cond) {
    console.log(`PASS  ${label}`);
  } else {
    failures++;
    console.error(`FAIL  ${label}${detail ? `: ${detail}` : ''}`);
  }
}

if (!refresh || typeof refresh.linksRow !== 'function') {
  console.error('FAIL: __queueRefresh.linksRow not exposed');
  process.exit(1);
}

// --- 1. cards ---------------------------------------------------------------
const LINKS = {
  chat: CHAT,
  issues: [{ url: ISSUE, label: null, text: 'org/foo#12' }],
  related: [
    { url: PR, label: null, kind: 'pr', text: 'org/foo#34' },
    { url: 'https://grafana.example/d/x', label: 'board', kind: null, text: 'board' },
  ],
};
const EMPTY = { chat: null, issues: [], related: [] };
function base(id, status, extra) {
  return Object.assign({
    id, status, summary: `summary ${id}`, description: '', scope: [],
    group_head: false, priority: 5, created_by: '', depends_on: [],
    age: '1m ago', links: EMPTY,
  }, extra || {});
}
const state = {
  totals: { running: 1, pending: 2, blocked: 1, wedged: 1, done: 1, abandoned: 0 },
  running: [base('q-run', 'running', {
    links: LINKS, description: `ship ${PR} now`,
    owner: { mode: 'agent', alive: true, agent_id: 'agent-x', jsonl_age: '1s' },
  })],
  wedged: [base('q-wdg', 'wedged', { links: { chat: CHAT, issues: [], related: [] } })],
  quarantined: [],
  pending: [base('q-pnd', 'pending', { links: LINKS }), base('q-old', 'pending', { links: undefined })],
  blocked: [base('q-blk', 'blocked', {
    links: { chat: null, issues: LINKS.issues, related: [] },
    block_reason: `waiting on ${ISSUE}, then <merge>`,
  })],
  other: [],
  done_recent: [base('q-don', 'done', { links: { chat: null, issues: [], related: LINKS.related } })],
  abandoned_recent: [],
};

const root = refresh.buildQueueDOM(state);
const card = (id) => root.querySelector(`article[data-queue-id="${id}"]`);

{
  const c = card('q-run');
  const chat = c.querySelector('.item-links a.chat-link');
  assert('running: Chat link', chat && chat.getAttribute('href') === CHAT && chat.textContent === 'Chat');
  const issue = c.querySelector('.links-issues a.issue-link');
  assert('running: issue link text', issue && issue.textContent === 'org/foo#12');
  const sum = c.querySelector('details.links-toggle > summary');
  assert('running: Links (2) disclosure', sum && sum.textContent === 'Links (2)');
  const kinds = Array.from(c.querySelectorAll('.link-list .link-kind')).map((e) => e.textContent);
  assert('running: kind badge only where kind is set', kinds.join(',') === 'pr');
  const all = Array.from(c.querySelectorAll('a.qlink'));
  assert('running: every link opens in a new tab with noopener',
    all.length === 4 && all.every((a) => a.getAttribute('target') === '_blank' &&
      /\bnoopener\b/.test(a.getAttribute('rel'))), `n=${all.length}`);
  const order = Array.from(c.children).map((e) => e.className);
  assert('running: links row before age, list before prompt',
    order.indexOf('item-links') < order.indexOf('age') &&
    order.indexOf('prompt-toggle links-toggle') < order.indexOf('prompt-toggle'),
    order.join(' | '));
}
{
  const c = card('q-blk');
  assert('blocked: issues row without chat', !c.querySelector('.chat-link') && c.querySelector('.issue-link'));
}
assert('wedged: chat link', !!card('q-wdg').querySelector('.chat-link'));
assert('pending: links render', !!card('q-pnd').querySelector('.links-toggle'));
assert('done: related-only list, no row',
  !!card('q-don').querySelector('.links-toggle') && !card('q-don').querySelector('.item-links'));
assert('legacy item (no links key): no links markup',
  !card('q-old').querySelector('.item-links, .links-toggle'));

// --- 2. merge keeps an open Links disclosure open --------------------------
refresh.mergeQueueRoot(state);
const live = document.querySelector('article[data-queue-id="q-run"] details.links-toggle');
live.open = true;
refresh.mergeQueueRoot(state);
assert('merge: open Links disclosure survives a tick',
  document.querySelector('article[data-queue-id="q-run"] details.links-toggle').open === true);

if (failures) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log('\nall links tests passed');
process.exit(0);
