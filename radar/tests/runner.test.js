import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';

import { GitLabClient, GitLabError } from '../src/gitlab.js';
import { describeError, refreshDashboard } from '../src/runner.mjs';
import { FakeClient, ME, emptyDashboardRoutes, mergeRequestsRoute, mr } from './helpers.js';

const BASE_URL = 'https://gitlab.example.com';
const APPROVALS = { approved_by: [], user_has_approved: false };
const RUNNER = fileURLToPath(new URL('../src/runner.mjs', import.meta.url));

const fakeClient = (routes) => {
  const client = new FakeClient({ ...emptyDashboardRoutes(), ...routes });
  client.requestCount = 7;
  return client;
};

const run = (input, client) =>
  refreshDashboard(
    { baseUrl: BASE_URL, apiBase: 'http://127.0.0.1:8123/gl', token: 'secret', ...input },
    { createClient: () => client, now: () => 1000 },
  );

describe('refreshDashboard', () => {
  it('collects the dashboard with the source and filters attached', async () => {
    const client = fakeClient({
      '/projects': [{ id: 10 }],
      '/merge_requests': mergeRequestsRoute({ authored: [mr({ iid: 2, author: ME })] }),
      '/projects/10/merge_requests/2/approvals': APPROVALS,
    });
    const result = await run({ settings: { ignoredCheckNames: ['b', 'a'], approvalThreshold: 2 } }, client);
    assert.equal(result.ok, true);
    assert.equal(result.dashboard.username, 'me');
    assert.equal(result.dashboard.baseUrl, BASE_URL);
    assert.deepEqual(result.dashboard.filters, { selectedProjectIds: null, ignoredCheckNames: ['a', 'b'], approvalThreshold: 2 });
    assert.deepEqual(result.dashboard.stats, { requests: 7, durationMs: 0 });
    assert.deepEqual(result.dashboard.sections.authored.map((item) => item.key), ['10!2']);
    assert.equal(result.threadCache.baseUrl, BASE_URL);
    assert.equal(result.dashboard.threadCache, undefined);
    assert.equal(typeof result.activePipelines, 'boolean');
  });

  it('builds the client for the proxy while keeping GitLab links', async () => {
    const created = [];
    await refreshDashboard(
      { baseUrl: BASE_URL, apiBase: 'http://127.0.0.1:8123/gl', token: 'secret' },
      {
        createClient: (...args) => {
          created.push(args);
          return fakeClient({});
        },
      },
    );
    assert.deepEqual(created, [[BASE_URL, 'secret', { apiBase: 'http://127.0.0.1:8123/gl' }]]);
    const client = new GitLabClient(BASE_URL, 'secret', { apiBase: 'http://127.0.0.1:8123/gl' });
    assert.equal(client.apiUrl, 'http://127.0.0.1:8123/gl/api/v4');
    assert.equal(client.graphqlUrl, 'http://127.0.0.1:8123/gl/api/graphql');
    assert.equal(client.baseUrl, BASE_URL);
  });

  it('limits watched projects in selected mode and warns when none is left', async () => {
    const client = fakeClient({ '/projects': [{ id: 10 }] });
    const result = await run({ settings: { projectFilterMode: 'selected', selectedProjectIds: [30, 20] } }, client);
    assert.deepEqual(result.dashboard.filters.selectedProjectIds, [20, 30]);
    assert.equal(result.dashboard.watchedProjects, 0);
    assert.match(result.dashboard.warning, /Ни один выбранный проект/);
  });

  it('skips hidden sections and reuses the thread cache of the same instance only', async () => {
    const calls = [];
    const client = fakeClient({ '/projects': [{ id: 10 }] });
    const original = client.resolve.bind(client);
    client.resolve = (path, params) => {
      calls.push(path);
      return original(path, params);
    };
    const result = await run({ settings: { hiddenSections: ['possibleMrs'] }, threadCache: { baseUrl: 'https://other.example.com' } }, client);
    assert.deepEqual(result.dashboard.skippedSections, ['possibleMrs']);
    assert.ok(!calls.some((path) => path.endsWith('/repository/branches')));
  });

  it('keeps the stable merge status of the previous dashboard', async () => {
    const authored = [mr({ iid: 2, author: ME, detailed_merge_status: 'checking' })];
    const client = fakeClient({
      '/merge_requests': mergeRequestsRoute({ authored }),
      '/projects/10/merge_requests/2/approvals': APPROVALS,
    });
    const previous = {
      baseUrl: BASE_URL,
      username: 'me',
      filters: { selectedProjectIds: null, ignoredCheckNames: [], approvalThreshold: 0 },
      sections: { authored: [{ key: '10!2', kind: 'mr', status: 'mergeable' }] },
    };
    const result = await run({ previous }, client);
    assert.equal(result.dashboard.sections.authored[0].status, 'mergeable');
  });

  it('reports failures with a readable message and the status', async () => {
    const client = fakeClient({ '/user': new GitLabError(401, '401 Unauthorized: /user', '/user') });
    assert.deepEqual(await run({}, client), {
      ok: false,
      status: 401,
      error: 'Токен невалиден или истёк — обнови PAT в Keychain',
    });
  });
});

describe('describeError', () => {
  it('explains rate limits, outages and network errors', () => {
    assert.match(describeError(new GitLabError(429, 'x', '/user')), /частоту запросов/);
    assert.match(describeError(new GitLabError(503, 'x', '/user')), /временно недоступен/);
    assert.match(describeError(new TypeError('fetch failed')), /сеть или VPN/);
    assert.equal(describeError(new GitLabError(404, '404 Not Found: /x', '/x')), '404 Not Found: /x');
  });
});

describe('runner process', () => {
  it('reads the input from stdin and prints only the JSON result', () => {
    const input = { baseUrl: BASE_URL, apiBase: 'http://127.0.0.1:9/gl', token: 'secret' };
    const child = spawnSync(process.execPath, [RUNNER], { input: JSON.stringify(input), encoding: 'utf8', timeout: 30_000 });
    assert.equal(child.status, 0, child.stderr);
    const result = JSON.parse(child.stdout);
    assert.equal(result.ok, false);
    assert.match(result.error, /сеть или VPN/);
  });

  it('fails with a non-zero exit code on malformed input', () => {
    const child = spawnSync(process.execPath, [RUNNER], { input: 'not json', encoding: 'utf8', timeout: 30_000 });
    assert.notEqual(child.status, 0);
    assert.equal(child.stdout, '');
  });
});
