import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { collectDashboard, collectMentions, collectPossibleMrs, countThreadsAwaitingReply } from '../src/dashboard.js';
import { GitLabError } from '../src/gitlab.js';
import { deriveView } from '../src/view.js';
import {
  ALICE,
  BOB,
  FakeClient,
  ME,
  activityNote,
  emptyDashboardRoutes,
  mergeRequestsRoute,
  mr,
  note,
  partialGraphql,
  serverError,
  statesGraphql,
  verdictsGraphql,
  withVerdicts,
} from './helpers.js';

const approvals = (approvedBy = [], userHasApproved = false) => ({
  approved_by: approvedBy.map((user) => ({ user })),
  user_has_approved: userHasApproved,
});


describe('countThreadsAwaitingReply', () => {
  const foreignMr = mr({ iid: 1, author: ALICE });
  const myMr = mr({ iid: 2, author: ME });

  it('counts unresolved threads where I participate and the last note is not mine', () => {
    const discussions = [
      { notes: [note(ME), note(ALICE)] },
      { notes: [note(ALICE), note(ME)] },
      { notes: [note(ME, { resolved: true }), note(ALICE, { resolved: true })] },
      { notes: [note(ALICE), note(BOB)] },
    ];
    assert.equal(countThreadsAwaitingReply(discussions, foreignMr, ME), 1);
  });

  it('treats every unresolved thread on my MR as mine', () => {
    const discussions = [{ notes: [note(ALICE)] }, { notes: [note(BOB), note(ME)] }];
    assert.equal(countThreadsAwaitingReply(discussions, myMr, ME), 1);
  });

  it('ignores system notes and non-resolvable discussions', () => {
    const discussions = [
      { notes: [note(ME), note(ALICE, { system: true })] },
      { notes: [note(ALICE, { resolvable: false })] },
    ];
    assert.equal(countThreadsAwaitingReply(discussions, myMr, ME), 0);
  });
});

describe('collectMentions', () => {
  const todo = (target, author, createdAt, action = 'mentioned') => ({
    action_name: action,
    target,
    author,
    created_at: createdAt,
    target_url: `${target.web_url}#note_${createdAt}`,
  });

  it('groups todos per MR and links to the latest mention', () => {
    const target = mr({ iid: 5, state: 'opened' });
    const items = collectMentions(
      [todo(target, ALICE, '2026-01-01T00:00:00Z'), todo(target, BOB, '2026-01-02T00:00:00Z', 'directly_addressed')],
      new Set(),
    );
    assert.equal(items.length, 1);
    assert.deepEqual(items[0].mentionedBy, ['alice', 'bob']);
    assert.equal(items[0].mentionCount, 2);
    assert.equal(items[0].mentionUrl, `${target.web_url}#note_2026-01-02T00:00:00Z`);
    assert.equal(items[0].mentionedAt, '2026-01-02T00:00:00Z');
    assert.equal(items[0].updatedAt, target.updated_at);
  });

  it('carries the MR author display name alongside the username', () => {
    const target = mr({ iid: 6, state: 'opened', author: { id: 4, username: '60262979', name: 'Иван Петров' } });
    const [item] = collectMentions([todo(target, ALICE, '2026-01-01T00:00:00Z')], new Set());
    assert.equal(item.author, '60262979');
    assert.equal(item.authorName, 'Иван Петров');
  });

  it('skips closed MRs, other actions and excluded keys', () => {
    const items = collectMentions(
      [
        todo(mr({ iid: 1, state: 'merged' }), ALICE, '2026-01-01T00:00:00Z'),
        todo(mr({ iid: 2, state: 'opened' }), ALICE, '2026-01-01T00:00:00Z', 'assigned'),
        todo(mr({ iid: 3, state: 'opened' }), ALICE, '2026-01-01T00:00:00Z'),
      ],
      new Set(['10!3']),
    );
    assert.deepEqual(items, []);
  });
});

