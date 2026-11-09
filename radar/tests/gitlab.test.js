import assert from 'node:assert/strict';
import { afterEach, describe, it, mock } from 'node:test';

import {
  GitLabClient,
  GitLabError,
  fetchDefaultBranchPipelines,
  fetchFirstUniqueCommit,
  fetchMergeRequestStates,
  fetchMergeRequestVerdicts,
  fetchPipelineFailures,
  fetchTokenInfo,
  reviewVerdictOf,
} from '../src/gitlab.js';
import { FakeClient, partialGraphql } from './helpers.js';

const BASE_URL = 'https://gitlab.example.com';

function jsonResponse(body, { status = 200, headers = {} } = {}) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', ...headers } });
}

describe('GitLabClient', () => {
  afterEach(() => mock.restoreAll());

  it('follows pagination and sends the token only in the header', async () => {
    const fetchMock = mock.method(globalThis, 'fetch', async (url, init) => {
      const page = new URL(url).searchParams.get('page');
      return jsonResponse([page], { headers: page === '1' ? { 'x-next-page': '2' } : {} });
    });

    const items = await new GitLabClient(BASE_URL, 'secret').getAll('/projects');

    assert.deepEqual(items, ['1', '2']);
    const [url, init] = fetchMock.mock.calls[0].arguments;
    assert.ok(!String(url).includes('secret'));
    assert.equal(init.headers['PRIVATE-TOKEN'], 'secret');
    assert.equal(init.credentials, 'omit');
    assert.equal(init.redirect, 'error');
  });

  it('retries after 429', async () => {
    let attempt = 0;
    mock.method(globalThis, 'fetch', async () =>
      attempt++ === 0 ? jsonResponse({}, { status: 429, headers: { 'retry-after': '0.01' } }) : jsonResponse({ id: 1 }),
    );

    const client = new GitLabClient(BASE_URL, 't');
    assert.deepEqual(await client.get('/user'), { id: 1 });
    assert.equal(attempt, 2);
    assert.equal(client.requestCount, 2);
  });

  it('throws GitLabError with status and path', async () => {
    mock.method(globalThis, 'fetch', async () => jsonResponse({}, { status: 401 }));
    await assert.rejects(new GitLabClient(BASE_URL, 't').get('/user'), (error) => {
      assert.ok(error instanceof GitLabError);
      assert.equal(error.status, 401);
      assert.equal(error.path, '/user');
      return true;
    });
  });

  it('turns GraphQL errors into GitLabError', async () => {
    mock.method(globalThis, 'fetch', async () => jsonResponse({ errors: [{ message: 'boom' }] }));
    await assert.rejects(new GitLabClient(BASE_URL, 't').graphql('{ x }'), /GraphQL: boom/);
  });

  it('returns partial GraphQL data together with errors', async () => {
    const body = { data: { p0: null, p1: { ok: true } }, errors: [{ message: 'boom', path: ['p0'] }] };
    mock.method(globalThis, 'fetch', async () => jsonResponse(body));
    const client = new GitLabClient(BASE_URL, 't');
    assert.deepEqual(await client.graphqlPartial('{ x }'), body);
    await assert.rejects(client.graphql('{ x }'), /GraphQL: boom/);
  });
});

describe('fetchPipelineFailures', () => {
  const pipeline = { projectPath: 'group/project', id: 7, sha: 'abc' };
  const root = '/projects/group%2Fproject';

  it('collects blocking jobs, downstream bridges and external statuses without duplicates', async () => {
    const client = new FakeClient({
      [`${root}/pipelines/7/jobs`]: [
        { id: 1, name: 'unit', stage: 'test', failure_reason: 'script_failure', web_url: 'j1' },
        { id: 2, name: 'lint', stage: 'test', allow_failure: true },
      ],
      [`${root}/pipelines/7/bridges`]: [
        { id: 3, name: 'deploy', stage: 'deploy', web_url: 'b3', downstream_pipeline: { web_url: 'd3' } },
      ],
      [`${root}/repository/commits/abc/statuses`]: [
        { id: 1, name: 'unit', status: 'failed' },
        { id: 4, name: 'jenkins', status: 'failed', description: 'red', target_url: 'https://ci/4' },
        { id: 5, name: 'ok', status: 'success' },
      ],
    });

    const failures = await fetchPipelineFailures(client, pipeline);

    assert.deepEqual(
      failures.map(({ name, kind, webUrl }) => [name, kind, webUrl]),
      [
        ['unit', 'job', 'j1'],
        ['deploy', 'downstream', 'd3'],
        ['jenkins', 'external', 'https://ci/4'],
      ],
    );
  });

  it('tolerates unavailable commit statuses', async () => {
    const client = new FakeClient({ [`${root}/pipelines/7/jobs`]: [], [`${root}/pipelines/7/bridges`]: [] });
    assert.deepEqual(await fetchPipelineFailures(client, pipeline), []);
  });
});

