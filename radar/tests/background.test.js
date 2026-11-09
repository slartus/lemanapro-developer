import assert from 'node:assert/strict';
import { afterEach, describe, it, mock } from 'node:test';

import { createChrome, loadBackground, sendMessage } from './chrome.js';

const BASE_URL = 'https://gitlab.example.com';
const ORIGIN = `${BASE_URL}/*`;
const ME = { id: 1, username: 'me' };
const ALICE = { id: 2, username: 'alice' };

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const restMr = (iid, author = ALICE) => ({
  project_id: 10,
  iid,
  title: `MR ${iid}`,
  web_url: `${BASE_URL}/group/project/-/merge_requests/${iid}`,
  references: { full: `group/project!${iid}` },
  author,
  draft: false,
  detailed_merge_status: 'not_approved',
  has_conflicts: false,
  updated_at: '2026-03-10T10:00:00Z',
  user_notes_count: 0,
});

function gitlab(initial) {
  const state = { authored: [], reviewing: [], pipelines: {}, userStatus: 200, ...initial };
  const tokens = [];
  const handle = async (url, init = {}) => {
    const { pathname, searchParams } = new URL(url);
    const token = init.headers?.['PRIVATE-TOKEN'];
    tokens.push(token);
    await state.gate?.(pathname, token);
    if (pathname === '/api/graphql') {
      const { query, variables } = JSON.parse(init.body);
      if (query.includes('AuthoredPipelines')) {
        const nodes = state.authored.map((mr) => ({
          iid: String(mr.iid),
          project: { fullPath: 'group/project' },
          headPipeline: state.pipelines[mr.iid]
            ? { id: 'gid://gitlab/Ci::Pipeline/7', sha: 'abc', status: state.pipelines[mr.iid], path: '/p/7', project: null }
            : null,
        }));
        return json({ data: { currentUser: { authoredMergeRequests: { pageInfo: { hasNextPage: false }, nodes } } } });
      }
      if (query.includes('MergeRequestStates')) {
        const nodes = variables.iids.map((iid) => ({
          iid,
          userNotesCount: 0,
          resolvableDiscussionsCount: 0,
          resolvedDiscussionsCount: 0,
          approvedBy: { nodes: [] },
          reviewers: { nodes: [] },
        }));
        return json({ data: { project: { mergeRequests: { nodes } } } });
      }
      return json({ errors: [{ message: 'unexpected' }] });
    }
    const routes = {
      '/api/v4/user': () => json(state.userStatus === 200 ? { ...ME, username: state.username?.(token) ?? 'me' } : {}, state.userStatus),
      '/api/v4/projects': () => json([]),
      '/api/v4/todos': () => json([]),
      '/api/v4/merge_requests': () =>
        json(searchParams.get('scope') === 'created_by_me' ? state.authored : searchParams.get('reviewer_id') ? state.reviewing : []),
    };
    return (routes[pathname] ?? (() => json({}, 404)))();
  };
  return { state, tokens, handle };
}