describe('collectPossibleMrs', () => {
  const project = { id: 10, path_with_namespace: 'group/project', default_branch: 'main' };
  const me = { id: 1, username: 'me', name: 'Me Myself' };
  const branch = (overrides = {}) => ({
    name: 'feature/x',
    default: false,
    merged: false,
    commit: {
      author_name: 'Me Myself',
      committer_name: 'Me Myself',
      author_email: 'me@example.com',
      committer_email: 'me@example.com',
      committed_date: '2026-01-02T00:00:00Z',
      authored_date: '2026-01-01T00:00:00Z',
    },
    ...overrides,
  });
  const compareClient = (firstUnique) =>
    new FakeClient({ '/projects/10/repository/compare': { commits: firstUnique ? [firstUnique] : [] } });
  const botCommit = { author_name: 'JenkinsBot', committer_name: 'JenkinsBot', committed_date: 't' };
  const branchMr = (iid, state, overrides = {}) => ({
    iid,
    state,
    title: `MR ${iid}`,
    web_url: `https://gitlab.example.com/group/project/-/merge_requests/${iid}`,
    ...overrides,
  });
  const branchMrsClient = (mrs) =>
    new FakeClient({ '/projects/10/merge_requests': mrs });

  it('keeps my non-default, non-merged branch without an open MR', async () => {
    const items = await collectPossibleMrs(project, [branch()], new Set(), me, 'https://gitlab.example.com');
    assert.equal(items.length, 1);
    assert.equal(items[0].key, 'branch:10:feature/x');
    assert.equal(items[0].kind, 'possibleMr');
    assert.equal(items[0].title, 'feature/x');
    assert.equal(items[0].reference, 'group/project');
    assert.equal(items[0].projectPath, 'group/project');
    assert.equal(items[0].author, null);
    assert.equal(items[0].updatedAt, '2026-01-02T00:00:00Z');
    assert.equal(
      items[0].webUrl,
      'https://gitlab.example.com/group/project/-/merge_requests/new?merge_request%5Bsource_branch%5D=feature%2Fx',
    );
  });

  it('excludes default and branches with an open MR, but keeps merged branches flagged', async () => {
    const items = await collectPossibleMrs(
      project,
      [
        branch(),
        branch({ name: 'main', default: true }),
        branch({ name: 'done', merged: true }),
        branch({ name: 'with-mr' }),
      ],
      new Set(['with-mr']),
      me,
      'https://gitlab.example.com',
    );
    assert.deepEqual(items.map((item) => item.title), ['feature/x', 'done']);
    assert.equal(items.find((item) => item.title === 'done').merged, true);
    assert.equal(items.find((item) => item.title === 'feature/x').merged, false);
  });

  it('matches the committer identity case-insensitively by email or username', async () => {
    const client = compareClient({ author_name: 'Alice', committer_name: 'Bob' });
    const items = await collectPossibleMrs(
      project,
      [
        branch({
          name: 'by-email',
          commit: { author_name: 'Someone', committer_name: 'Else', author_email: 'Me@Example.com', committed_date: 't' },
        }),
        branch({ name: 'by-username', commit: { author_name: 'ME', committed_date: 't' } }),
        branch({ name: 'foreign', commit: { author_name: 'Alice', committer_name: 'Bob', committed_date: 't' } }),
      ],
      new Set(),
      { ...me, email: 'me@example.com' },
      'https://gitlab.example.com',
      client,
    );
    assert.deepEqual(items.map((item) => item.title).sort(), ['by-email', 'by-username']);
  });

  it('falls back to authored_date when committed_date is missing', async () => {
    const items = await collectPossibleMrs(
      project,
      [branch({ commit: { author_name: 'Me Myself', authored_date: '2026-01-03T00:00:00Z' } })],
      new Set(),
      me,
      'https://gitlab.example.com',
    );
    assert.equal(items[0].updatedAt, '2026-01-03T00:00:00Z');
  });

  it('ignores branches with no commit or no matching identity', async () => {
    const items = await collectPossibleMrs(
      project,
      [branch({ name: 'no-commit', commit: null }), branch({ name: 'no-identity' })],
      new Set(),
      {},
      'https://gitlab.example.com',
    );
    assert.deepEqual(items, []);
  });

  it('matches a committer-only identity', async () => {
    const items = await collectPossibleMrs(
      project,
      [branch({ commit: { author_name: 'Alice', committer_name: 'Me Myself', committed_date: 't' } })],
      new Set(),
      me,
      'https://gitlab.example.com',
    );
    assert.deepEqual(items.map((item) => item.title), ['feature/x']);
  });

  it('builds a create-MR link under a self-hosted base path', async () => {
    const items = await collectPossibleMrs(
      project,
      [branch()],
      new Set(),
      me,
      'https://gitlab.example.com/sub',
    );
    assert.equal(
      items[0].webUrl,
      'https://gitlab.example.com/sub/group/project/-/merge_requests/new?merge_request%5Bsource_branch%5D=feature%2Fx',
    );
  });

  it('attributes a bot-tipped branch by its first unique commit', async () => {
    const client = compareClient({ author_name: 'Me Myself', author_email: 'me@example.com' });
    const items = await collectPossibleMrs(
      project,
      [branch({ name: 'task/bot-tip', commit: botCommit })],
      new Set(),
      me,
      'https://gitlab.example.com',
      client,
    );
    assert.deepEqual(items.map((item) => item.title), ['task/bot-tip']);
    const compareCalls = client.calls.filter((call) => call.path === '/projects/10/repository/compare');
    assert.deepEqual(compareCalls.map((call) => call.path), ['/projects/10/repository/compare']);
    assert.deepEqual(compareCalls[0].params, { from: 'main', to: 'task/bot-tip', straight: false });
  });

  it('takes the first (oldest) commit when compare returns several in chronological order', async () => {
    const mine = { id: 'a1', author_name: 'Me Myself', author_email: 'me@example.com' };
    const bot = { id: 'c3', author_name: 'JenkinsBot', committer_name: 'JenkinsBot' };
    const client = new FakeClient({ '/projects/10/repository/compare': { commits: [mine, bot] } });
    const items = await collectPossibleMrs(
      project,
      [branch({ name: 'task/ordered', commit: bot })],
      new Set(),
      me,
      'https://gitlab.example.com',
      client,
    );
    assert.deepEqual(items.map((item) => item.title), ['task/ordered']);
  });

  it('excludes a bot-tipped branch whose first unique commit is foreign', async () => {
    const client = compareClient({ author_name: 'Alice', author_email: 'alice@example.com' });
    const items = await collectPossibleMrs(
      project,
      [branch({ name: 'task/foreign-origin', commit: botCommit })],
      new Set(),
      me,
      'https://gitlab.example.com',
      client,
    );
    assert.deepEqual(items, []);
  });

  it('excludes a branch with no commits unique to the default branch', async () => {
    const client = compareClient(null);
    const items = await collectPossibleMrs(
      project,
      [branch({ name: 'task/stale-sync', commit: botCommit })],
      new Set(),
      me,
      'https://gitlab.example.com',
      client,
    );
    assert.deepEqual(items, []);
  });

  it('excludes a bot-tipped branch when compare fails without failing the section', async () => {
    let failing = true;
    const client = new FakeClient({
      '/projects/10/repository/compare': () =>
        failing
          ? Promise.reject(serverError('/projects/10/repository/compare'))
          : { commits: [{ author_name: 'Me Myself', author_email: 'me@example.com' }] },
    });
    const botBranch = branch({ name: 'task/error', commit: botCommit });
    const call = () => collectPossibleMrs(project, [botBranch], new Set(), me, 'https://gitlab.example.com', client);
    assert.deepEqual((await call()).map((item) => item.title), []);
    failing = false;
    assert.deepEqual((await call()).map((item) => item.title), ['task/error']);
    assert.equal(client.calls.filter((item) => item.path === '/projects/10/repository/compare').length, 2);
  });

  it('excludes a bot-tipped branch on a network error without failing the section', async () => {
    const client = new FakeClient({
      '/projects/10/repository/compare': () => Promise.reject(new TypeError('fetch failed')),
    });
    const items = await collectPossibleMrs(
      project,
      [branch({ name: 'task/network-error', commit: botCommit })],
      new Set(),
      me,
      'https://gitlab.example.com',
      client,
    );
    assert.deepEqual(items, []);
  });

  it('propagates fatal compare errors', async () => {
    const client = new FakeClient({
      '/projects/10/repository/compare': () =>
        Promise.reject(new GitLabError(401, '401 Unauthorized', '/projects/10/repository/compare')),
    });
    await assert.rejects(
      collectPossibleMrs(
        project,
        [branch({ name: 'task/fatal', commit: botCommit })],
        new Set(),
        me,
        'https://gitlab.example.com',
        client,
      ),
    );
  });

  it('reuses cached attribution while the branch tip does not change', async () => {
    const client = compareClient({ author_name: 'Me Myself', author_email: 'me@example.com' });
    const botBranch = branch({ name: 'task/cached', commit: { ...botCommit, id: 'sha1' } });
    await collectPossibleMrs(project, [botBranch], new Set(), me, 'https://gitlab.example.com', client);
    await collectPossibleMrs(project, [botBranch], new Set(), me, 'https://gitlab.example.com', client);
    assert.equal(client.calls.filter((call) => call.path === '/projects/10/repository/compare').length, 1);
    const renamedTip = branch({ name: 'task/cached', commit: { ...botCommit, id: 'sha2' } });
    await collectPossibleMrs(project, [renamedTip], new Set(), me, 'https://gitlab.example.com', client);
    assert.equal(client.calls.filter((call) => call.path === '/projects/10/repository/compare').length, 2);
  });

  it('does not request compare when the project has no default branch', async () => {
    const client = compareClient({ author_name: 'Me Myself' });
    const items = await collectPossibleMrs(
      { id: 10, path_with_namespace: 'group/project', default_branch: null },
      [branch({ name: 'task/no-default', commit: botCommit })],
      new Set(),
      me,
      'https://gitlab.example.com',
      client,
    );
    assert.deepEqual(items, []);
    assert.deepEqual(client.calls, []);
  });

  it('attaches the state of an existing closed MR', async () => {
    const client = branchMrsClient([branchMr(4945, 'closed', { title: 'ПОКА НЕ СМОТРЕТЬ' })]);
    const items = await collectPossibleMrs(
      project,
      [branch({ name: 'task/closed-mr' })],
      new Set(),
      me,
      'https://gitlab.example.com',
      client,
    );
    assert.equal(items.length, 1);
    assert.equal(items[0].mrState, 'closed');
    assert.equal(items[0].mrIid, 4945);
    assert.equal(items[0].mrTitle, 'ПОКА НЕ СМОТРЕТЬ');
    assert.equal(items[0].mrUrl, 'https://gitlab.example.com/group/project/-/merge_requests/4945');
    assert.deepEqual(client.calls[0].params, { source_branch: 'task/closed-mr', state: 'all' });
    assert.equal(
      items[0].webUrl,
      'https://gitlab.example.com/group/project/-/merge_requests/new?merge_request%5Bsource_branch%5D=task%2Fclosed-mr',
    );
  });

  it('marks a branch with a merged MR', async () => {
    const client = branchMrsClient([branchMr(5001, 'merged')]);
    const items = await collectPossibleMrs(
      project,
      [branch({ name: 'task/merged-mr' })],
      new Set(),
      me,
      'https://gitlab.example.com',
      client,
    );
    assert.equal(items[0].mrState, 'merged');
    assert.equal(items[0].mrIid, 5001);
  });

  it('picks the newest closed or merged MR and ignores opened ones', async () => {
    const client = branchMrsClient([
      branchMr(100, 'closed'),
      branchMr(150, 'opened'),
      branchMr(200, 'merged'),
    ]);
    const items = await collectPossibleMrs(
      project,
      [branch({ name: 'task/newest-mr' })],
      new Set(),
      me,
      'https://gitlab.example.com',
      client,
    );
    assert.equal(items[0].mrIid, 200);
    assert.equal(items[0].mrState, 'merged');
  });

  it('omits MR state when the lookup finds nothing or fails', async () => {
    const noMrs = branchMrsClient([]);
    const [withNone] = await collectPossibleMrs(
      project,
      [branch({ name: 'task/no-mr-info' })],
      new Set(),
      me,
      'https://gitlab.example.com',
      noMrs,
    );
    assert.ok(!('mrState' in withNone));
    assert.ok(!('mrUrl' in withNone));

    const failing = new FakeClient({
      '/projects/10/merge_requests': serverError('/projects/10/merge_requests'),
    });
    const [withError] = await collectPossibleMrs(
      project,
      [branch({ name: 'task/mr-error' })],
      new Set(),
      me,
      'https://gitlab.example.com',
      failing,
    );
    assert.ok(!('mrState' in withError));
  });

  it('propagates fatal MR lookup errors', async () => {
    const client = new FakeClient({
      '/projects/10/merge_requests': () =>
        Promise.reject(new GitLabError(401, '401 Unauthorized', '/projects/10/merge_requests')),
    });
    await assert.rejects(
      collectPossibleMrs(
        project,
        [branch({ name: 'task/mr-fatal' })],
        new Set(),
        me,
        'https://gitlab.example.com',
        client,
      ),
    );
  });
});