describe('fetchMergeRequestStates', () => {
  it('maps approvers and thread counters by iid', async () => {
    let variables = null;
    const client = new FakeClient({}, {
      graphql: (query, vars) => {
        variables = vars;
        return {
          project: {
            mergeRequests: {
              nodes: [
                {
                  iid: '7',
                  userNotesCount: 4,
                  resolvableDiscussionsCount: 3,
                  resolvedDiscussionsCount: 1,
                  approvedBy: { nodes: [{ id: 'gid://gitlab/User/42', name: 'Alice', username: 'alice' }] },
                },
              ],
            },
          },
        };
      },
    });

    const states = await fetchMergeRequestStates(client, 'group/project', [7, 8]);

    assert.deepEqual(variables, { fullPath: 'group/project', iids: ['7', '8'] });
    assert.deepEqual(states.get(7), {
      approverIds: [42],
      approverNames: ['Alice'],
      userNotesCount: 4,
      unresolvedThreads: 2,
      resolvedThreads: 1,
    });
    assert.equal(states.has(8), false);
  });

  it('reads review states of reviewers only when asked', async () => {
    const queries = [];
    const node = {
      iid: '7',
      userNotesCount: 0,
      resolvableDiscussionsCount: 0,
      resolvedDiscussionsCount: 0,
      approvedBy: { nodes: [] },
      reviewers: {
        nodes: [
          { id: 'gid://gitlab/User/1', mergeRequestInteraction: { reviewState: 'REQUESTED_CHANGES' } },
          { id: 'gid://gitlab/User/2', mergeRequestInteraction: null },
        ],
      },
    };
    const client = new FakeClient({}, {
      graphql: (query) => {
        queries.push(query);
        return { project: { mergeRequests: { nodes: [node] } } };
      },
    });

    const states = await fetchMergeRequestStates(client, 'group/project', [7], { withReviewState: true });
    await fetchMergeRequestStates(client, 'group/project', [7]);

    assert.deepEqual(states.get(7).reviewStates, { 1: 'requested_changes' });
    assert.ok(queries[0].includes('reviewState'));
    assert.ok(!queries[1].includes('reviewState'));
  });

  it('returns no states for an inaccessible project', async () => {
    const client = new FakeClient({}, { graphql: () => ({ project: null }) });
    assert.equal((await fetchMergeRequestStates(client, 'group/project', [1])).size, 0);
  });
});

describe('fetchDefaultBranchPipelines', () => {
  it('takes the latest conclusive pipeline, passing paths and branches only as variables', async () => {
    let request = null;
    const client = new FakeClient({}, {
      graphql: (query, variables) => {
        request = { query, variables };
        return {
          p0: {
            pipelines: {
              nodes: [
                { status: 'CANCELED', path: '/a/b/-/pipelines/4', finishedAt: 'u' },
                { status: 'SKIPPED', path: '/a/b/-/pipelines/5', finishedAt: 'v' },
                { status: 'FAILED', path: '/a/b/-/pipelines/3', finishedAt: 't' },
              ],
            },
          },
          p1: { pipelines: { nodes: [{ status: 'CANCELED', path: '/a/c/-/pipelines/6', finishedAt: 'w' }] } },
        };
      },
    });

    const result = await fetchDefaultBranchPipelines(client, [
      { path_with_namespace: 'a/b', default_branch: 'main' },
      { path_with_namespace: 'a/"c', default_branch: 'dev' },
    ]);

    assert.deepEqual(request.variables, { path0: 'a/b', ref0: 'main', path1: 'a/"c', ref1: 'dev' });
    assert.ok(!request.query.includes('a/b') && !request.query.includes('main'));
    assert.deepEqual(result, [
      { status: 'failed', webUrl: 'https://gitlab.example.com/a/b/-/pipelines/3', finishedAt: 't' },
      null,
    ]);
  });
});