async function setup({ settings = {}, permissions = [], ...server } = {}) {
  const chrome = createChrome({ origins: [ORIGIN], permissions });
  await chrome.storage.local.set({ settings: { baseUrl: BASE_URL, token: 'secret', ...settings } });
  const api = gitlab(server);
  mock.method(globalThis, 'fetch', api.handle);
  await loadBackground(chrome);
  const refresh = (message = {}) => sendMessage(chrome, { type: 'refresh', ...message });
  const stored = async (key) => (await chrome.storage.local.get(key))[key];
  return { chrome, api, refresh, stored };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

const clock = (offsetMinutes) => {
  const date = new Date(Date.now() + offsetMinutes * 60_000);
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
};

afterEach(() => {
  mock.restoreAll();
  delete globalThis.chrome;
});

describe('background refresh', () => {
  it('stores the dashboard, shows counts and schedules the next refresh after the interval', async () => {
    const { chrome, api, refresh, stored } = await setup({ reviewing: [restMr(1)], settings: { intervalMinutes: 15 } });

    await refresh();

    const dashboard = await stored('dashboard');
    assert.deepEqual(dashboard.sections.reviewing.map((item) => item.key), ['10!1']);
    assert.equal(dashboard.stats.requests, api.tokens.length);
    assert.equal(chrome.action.badge.text, '1');
    assert.deepEqual(chrome.alarms.all.get('refresh'), { delayInMinutes: 15, periodInMinutes: 15 });
  });

  it('refreshes again in a minute while my pipeline runs, unless turned off', async () => {
    const running = { authored: [restMr(2, ME)], pipelines: { 2: 'RUNNING' }, settings: { intervalMinutes: 15 } };
    const fast = await setup(running);
    await fast.refresh();
    assert.equal(fast.chrome.alarms.all.get('refresh').delayInMinutes, 1);
    mock.restoreAll();

    const off = await setup({ ...running, settings: { intervalMinutes: 15, fastRefreshWhileCi: false } });
    await off.refresh();
    assert.equal(off.chrome.alarms.all.get('refresh').delayInMinutes, 15);
  });

  it('keeps the last dashboard and marks the error when GitLab rejects the token', async () => {
    const { chrome, api, refresh, stored } = await setup({ reviewing: [restMr(1)] });
    await refresh();
    api.state.userStatus = 401;

    await refresh();

    const dashboard = await stored('dashboard');
    assert.match(dashboard.error, /Токен/);
    assert.deepEqual(dashboard.sections.reviewing.map((item) => item.key), ['10!1']);
    assert.equal(chrome.action.badge.text, '!');
  });

  it('reruns with new settings saved during a refresh and drops the stale result', async () => {
    let release;
    const released = new Promise((resolve) => {
      release = resolve;
    });
    const { chrome, refresh, stored } = await setup({
      settings: { token: 'old' },
      username: (token) => `${token}-user`,
      gate: (pathname, token) => (pathname === '/api/v4/user' && token === 'old' ? released : null),
    });
    const written = [];
    chrome.storage.onChanged.addListener((changes) => {
      if (changes.dashboard?.newValue) written.push(changes.dashboard.newValue.username);
    });

    const first = refresh();
    await settle();
    await chrome.storage.local.set({ settings: { ...(await stored('settings')), token: 'new' } });
    const second = refresh({ settingsChanged: true });
    release();
    await Promise.all([first, second]);

    assert.deepEqual(written, ['new-user']);
    assert.equal((await stored('dashboard')).username, 'new-user');
  });
});

describe('background notifications', () => {
  const enabled = (settings = {}) => ({
    permissions: ['notifications'],
    settings: { notifications: true, ...settings },
  });

  it('notifies about a new MR for review', async () => {
    const { chrome, api, refresh } = await setup(enabled());
    await refresh();
    api.state.reviewing = [restMr(1)];

    await refresh();

    assert.equal(chrome.notifications.created.length, 1);
    assert.equal(chrome.notifications.created[0].options.message, 'на ревью: 1');
  });

  it('stays silent about muted event types', async () => {
    const { chrome, api, refresh } = await setup(enabled({ mutedNotificationTypes: ['review'] }));
    await refresh();
    api.state.reviewing = [restMr(1)];

    await refresh();

    assert.equal(chrome.notifications.created.length, 0);
  });

  it('holds events during quiet hours and delivers those still relevant afterwards', async () => {
    const quietHours = { enabled: true, from: clock(-60), to: clock(60), weekends: false };
    const { chrome, api, refresh, stored } = await setup(enabled({ quietHours }));
    await refresh();
    api.state.reviewing = [restMr(1), restMr(2)];
    await refresh();

    assert.equal(chrome.notifications.created.length, 0);
    assert.deepEqual((await stored('pendingNotifications')).events.map(({ item }) => item.key), ['10!1', '10!2']);

    await chrome.storage.local.set({ settings: { ...(await stored('settings')), quietHours: { ...quietHours, enabled: false } } });
    await settle();
    api.state.reviewing = [restMr(2)];
    await refresh();

    assert.equal(chrome.notifications.created.length, 1);
    assert.equal(chrome.notifications.created[0].options.message, 'на ревью: 1');
    assert.equal(await stored('pendingNotifications'), undefined);
  });
});