describe('collectDashboard', () => {
  it('sorts MRs into sections', async () => {
    const reviewing = mr({ iid: 1, updatedAt: '2026-01-03T00:00:00Z', user_notes_count: 2 });
    const authored = mr({ iid: 2, author: ME, user_notes_count: 1 });
    const maintained = mr({ iid: 3, author: BOB });
    const approvedMaintained = mr({ iid: 4, author: BOB });
    const draftMaintained = mr({ iid: 5, author: BOB, draft: true });
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/projects': [{ id: 10 }],
      '/merge_requests': mergeRequestsRoute({ authored: [authored], reviewing: [reviewing] }),
      '/projects/10/merge_requests': [reviewing, maintained, approvedMaintained, draftMaintained],
      '/projects/10/merge_requests/1/approvals': approvals(),
      '/projects/10/merge_requests/2/approvals': approvals([ALICE]),
      '/projects/10/merge_requests/3/approvals': approvals(),
      '/projects/10/merge_requests/4/approvals': approvals([ME], true),
      '/projects/10/merge_requests/1/discussions': [{ notes: [note(ME), note(ALICE)] }],
      '/projects/10/merge_requests/2/discussions': [{ notes: [note(BOB)] }],
    });

    const { sections, username, skippedMrs, skippedProjects, approvals: approvalMap } = await collectDashboard(client);

    assert.equal(username, 'me');
    assert.equal(skippedMrs, 0);
    assert.equal(skippedProjects, 0);
    assert.deepEqual(sections.reviewing.map((item) => item.key), ['10!1']);
    assert.deepEqual(sections.awaitingApproval.map((item) => item.key), ['10!3']);
    assert.deepEqual(sections.authored.map((item) => item.key), ['10!2']);
    assert.equal(sections.authored[0].approvalsCount, 1);
    assert.deepEqual(sections.authored[0].approverNames, ['alice']);
    assert.deepEqual(approvalMap['10!4'], { approvedByMe: true, approvalsCount: 1, approverNames: ['me'] });
    assert.deepEqual(sections.threads.map((item) => item.key), ['10!1', '10!2']);
    assert.ok(!client.calls.some((call) => call.path === '/projects/10/merge_requests/3/discussions'));
  });

  it('keeps approval info for an approved MR that is hidden from sections', async () => {
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/projects': [{ id: 10 }],
      '/projects/10/merge_requests': [mr({ iid: 4, author: BOB })],
      '/projects/10/merge_requests/4/approvals': approvals([ME], true),
    });
    const dashboard = await collectDashboard(client);

    assert.deepEqual(dashboard.sections.awaitingApproval, []);
    assert.deepEqual(dashboard.approvals['10!4'], { approvedByMe: true, approvalsCount: 1, approverNames: ['me'] });
    assert.equal(deriveView(dashboard).approvals['10!4'].approvedByMe, true);
  });

  it('excludes my own MRs from awaiting approval', async () => {
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/projects': [{ id: 10 }],
      '/projects/10/merge_requests': [mr({ iid: 1, author: ME }), mr({ iid: 2, author: BOB })],
      '/projects/10/merge_requests/2/approvals': approvals(),
    });
    const { sections } = await collectDashboard(client);
    assert.deepEqual(sections.awaitingApproval.map((item) => item.key), ['10!2']);
  });

  it('hides MRs awaiting my approval once they have enough approvals from others', async () => {
    const client = (approvalThreshold) => [
      new FakeClient({
        ...emptyDashboardRoutes(),
        '/projects': [{ id: 10 }],
        '/projects/10/merge_requests': [mr({ iid: 1, author: BOB }), mr({ iid: 2, author: BOB }), mr({ iid: 3, author: BOB })],
        '/projects/10/merge_requests/1/approvals': approvals(),
        '/projects/10/merge_requests/2/approvals': approvals([ALICE]),
        '/projects/10/merge_requests/3/approvals': approvals([ALICE, BOB]),
      }),
      { approvalThreshold },
    ];
    const keys = async (threshold) =>
      (await collectDashboard(...client(threshold))).sections.awaitingApproval.map((item) => item.key);

    assert.deepEqual(await keys(0), ['10!1', '10!2', '10!3']);
    assert.deepEqual(await keys(2), ['10!1', '10!2']);
    assert.deepEqual(await keys(1), ['10!1']);
  });

  it('keeps the dashboard when optional sources fail', async () => {
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/projects': serverError('/projects'),
      '/todos': serverError('/todos'),
    });

    const dashboard = await collectDashboard(client);

    assert.equal(dashboard.projectsUnavailable, true);
    assert.equal(dashboard.mentionsUnavailable, true);
    assert.equal(dashboard.pipelinesUnavailable, true);
    assert.equal(dashboard.watchedProjects, 0);
  });

  it('skips a project or an MR that fails instead of failing the whole dashboard', async () => {
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/projects': [{ id: 10 }, { id: 11 }],
      '/projects/10/merge_requests': serverError('/projects/10/merge_requests'),
      '/projects/11/merge_requests': [mr({ projectId: 11, iid: 1 }), mr({ projectId: 11, iid: 2 })],
      '/projects/11/merge_requests/1/approvals': approvals(),
      '/projects/11/merge_requests/2/approvals': serverError('/projects/11/merge_requests/2/approvals'),
    });

    const dashboard = await collectDashboard(client);

    assert.equal(dashboard.skippedProjects, 1);
    assert.equal(dashboard.skippedMrs, 1);
    assert.deepEqual(dashboard.sections.awaitingApproval.map((item) => item.key), ['11!1']);
  });

  it('fails instead of saving an empty dashboard when every MR fails with 5xx', async () => {
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/merge_requests': mergeRequestsRoute({ authored: [mr({ iid: 1, author: ME })], reviewing: [mr({ iid: 2 })] }),
      '/projects/10/merge_requests/1/approvals': serverError('/projects/10/merge_requests/1/approvals'),
      '/projects/10/merge_requests/2/approvals': serverError('/projects/10/merge_requests/2/approvals'),
    });
    await assert.rejects(collectDashboard(client), { status: 500 });
  });

  it('fails when every maintained project fails with 5xx', async () => {
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/projects': [{ id: 10 }],
      '/projects/10/merge_requests': serverError('/projects/10/merge_requests'),
    });
    await assert.rejects(collectDashboard(client), { status: 500 });
  });

  it('fails when every project fails and at least one of them with 5xx', async () => {
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/projects': [{ id: 10 }, { id: 11 }],
      '/projects/10/merge_requests': new GitLabError(403, '403 Forbidden', '/projects/10/merge_requests'),
      '/projects/11/merge_requests': serverError('/projects/11/merge_requests'),
    });
    await assert.rejects(collectDashboard(client), { status: 500 });
  });

  it('skips projects without access even when all of them are forbidden', async () => {
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/projects': [{ id: 10 }],
      '/projects/10/merge_requests': new GitLabError(403, '403 Forbidden', '/projects/10/merge_requests'),
    });
    const dashboard = await collectDashboard(client);
    assert.equal(dashboard.skippedProjects, 1);
  });

  it('fails on rate limiting of an optional source', async () => {
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/projects': new GitLabError(429, 'Rate limited', '/projects'),
    });
    await assert.rejects(collectDashboard(client), { status: 429 });
  });

  it('does not repeat MRs under review in mentions', async () => {
    const reviewing = mr({ iid: 1, state: 'opened' });
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/merge_requests': mergeRequestsRoute({ reviewing: [reviewing] }),
      '/projects/10/merge_requests/1/approvals': approvals(),
      '/todos': [
        { action_name: 'mentioned', target: reviewing, author: ALICE, created_at: '2026-01-01T00:00:00Z' },
      ],
    });
    const { sections } = await collectDashboard(client);
    assert.deepEqual(sections.reviewing.map((item) => item.key), ['10!1']);
    assert.deepEqual(sections.mentioned, []);
  });

  it('still excludes reviewing MRs from mentions when the other sections are skipped', async () => {
    const reviewing = mr({ iid: 1, state: 'opened' });
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/merge_requests': mergeRequestsRoute({ reviewing: [reviewing] }),
      '/projects/10/merge_requests/1/approvals': approvals(),
      '/todos': [
        { action_name: 'mentioned', target: reviewing, author: ALICE, created_at: '2026-01-01T00:00:00Z' },
      ],
    });
    const { sections } = await collectDashboard(client, {
      skipSections: ['reviewing', 'assigned', 'threads', 'authored', 'awaitingApproval', 'brokenBranches', 'possibleMrs'],
    });
    assert.deepEqual(sections.mentioned, []);
  });

  it('fails when a required source fails', async () => {
    const client = new FakeClient({ ...emptyDashboardRoutes(), '/merge_requests': serverError('/merge_requests') });
    await assert.rejects(collectDashboard(client), { status: 500 });
  });

  it('limits maintained projects to the selected ones', async () => {
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/projects': [{ id: 10 }, { id: 11 }],
      '/projects/11/merge_requests': [],
    });

    const dashboard = await collectDashboard(client, { selectedProjectIds: [11] });

    assert.equal(dashboard.watchedProjects, 1);
    assert.ok(!client.calls.some((call) => call.path === '/projects/10/merge_requests'));
  });

  it('attaches pipeline failures and marks pipelines that failed only on ignored checks', async () => {
    const failed = mr({ iid: 1, author: ME });
    const ignoredOnly = mr({ iid: 2, author: ME });
    const pipeline = (id) => ({
      id: `gid://gitlab/Ci::Pipeline/${id}`,
      sha: `sha${id}`,
      status: 'FAILED',
      path: `/group/project/-/pipelines/${id}`,
      project: { fullPath: 'group/project' },
    });
    const job = (id, name) => ({ id, name, stage: 'test', failure_reason: 'script_failure', web_url: 'u' });
    const client = new FakeClient(
      {
        ...emptyDashboardRoutes(),
        '/merge_requests': mergeRequestsRoute({ authored: [failed, ignoredOnly] }),
        '/projects/10/merge_requests/1/approvals': approvals(),
        '/projects/10/merge_requests/2/approvals': approvals(),
        '/projects/group%2Fproject/pipelines/101/jobs': [job(1, 'unit')],
        '/projects/group%2Fproject/pipelines/102/jobs': [job(2, 'Approves')],
        '/projects/group%2Fproject/pipelines/101/bridges': [],
        '/projects/group%2Fproject/pipelines/102/bridges': [],
      },
      {
        graphql: () => ({
          currentUser: {
            authoredMergeRequests: {
              pageInfo: { hasNextPage: false, endCursor: null },
              nodes: [
                { iid: '1', project: { fullPath: 'group/project' }, headPipeline: pipeline(101) },
                { iid: '2', project: { fullPath: 'group/project' }, headPipeline: pipeline(102) },
              ],
            },
          },
        }),
      },
    );

    const { sections } = await collectDashboard(client, { ignoredCheckNames: ['approves'] });
    const byKey = Object.fromEntries(sections.authored.map((item) => [item.key, item.pipeline]));

    assert.equal(byKey['10!1'].status, 'failed');
    assert.equal(byKey['10!1'].onlyIgnoredFailures, false);
    assert.deepEqual(byKey['10!1'].failures.map((failure) => failure.name), ['unit']);
    assert.equal(byKey['10!2'].onlyIgnoredFailures, true);
  });

  it('keeps a failed pipeline in the badge when its jobs cannot be loaded', async () => {
    const client = new FakeClient(
      {
        ...emptyDashboardRoutes(),
        '/merge_requests': mergeRequestsRoute({ authored: [mr({ iid: 1, author: ME })] }),
        '/projects/10/merge_requests/1/approvals': approvals(),
        '/projects/group%2Fproject/pipelines/101/jobs': serverError('/jobs'),
      },
      {
        graphql: () => ({
          currentUser: {
            authoredMergeRequests: {
              pageInfo: { hasNextPage: false, endCursor: null },
              nodes: [
                {
                  iid: '1',
                  project: { fullPath: 'group/project' },
                  headPipeline: { id: 'gid://gitlab/Ci::Pipeline/101', sha: 's', status: 'FAILED', path: '/p/101' },
                },
              ],
            },
          },
        }),
      },
    );

    const dashboard = await collectDashboard(client);
    const [item] = dashboard.sections.authored;

    assert.equal(item.pipeline.failures, null);
    assert.equal(item.pipeline.onlyIgnoredFailures, false);
    assert.equal(deriveView(dashboard).counts.failedCi, 1);
  });

  it('attaches head pipeline failures to MRs from other sections', async () => {
    const reviewing = mr({ iid: 2, detailed_merge_status: 'ci_must_pass' });
    const mentionedTarget = mr({ projectId: 11, iid: 3, detailed_merge_status: 'ci_must_pass' });
    const withPipeline = (mr, pipeline) => ({ ...mr, head_pipeline: pipeline });
    const job = (id, name) => ({ id, name, stage: 'test', failure_reason: 'script_failure', web_url: 'u' });
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/merge_requests': mergeRequestsRoute({ reviewing: [reviewing] }),
      '/projects/10/merge_requests/2/approvals': approvals(),
      '/projects/10/merge_requests/2': withPipeline(reviewing, {
        id: 201,
        sha: 'sha201',
        status: 'failed',
        web_url: 'https://gitlab.example.com/group/project/-/pipelines/201',
      }),
      '/projects/11/merge_requests/3': withPipeline(mentionedTarget, {
        id: 301,
        sha: 'sha301',
        status: 'failed',
        web_url: 'https://gitlab.example.com/group/project/-/pipelines/301',
      }),
      '/todos': [
        {
          id: 1,
          action_name: 'mentioned',
          target_type: 'MergeRequest',
          target: { ...mentionedTarget, state: 'opened' },
          target_url: 'u',
          author: ALICE,
          created_at: '2026-01-02T00:00:00Z',
        },
      ],
      '/projects/group%2Fproject/pipelines/201/jobs': [job(5, 'unit')],
      '/projects/group%2Fproject/pipelines/201/bridges': [],
      '/projects/group%2Fproject/pipelines/301/jobs': [job(6, 'lint')],
      '/projects/group%2Fproject/pipelines/301/bridges': [],
    });

    const { sections } = await collectDashboard(client);
    const reviewingItem = sections.reviewing.find((item) => item.key === '10!2');
    const mentionedItem = sections.mentioned.find((item) => item.key === '11!3');

    assert.equal(reviewingItem.pipeline.status, 'failed');
    assert.deepEqual(reviewingItem.pipeline.failures.map((failure) => failure.name), ['unit']);
    assert.equal(mentionedItem.pipeline.status, 'failed');
    assert.deepEqual(mentionedItem.pipeline.failures.map((failure) => failure.name), ['lint']);
  });

  it('does not fetch failures for healthy head pipelines of other sections', async () => {
    const reviewing = mr({
      iid: 2,
      head_pipeline: { id: 202, sha: 'sha202', status: 'running', web_url: 'https://gitlab.example.com/group/project/-/pipelines/202' },
    });
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/merge_requests': mergeRequestsRoute({ reviewing: [reviewing] }),
      '/projects/10/merge_requests/2/approvals': approvals(),
    });

    const { sections } = await collectDashboard(client);

    assert.equal(sections.reviewing[0].pipeline.status, 'running');
    assert.ok(!client.calls.some((call) => call.path.includes('/pipelines/202/jobs')));
  });

  it('does not resolve head pipelines for other sections without a blocked merge status', async () => {
    const reviewing = mr({ iid: 2 });
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/merge_requests': mergeRequestsRoute({ reviewing: [reviewing] }),
      '/projects/10/merge_requests/2/approvals': approvals(),
    });

    const { sections } = await collectDashboard(client);

    assert.equal(sections.reviewing[0].pipeline, null);
    assert.ok(!client.calls.some((call) => call.path === '/projects/10/merge_requests/2'));
  });

  it('resolves head pipelines for transient merge statuses using the previous status', async () => {
    const reviewing = mr({ iid: 2, detailed_merge_status: 'unchecked' });
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/merge_requests': mergeRequestsRoute({ reviewing: [reviewing] }),
      '/projects/10/merge_requests/2/approvals': approvals(),
      '/projects/10/merge_requests/2': {
        ...reviewing,
        head_pipeline: { id: 203, sha: 'sha203', status: 'failed', web_url: 'https://gitlab.example.com/group/project/-/pipelines/203' },
      },
      '/projects/group%2Fproject/pipelines/203/jobs': [{ id: 7, name: 'unit', stage: 'test', failure_reason: 'script_failure', web_url: 'u' }],
      '/projects/group%2Fproject/pipelines/203/bridges': [],
    });

    const withoutPrevious = await collectDashboard(client);
    assert.ok(!client.calls.some((call) => call.path === '/projects/10/merge_requests/2'));
    assert.equal(withoutPrevious.sections.reviewing[0].pipeline, null);

    const withPrevious = await collectDashboard(client, { previousStatuses: new Map([['10!2', 'ci_must_pass']]) });
    const [item] = withPrevious.sections.reviewing;

    assert.equal(item.pipeline.status, 'failed');
    assert.deepEqual(item.pipeline.failures.map((failure) => failure.name), ['unit']);
  });
});