describe('fetchDefaultBranchPipelines with partial errors', () => {
  const projects = [
    { path_with_namespace: 'a/b', default_branch: 'main' },
    { path_with_namespace: 'a/c', default_branch: 'main' },
  ];
  const failed = { pipelines: { nodes: [{ status: 'FAILED', path: '/a/c/-/pipelines/1', finishedAt: 't' }] } };

  it('keeps projects that resolved and marks failed aliases as unknown', async () => {
    const client = new FakeClient({}, {
      graphql: () => partialGraphql({ p0: null, p1: failed }, [{ message: 'boom', path: ['p0', 'pipelines'] }]),
    });
    assert.deepEqual(await fetchDefaultBranchPipelines(client, projects), [
      undefined,
      { status: 'failed', webUrl: 'https://gitlab.example.com/a/c/-/pipelines/1', finishedAt: 't' },
    ]);
  });

  it('treats an error without a path as unknown for every project', async () => {
    const client = new FakeClient({}, { graphql: () => partialGraphql({ p0: null, p1: failed }, [{ message: 'boom' }]) });
    assert.deepEqual(await fetchDefaultBranchPipelines(client, projects), [undefined, undefined]);
  });
});

describe('fetchFirstUniqueCommit', () => {
  it('returns the oldest commit unique to the branch and passes refs as params', async () => {
    const calls = [];
    const oldest = { id: 'a1', author_name: 'Me Myself' };
    const client = {
      get: async (path, params) => {
        calls.push({ path, params });
        return { commits: [oldest, { id: 'c2' }, { id: 'c3' }] };
      },
    };

    const commit = await fetchFirstUniqueCommit(client, 10, 'main', 'task/multi-scanner-mode');

    assert.equal(commit, oldest);
    assert.deepEqual(calls, [
      {
        path: '/projects/10/repository/compare',
        params: { from: 'main', to: 'task/multi-scanner-mode', straight: false },
      },
    ]);
  });

  it('returns null when the branch has no unique commits', async () => {
    const client = { get: async () => ({ commits: [] }) };
    assert.equal(await fetchFirstUniqueCommit(client, 10, 'main', 'feature'), null);
  });

  it('tolerates a missing commits array', async () => {
    const client = { get: async () => ({}) };
    assert.equal(await fetchFirstUniqueCommit(client, 10, 'main', 'feature'), null);
  });

  it('propagates client errors', async () => {
    const client = { get: async () => { throw new GitLabError(500, 'boom', '/compare'); } };
    await assert.rejects(fetchFirstUniqueCommit(client, 10, 'main', 'feature'));
  });
});

describe('fetchTokenInfo', () => {
  it('reads expiry and scopes of the current token', async () => {
    const client = new FakeClient({ '/personal_access_tokens/self': { expires_at: null, scopes: ['read_api'] } });
    assert.deepEqual(await fetchTokenInfo(client), { expiresAt: null, scopes: ['read_api'] });
  });
});