describe('collectDashboard with GraphQL merge request states', () => {
  const authored = mr({ iid: 1, author: ME });
  const reviewing = mr({ iid: 2 });
  const maintained = mr({ iid: 3, author: BOB });
  const routes = () => ({
    ...emptyDashboardRoutes(),
    '/projects': [{ id: 10 }],
    '/merge_requests': mergeRequestsRoute({ authored: [authored], reviewing: [reviewing] }),
    '/projects/10/merge_requests': [reviewing, maintained],
    '/projects/10/merge_requests/1/discussions': [{ notes: [note(ALICE)] }],
    '/projects/10/merge_requests/2/discussions': [{ notes: [note(ME), note(ALICE)] }],
  });
  const states = {
    'group/project': {
      1: { approvers: [ALICE], notes: 1, resolvable: 1, resolved: 0 },
      2: { approvers: [ME], notes: 2, resolvable: 1, resolved: 0 },
      3: { approvers: [], notes: 5, resolvable: 2, resolved: 0 },
    },
  };
  const discussionCalls = (client) => client.calls.filter((call) => call.path.endsWith('/discussions'));

  it('takes approvals from one GraphQL query and loads discussions only where threads are unresolved', async () => {
    const client = new FakeClient(routes(), { graphql: statesGraphql(states) });

    const dashboard = await collectDashboard(client);

    assert.ok(!client.calls.some((call) => call.path.endsWith('/approvals')));
    assert.deepEqual(discussionCalls(client).map((call) => call.path).sort(), [
      '/projects/10/merge_requests/1/discussions',
      '/projects/10/merge_requests/2/discussions',
    ]);
    assert.equal(dashboard.sections.authored[0].approvalsCount, 1);
    assert.equal(dashboard.sections.reviewing[0].approvedByMe, true);
    assert.deepEqual(dashboard.sections.awaitingApproval.map((item) => item.key), ['10!3']);
    assert.deepEqual(dashboard.sections.threads.map((item) => item.key).sort(), ['10!1', '10!2']);
    assert.deepEqual(dashboard.threadCache, {
      userId: ME.id,
      entries: {
        '10!1': { stateKey: '2026-01-01T00:00:00Z:1:1:0', threadsAwaitingReply: 1 },
        '10!2': { stateKey: '2026-01-01T00:00:00Z:2:1:0', threadsAwaitingReply: 1 },
      },
      verdictEntries: {},
    });
  });

  it('reuses cached thread counts while the thread counters stay the same', async () => {
    const first = await collectDashboard(new FakeClient(routes(), { graphql: statesGraphql(states) }));
    const changed = {
      'group/project': { ...states['group/project'], 2: { approvers: [], notes: 3, resolvable: 1, resolved: 0 } },
    };
    const client = new FakeClient(routes(), { graphql: statesGraphql(changed) });

    await collectDashboard(client, { threadCache: first.threadCache });

    assert.deepEqual(discussionCalls(client).map((call) => call.path), ['/projects/10/merge_requests/2/discussions']);
  });

  it('reloads discussions when the MR was updated even if the counters did not change', async () => {
    const first = await collectDashboard(new FakeClient(routes(), { graphql: statesGraphql(states) }));
    const updatedAuthored = { ...authored, updated_at: '2026-01-02T00:00:00Z' };
    const client = new FakeClient(
      { ...routes(), '/merge_requests': mergeRequestsRoute({ authored: [updatedAuthored], reviewing: [reviewing] }) },
      { graphql: statesGraphql(states) },
    );

    await collectDashboard(client, { threadCache: first.threadCache });

    assert.deepEqual(discussionCalls(client).map((call) => call.path), ['/projects/10/merge_requests/1/discussions']);
  });

  it('ignores a thread cache of another user', async () => {
    const client = new FakeClient(routes(), { graphql: statesGraphql(states) });
    await collectDashboard(client, {
      threadCache: { userId: 99, entries: { '10!1': { stateKey: '1:1:0', threadsAwaitingReply: 7 } } },
    });
    assert.equal(discussionCalls(client).length, 2);
  });

  it('falls back to REST only for projects whose GraphQL query fails', async () => {
    const other = mr({ projectId: 11, iid: 2, references: { full: 'group/other!2' } });
    const client = new FakeClient(
      {
        ...emptyDashboardRoutes(),
        '/merge_requests': mergeRequestsRoute({ reviewing: [reviewing, other] }),
        '/projects/11/merge_requests/2/approvals': approvals([ME], true),
      },
      {
        graphql: (query, variables) => {
          if (variables.fullPath === 'group/other') {
            throw new GitLabError(200, 'GraphQL: field missing', '/graphql');
          }
          return statesGraphql({ 'group/project': { 2: { approvers: [] } } })(query, variables);
        },
      },
    );

    const { sections } = await collectDashboard(client);

    assert.deepEqual(
      client.calls.filter((call) => call.path.endsWith('/approvals')).map((call) => call.path),
      ['/projects/11/merge_requests/2/approvals'],
    );
    const byKey = Object.fromEntries(sections.reviewing.map((item) => [item.key, item.approvedByMe]));
    assert.deepEqual(byKey, { '10!2': false, '11!2': true });
  });

  it('keeps MRs where I asked for changes out of the badge until review is requested again', async () => {
    const asked = mr({ iid: 1, references: { full: 'group/project!1' } });
    const fresh = mr({ iid: 2, references: { full: 'group/project!2' } });
    const client = (reviewState) =>
      new FakeClient(
        { ...emptyDashboardRoutes(), '/merge_requests': mergeRequestsRoute({ reviewing: [asked, fresh] }) },
        { graphql: statesGraphql({ 'group/project': { 1: { reviewStates: { [ME.id]: reviewState } }, 2: {} } }) },
      );

    const waiting = await collectDashboard(client('REQUESTED_CHANGES'));
    assert.deepEqual(waiting.sections.reviewing.map((item) => [item.key, item.myReviewState]), [
      ['10!1', 'requested_changes'],
      ['10!2', null],
    ]);
    assert.equal(deriveView(waiting).counts.waiting, 1);

    const again = await collectDashboard(client('UNREVIEWED'));
    assert.equal(deriveView(again).counts.waiting, 2);
  });

  it('drops review states when the GitLab schema does not know them', async () => {
    const queries = [];
    const client = new FakeClient(
      { ...emptyDashboardRoutes(), '/merge_requests': mergeRequestsRoute({ reviewing: [mr({ iid: 1 })] }) },
      {
        graphql: (query, variables) => {
          queries.push(query);
          if (query.includes('reviewState')) {
            throw new GitLabError(200, "GraphQL: Field 'mergeRequestInteraction' doesn't exist", '/graphql');
          }
          return statesGraphql({ 'group/project': { 1: { approvers: [ME] } } })(query, variables);
        },
      },
    );

    const { sections } = await collectDashboard(client);

    assert.equal(sections.reviewing[0].approvedByMe, true);
    assert.equal(sections.reviewing[0].myReviewState, undefined);
    assert.equal(queries.filter((query) => query.includes('MergeRequestStates')).length, 2);
    assert.ok(!client.calls.some((call) => call.path.endsWith('/approvals')));
  });

  it('keeps review state on for GraphQL errors unrelated to it', async () => {
    const other = mr({ projectId: 11, iid: 2, references: { full: 'group/other!2' } });
    const queries = [];
    const client = new FakeClient(
      {
        ...emptyDashboardRoutes(),
        '/merge_requests': mergeRequestsRoute({ reviewing: [mr({ iid: 1 }), other] }),
        '/projects/11/merge_requests/2/approvals': approvals(),
      },
      {
        graphql: (query, variables) => {
          if (query.includes('MergeRequestStates')) {
            queries.push({ path: variables.fullPath, withReviewState: query.includes('reviewState') });
          }
          if (variables.fullPath === 'group/other') {
            throw new GitLabError(200, 'GraphQL: Query has complexity of 300, which exceeds max complexity', '/graphql');
          }
          return statesGraphql({ 'group/project': { 1: { reviewStates: { [ME.id]: 'REVIEWED' } } } })(query, variables);
        },
      },
    );

    const { sections } = await collectDashboard(client);

    assert.deepEqual(queries, [
      { path: 'group/project', withReviewState: true },
      { path: 'group/other', withReviewState: true },
    ]);
    const byKey = Object.fromEntries(sections.reviewing.map((item) => [item.key, item.myReviewState]));
    assert.deepEqual(byKey, { '10!1': 'reviewed', '11!2': undefined });
    assert.ok(client.calls.some((call) => call.path === '/projects/11/merge_requests/2/approvals'));
  });

  it('falls back to REST when the retry without review state fails as well', async () => {
    const client = new FakeClient(
      {
        ...emptyDashboardRoutes(),
        '/merge_requests': mergeRequestsRoute({ reviewing: [mr({ iid: 1 })] }),
        '/projects/10/merge_requests/1/approvals': approvals([ME], true),
      },
      {
        graphql: (query) => {
          throw new GitLabError(
            200,
            query.includes('reviewState') ? "GraphQL: Field 'mergeRequestInteraction' doesn't exist" : 'GraphQL: boom',
            '/graphql',
          );
        },
      },
    );
    const { sections } = await collectDashboard(client);
    assert.equal(sections.reviewing[0].approvedByMe, true);
  });

  it('falls back to REST for MRs with incomplete GraphQL data', async () => {
    const client = new FakeClient(
      { ...routes(), '/projects/10/merge_requests/3/approvals': approvals([ME], true) },
      {
        graphql: (query, variables) => {
          const data = statesGraphql(states)(query, variables);
          data.project.mergeRequests.nodes.find((node) => node.iid === '3').approvedBy = null;
          return data;
        },
      },
    );

    const dashboard = await collectDashboard(client);

    assert.deepEqual(
      client.calls.filter((call) => call.path.endsWith('/approvals')).map((call) => call.path),
      ['/projects/10/merge_requests/3/approvals'],
    );
    assert.deepEqual(dashboard.sections.awaitingApproval, []);
  });

  it('falls back to REST approvals when GraphQL is unavailable', async () => {
    const client = new FakeClient({
      ...routes(),
      '/projects/10/merge_requests/1/approvals': approvals([ALICE]),
      '/projects/10/merge_requests/2/approvals': approvals(),
      '/projects/10/merge_requests/3/approvals': approvals(),
    });

    const dashboard = await collectDashboard(client);

    assert.equal(client.calls.filter((call) => call.path.endsWith('/approvals')).length, 3);
    assert.equal(dashboard.sections.authored[0].approvalsCount, 1);
  });

  it('shows MRs assigned to me in their own section and counts them in the badge', async () => {
    const assigned = mr({ iid: 4, author: BOB });
    const client = new FakeClient(
      {
        ...routes(),
        '/merge_requests': mergeRequestsRoute({
          authored: [authored],
          reviewing: [reviewing],
          assigned: [assigned, authored],
        }),
        '/projects/10/merge_requests': [reviewing, maintained, assigned],
        '/todos': [{ action_name: 'mentioned', target: { ...assigned, state: 'opened' }, author: ALICE, created_at: 'x' }],
      },
      { graphql: statesGraphql({ 'group/project': { ...states['group/project'], 4: {} } }) },
    );

    const dashboard = await collectDashboard(client);

    assert.deepEqual(dashboard.sections.assigned.map((item) => item.key), ['10!4']);
    assert.ok(!dashboard.sections.awaitingApproval.some((item) => item.key === '10!4'));
    assert.deepEqual(dashboard.sections.mentioned, []);
    assert.equal(deriveView(dashboard).counts.waiting, 4);
  });

  it('keeps working when MRs assigned to me cannot be loaded', async () => {
    const client = new FakeClient(
      {
        ...routes(),
        '/merge_requests': (params) =>
          params.scope === 'assigned_to_me'
            ? Promise.reject(serverError('/merge_requests'))
            : mergeRequestsRoute({ authored: [authored], reviewing: [reviewing] })(params),
      },
      { graphql: statesGraphql(states) },
    );

    const dashboard = await collectDashboard(client);

    assert.equal(dashboard.assignedUnavailable, true);
    assert.deepEqual(dashboard.sections.assigned, []);
  });
});

describe('collectDashboard extras', () => {
  it('collects mentions in issues next to merge requests', async () => {
    const issue = {
      project_id: 10,
      iid: 5,
      title: 'Crash report',
      web_url: 'https://gitlab.example.com/group/project/-/issues/5',
      references: { full: 'group/project#5' },
      author: ALICE,
      updated_at: '2026-01-01T00:00:00Z',
      state: 'opened',
    };
    const mergeRequest = mr({ iid: 5, state: 'opened' });
    const todo = (target, targetType) => ({
      action_name: 'mentioned',
      target_type: targetType,
      target,
      author: BOB,
      created_at: '2026-01-02T00:00:00Z',
      target_url: `${target.web_url}#note_1`,
    });
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/todos': (params) =>
        params.type === 'Issue' ? [todo(issue, 'Issue')] : [todo(mergeRequest, 'MergeRequest')],
    });

    const { sections } = await collectDashboard(client);

    assert.deepEqual(
      sections.mentioned.map(({ key, kind, reference }) => [key, kind, reference]).sort(),
      [
        ['10!5', 'mr', 'group/project!5'],
        ['10#5', 'issue', 'group/project#5'],
      ],
    );
    assert.equal(sections.mentioned.find((item) => item.kind === 'issue').status, null);
  });

  it('reports token expiry and scopes when GitLab exposes them', async () => {
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/personal_access_tokens/self': { expires_at: '2026-02-01', scopes: ['read_api', 'api'] },
    });
    assert.deepEqual((await collectDashboard(client)).token, { expiresAt: '2026-02-01', scopes: ['read_api', 'api'] });
    assert.equal((await collectDashboard(new FakeClient(emptyDashboardRoutes()))).token, null);
  });

  it('lists maintained projects whose default branch pipeline failed', async () => {
    const project = (id, path, branch = 'main') => ({ id, path_with_namespace: path, default_branch: branch });
    const statuses = { 'group/red': 'FAILED', 'group/green': 'SUCCESS' };
    const client = new FakeClient(
      {
        ...emptyDashboardRoutes(),
        '/projects': [project(10, 'group/red'), project(11, 'group/green'), project(12, 'group/empty', null)],
        '/projects/10/merge_requests': [],
        '/projects/11/merge_requests': [],
        '/projects/12/merge_requests': [],
      },
      {
        graphql: (query, variables) => {
          if (!query.includes('DefaultBranchPipelines')) {
            throw new GitLabError(404, 'unexpected', '/graphql');
          }
          return Object.fromEntries(
            Object.keys(variables)
              .filter((name) => name.startsWith('path'))
              .map((name) => [
                `p${name.slice(4)}`,
                {
                  pipelines: {
                    nodes: [
                      {
                        status: statuses[variables[name]],
                        path: `/${variables[name]}/-/pipelines/1`,
                        finishedAt: '2026-01-05T00:00:00Z',
                      },
                    ],
                  },
                },
              ]),
          );
        },
      },
    );

    const dashboard = await collectDashboard(client);

    assert.deepEqual(dashboard.sections.brokenBranches, [
      {
        key: 'branch:10',
        kind: 'branch',
        projectId: 10,
        title: 'group/red',
        reference: 'main',
        webUrl: 'https://gitlab.example.com/group/red/-/pipelines/1',
        updatedAt: '2026-01-05T00:00:00Z',
      },
    ]);
    assert.equal(dashboard.defaultBranchesUnavailable, false);
    assert.equal(deriveView(dashboard).counts.failedCi, 0);
  });

  it('marks default branch statuses unavailable when GraphQL fails', async () => {
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/projects': [{ id: 10, path_with_namespace: 'group/red', default_branch: 'main' }],
      '/projects/10/merge_requests': [],
    });
    const dashboard = await collectDashboard(client);
    assert.equal(dashboard.defaultBranchesUnavailable, true);
    assert.deepEqual(dashboard.sections.brokenBranches, []);
  });

  it('keeps broken branches of other projects when GraphQL fails for one of them', async () => {
    const failed = { pipelines: { nodes: [{ status: 'FAILED', path: '/group/red/-/pipelines/1', finishedAt: 't' }] } };
    const client = new FakeClient(
      {
        ...emptyDashboardRoutes(),
        '/projects': [
          { id: 10, path_with_namespace: 'group/red', default_branch: 'main' },
          { id: 11, path_with_namespace: 'group/gone', default_branch: 'main' },
        ],
        '/projects/10/merge_requests': [],
        '/projects/11/merge_requests': [],
      },
      { graphql: () => partialGraphql({ p0: failed, p1: null }, [{ message: 'boom', path: ['p1'] }]) },
    );
    const dashboard = await collectDashboard(client);
    assert.equal(dashboard.defaultBranchesUnavailable, true);
    assert.deepEqual(dashboard.sections.brokenBranches.map((item) => item.key), ['branch:10']);
  });

  it('collects my branches without an open MR into possibleMrs', async () => {
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/user': { ...ME, name: 'Me Myself' },
      '/projects': [{ id: 10, path_with_namespace: 'group/project' }],
      '/projects/10/merge_requests': [mr({ iid: 1, author: BOB, source_branch: 'feature/has-mr' })],
      '/projects/10/repository/branches': [
        { name: 'main', default: true, merged: false },
        {
          name: 'feature/has-mr',
          default: false,
          merged: false,
          commit: { author_name: 'Me Myself', committed_date: '2026-01-01T00:00:00Z' },
        },
        {
          name: 'feature/new',
          default: false,
          merged: false,
          commit: { author_name: 'Me Myself', committed_date: '2026-01-02T00:00:00Z' },
        },
      ],
    });
    const dashboard = await collectDashboard(client);
    assert.deepEqual(dashboard.sections.possibleMrs.map((item) => item.title), ['feature/new']);
    assert.equal(dashboard.possibleMrsUnavailable, false);
  });

  it('marks possible MRs unavailable when branches cannot be loaded', async () => {
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/projects': [{ id: 10, path_with_namespace: 'group/project' }],
      '/projects/10/merge_requests': [],
    });
    const dashboard = await collectDashboard(client);
    assert.deepEqual(dashboard.sections.possibleMrs, []);
    assert.equal(dashboard.possibleMrsUnavailable, true);
  });

  it('skips possible MRs when the project MR list failed but branches loaded', async () => {
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/user': { ...ME, name: 'Me Myself' },
      '/projects': [{ id: 10, path_with_namespace: 'group/project' }],
      '/projects/10/merge_requests': new GitLabError(403, '403 Forbidden', '/projects/10/merge_requests'),
      '/projects/10/repository/branches': [
        {
          name: 'feature/new',
          default: false,
          merged: false,
          commit: { author_name: 'Me Myself', committed_date: '2026-01-02T00:00:00Z' },
        },
      ],
    });
    const dashboard = await collectDashboard(client);
    assert.deepEqual(dashboard.sections.possibleMrs, []);
    assert.equal(dashboard.possibleMrsUnavailable, false);
  });

  it('does not fetch branches when the possible MRs section is hidden', async () => {
    const client = new FakeClient({
      ...emptyDashboardRoutes(),
      '/projects': [{ id: 10, path_with_namespace: 'group/project' }],
      '/projects/10/merge_requests': [],
    });
    const dashboard = await collectDashboard(client, { skipSections: ['possibleMrs'] });
    assert.deepEqual(dashboard.sections.possibleMrs, []);
    assert.equal(dashboard.possibleMrsUnavailable, false);
    assert.ok(!client.calls.some((call) => call.path === '/projects/10/repository/branches'));
  });

  it('skips mention todos when the mentioned section is skipped', async () => {
    const client = new FakeClient(emptyDashboardRoutes());
    const dashboard = await collectDashboard(client, { skipSections: ['mentioned'] });
    assert.deepEqual(dashboard.sections.mentioned, []);
    assert.equal(dashboard.mentionsUnavailable, false);
    assert.ok(!client.calls.some((call) => call.path === '/todos'));
  });

  it('skips the watched-projects cluster when its sections are all skipped', async () => {
    const client = new FakeClient(emptyDashboardRoutes());
    const dashboard = await collectDashboard(client, {
      skipSections: ['awaitingApproval', 'brokenBranches', 'possibleMrs'],
    });
    assert.equal(dashboard.projectsUnavailable, false);
    assert.equal(dashboard.watchedProjects, 0);
    assert.equal(dashboard.defaultBranchesUnavailable, false);
    assert.equal(dashboard.possibleMrsUnavailable, false);
    assert.ok(!client.calls.some((call) => call.path === '/projects'));
    assert.ok(!client.calls.some((call) => call.path.includes('repository/branches')));
  });

  it('does not fetch authored pipelines when the authored section is skipped', async () => {
    const client = new FakeClient(emptyDashboardRoutes());
    const dashboard = await collectDashboard(client, { skipSections: ['authored'] });
    assert.equal(dashboard.pipelinesUnavailable, false);
    assert.deepEqual(dashboard.sections.authored, []);
  });

  it('does not fetch default branch pipelines when brokenBranches is skipped', async () => {
    const client = new FakeClient(emptyDashboardRoutes());
    const dashboard = await collectDashboard(client, { skipSections: ['brokenBranches'] });
    assert.equal(dashboard.defaultBranchesUnavailable, false);
    assert.deepEqual(dashboard.sections.brokenBranches, []);
  });

  it('asks GitLab to recheck merge status of my and assigned MRs', async () => {
    const client = new FakeClient(emptyDashboardRoutes());
    await collectDashboard(client);
    const scopes = client.calls
      .filter(({ path, params }) => path === '/merge_requests' && params.with_merge_status_recheck)
      .map(({ params }) => params.scope);
    assert.deepEqual(scopes.sort(), ['assigned_to_me', 'created_by_me']);
  });
});