describe('reviewVerdictOf', () => {
  const me = { id: 1, username: 'me' };
  const myChanges = (createdAt, username = 'me') => ({
    system: true,
    body: 'requested changes',
    createdAt,
    author: { username },
  });
  const reviewRequest = (createdAt, body = 'requested review from @me', username = 'alice') => ({
    system: true,
    body,
    createdAt,
    author: { username },
  });

  it('finds my requested-changes note', () => {
    assert.equal(reviewVerdictOf([reviewRequest('2026-01-01T00:00:00Z'), myChanges('2026-01-02T00:00:00Z')], me), 'requested_changes');
  });

  it('ignores verdict notes of other users and non-system notes', () => {
    assert.equal(reviewVerdictOf([myChanges('2026-01-02T00:00:00Z', 'alice')], me), null);
    assert.equal(reviewVerdictOf([{ system: false, body: 'requested changes', createdAt: 't', author: { username: 'me' } }], me), null);
  });

  it('resets my verdict when a review is requested from me afterwards', () => {
    const notes = [myChanges('2026-01-02T00:00:00Z'), reviewRequest('2026-01-03T00:00:00Z')];
    assert.equal(reviewVerdictOf(notes, me), null);
  });

  it('keeps my verdict when the review request predates it or names someone else', () => {
    const notes = [reviewRequest('2026-01-01T00:00:00Z'), myChanges('2026-01-02T00:00:00Z')];
    assert.equal(reviewVerdictOf(notes, me), 'requested_changes');
    const otherReviewee = [myChanges('2026-01-02T00:00:00Z'), reviewRequest('2026-01-03T00:00:00Z', 'requested review from @bob')];
    assert.equal(reviewVerdictOf(otherReviewee, me), 'requested_changes');
  });

  it('handles multi-reviewer requests and my own re-reviews by the latest note', () => {
    const multi = [
      myChanges('2026-01-02T00:00:00Z'),
      reviewRequest('2026-01-03T00:00:00Z', 'requested review from @bob and @me'),
    ];
    assert.equal(reviewVerdictOf(multi, me), null);
    const reReviewed = [...multi, myChanges('2026-01-04T00:00:00Z')];
    assert.equal(reviewVerdictOf(reReviewed, me), 'requested_changes');
  });

  it('does not reset on a mention of another user with a name-prefix username', () => {
    const meLike = { id: 1, username: 'artem' };
    const notes = [
      { system: true, body: 'requested changes', createdAt: '2026-01-02T00:00:00Z', author: { username: 'artem' } },
      reviewRequest('2026-01-03T00:00:00Z', 'requested review from @artem2', 'alice'),
    ];
    assert.equal(reviewVerdictOf(notes, meLike), 'requested_changes');
  });

  it('resets for dotted usernames and ignores trailing punctuation after a mention', () => {
    const dotted = { id: 1, username: 'a.slinkin' };
    const changes = { system: true, body: 'requested changes', createdAt: '2026-01-02T00:00:00Z', author: { username: 'a.slinkin' } };
    assert.equal(reviewVerdictOf([changes, reviewRequest('2026-01-03T00:00:00Z', 'requested review from @a.slinkin.')], dotted), null);
    assert.equal(reviewVerdictOf([myChanges('2026-01-02T00:00:00Z'), reviewRequest('2026-01-03T00:00:00Z', 'requested review from @me.')], me), null);
  });

  it('matches verdict bodies case-insensitively and sorts unsorted notes', () => {
    const unsorted = [
      { system: true, body: 'Requested Changes', createdAt: '2026-01-02T00:00:00Z', author: { username: 'Me' } },
      reviewRequest('2026-01-01T00:00:00Z'),
    ];
    assert.equal(reviewVerdictOf(unsorted, me), 'requested_changes');
  });
});

describe('fetchMergeRequestVerdicts', () => {
  it('maps activity notes per merge request and skips incomplete nodes', async () => {
    const graphql = (query, variables) => {
      assert.ok(query.includes('MergeRequestVerdicts'));
      assert.ok(query.includes('ONLY_ACTIVITY'));
      assert.deepEqual(variables, { fullPath: 'group/project', iids: ['1', '2'] });
      return {
        project: {
          mergeRequests: {
            nodes: [
              { iid: '1', notes: { nodes: [{ system: true, body: 'requested changes', author: { username: 'me' } }] } },
              { iid: '2', notes: null },
            ],
          },
        },
      };
    };
    const client = new FakeClient({}, { graphql });

    const verdicts = await fetchMergeRequestVerdicts(client, 'group/project', [1, 2]);

    assert.equal(verdicts.size, 1);
    assert.deepEqual(verdicts.get(1), [{ system: true, body: 'requested changes', author: { username: 'me' } }]);
  });
});