describe('collectDashboard review verdicts from activity notes', () => {
  const maintained = mr({ iid: 3, author: BOB });
  const myChangesNote = activityNote(ME, 'requested changes', '2026-01-02T00:00:00Z');
  const reRequestNote = activityNote(BOB, 'requested review from @me', '2026-01-03T00:00:00Z');
  const routes = (extra = {}) => ({
    ...emptyDashboardRoutes(),
    '/projects': [{ id: 10 }],
    '/projects/10/merge_requests': [maintained],
    ...extra,
  });
  const states = () => ({ 'group/project': { 3: { approvers: [], notes: 5, resolvable: 0, resolved: 0 } } });

  it('keeps a maintained MR where I asked for changes in the section but out of the badge', async () => {
    const client = new FakeClient(
      routes(),
      { graphql: withVerdicts(statesGraphql(states()), verdictsGraphql({ 'group/project': { 3: [myChangesNote] } })) },
    );

    const dashboard = await collectDashboard(client);

    assert.deepEqual(dashboard.sections.awaitingApproval.map((item) => [item.key, item.myReviewState]), [
      ['10!3', 'requested_changes'],
    ]);
    assert.equal(deriveView(dashboard).counts.waiting, 0);
    assert.deepEqual(dashboard.threadCache.verdictEntries, {
      '10!3': { updatedAt: '2026-01-01T00:00:00Z', verdict: 'requested_changes' },
    });
  });

  it('rescans only when the MR was updated, and resets the verdict on a new review request', async () => {
    const notesByPath = { 'group/project': { 3: [myChangesNote, reRequestNote] } };
    const counter = { verdictQueries: 0 };
    const graphql = () =>
      withVerdicts(statesGraphql(states()), (query, variables) => {
        counter.verdictQueries++;
        return verdictsGraphql(notesByPath)(query, variables);
      });
    const first = await collectDashboard(new FakeClient(routes(), { graphql: graphql() }));
    assert.equal(first.sections.awaitingApproval[0].myReviewState, null);
    assert.equal(deriveView(first).counts.waiting, 1);

    const second = await collectDashboard(new FakeClient(routes(), { graphql: graphql() }), {
      threadCache: first.threadCache,
    });
    assert.equal(counter.verdictQueries, 1);
    assert.deepEqual(second.sections.awaitingApproval.map((item) => [item.key, item.myReviewState]), [['10!3', null]]);

    const updated = mr({ iid: 3, author: BOB, updatedAt: '2026-01-04T00:00:00Z' });
    const third = await collectDashboard(
      new FakeClient(routes({ '/projects/10/merge_requests': [updated] }), { graphql: graphql() }),
      { threadCache: second.threadCache },
    );
    assert.equal(counter.verdictQueries, 2);
    assert.equal(third.sections.awaitingApproval[0].myReviewState, null);
  });

  it('keeps the verdict cached without rescans while the MR is unchanged', async () => {
    const counter = { verdictQueries: 0 };
    const graphql = withVerdicts(
      statesGraphql(states()),
      (query, variables) => {
        counter.verdictQueries++;
        return verdictsGraphql({ 'group/project': { 3: [myChangesNote] } })(query, variables);
      },
    );
    const first = await collectDashboard(new FakeClient(routes(), { graphql }));
    assert.equal(first.sections.awaitingApproval[0].myReviewState, 'requested_changes');
    assert.equal(counter.verdictQueries, 1);

    const second = await collectDashboard(new FakeClient(routes(), { graphql }), { threadCache: first.threadCache });
    assert.equal(counter.verdictQueries, 1);
    assert.equal(second.sections.awaitingApproval[0].myReviewState, 'requested_changes');
  });

  it('finds my requested changes on a reviewer MR whose interaction state was reset', async () => {
    const reviewing = mr({ iid: 1, updatedAt: '2026-01-05T00:00:00Z' });
    const client = new FakeClient(
      { ...emptyDashboardRoutes(), '/merge_requests': mergeRequestsRoute({ reviewing: [reviewing] }) },
      {
        graphql: withVerdicts(
          statesGraphql({ 'group/project': { 1: { reviewStates: { [ME.id]: 'UNREVIEWED' } } } }),
          verdictsGraphql({ 'group/project': { 1: [myChangesNote, activityNote(BOB, 'added 1 commit', '2026-01-03T00:00:00Z')] } }),
        ),
      },
    );

    const dashboard = await collectDashboard(client);

    assert.equal(dashboard.sections.reviewing[0].myReviewState, 'requested_changes');
    assert.equal(deriveView(dashboard).counts.waiting, 0);
  });

  it('drops a cached verdict of an updated MR when its notes could not be loaded', async () => {
    const first = await collectDashboard(
      new FakeClient(routes(), {
        graphql: withVerdicts(statesGraphql(states()), verdictsGraphql({ 'group/project': { 3: [myChangesNote] } })),
      }),
    );
    assert.equal(first.sections.awaitingApproval[0].myReviewState, 'requested_changes');

    const updated = mr({ iid: 3, author: BOB, updatedAt: '2026-01-04T00:00:00Z' });
    const second = await collectDashboard(
      new FakeClient(routes({ '/projects/10/merge_requests': [updated] }), {
        graphql: withVerdicts(statesGraphql(states()), () => {
          throw new GitLabError(502, 'Bad Gateway', '/graphql');
        }),
      }),
      { threadCache: first.threadCache },
    );

    assert.equal(second.sections.awaitingApproval[0].myReviewState, null);
    assert.equal(deriveView(second).counts.waiting, 1);
  });

  it('disables the verdict scan when the GitLab schema does not know the notes filter', async () => {
    const counter = { verdictQueries: 0 };
    const client = new FakeClient(
      routes(),
      {
        graphql: withVerdicts(
          statesGraphql(states()),
          (query) => {
            counter.verdictQueries++;
            throw new GitLabError(200, "GraphQL: Field 'notes' doesn't accept argument 'filter'", '/graphql');
          },
        ),
      },
    );

    const dashboard = await collectDashboard(client);

    assert.equal(counter.verdictQueries, 1);
    assert.equal(dashboard.sections.awaitingApproval[0].myReviewState, null);
    assert.deepEqual(dashboard.threadCache.verdictEntries, {});
    assert.equal(deriveView(dashboard).counts.waiting, 1);
  });
});
