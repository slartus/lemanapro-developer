import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { buildRows, filterCounts, matchesFilter, matchesSearch, rowSections } from '../../src/lpdev/web/static/board-model.js';

const watch = (overrides = {}) => ({
  gitlab_project_id: 10,
  iid: 1,
  title: 'Watch title',
  path_with_namespace: 'g/p',
  author_username: 'alice',
  source_branch: 'feature/x',
  updated_at: '2026-11-09T08:00:00Z',
  my_roles: ['reviewer'],
  latest_review_status: 'done',
  verdict: null,
  ...overrides,
});

const radarItem = (key, overrides = {}) => ({ key, kind: 'mr', title: `Radar ${key}`, updatedAt: '2026-11-09T09:00:00Z', ...overrides });

const view = (sections, snoozed = [], approvals = {}) => ({
  sections: { reviewing: [], assigned: [], authored: [], awaitingApproval: [], mentioned: [], threads: [], ...sections },
  snoozed,
  approvals,
});

describe('buildRows', () => {
  it('merges radar sections and the review row of the same MR into one row', () => {
    const rows = buildRows(
      view({
        reviewing: [radarItem('10!1', { approvedByMe: false })],
        threads: [radarItem('10!1', { threadsAwaitingReply: 2 })],
      }),
      [watch()],
    );
    assert.equal(rows.length, 1);
    const [row] = rows;
    assert.equal(row.key, '10!1');
    assert.deepEqual([...row.sections], ['reviewing', 'threads']);
    assert.equal(row.radar.threadsAwaitingReply, 2);
    assert.equal(row.watch.title, 'Watch title');
    assert.equal(row.updatedAt, '2026-11-09T09:00:00Z');
  });

  it('keeps MRs known to only one side and sorts by the latest update', () => {
    const rows = buildRows(view({ authored: [radarItem('20!5', { updatedAt: '2026-11-08T00:00:00Z' })] }), [watch()]);
    assert.deepEqual(rows.map((row) => row.key), ['10!1', '20!5']);
    assert.equal(rows[0].radar, null);
    assert.equal(rows[1].watch, null);
  });

  it('skips issues, hidden sections and snoozed MRs, including their review rows', () => {
    const rows = buildRows(
      view(
        {
          mentioned: [radarItem('10!7', { kind: 'issue' })],
          awaitingApproval: [radarItem('30!1')],
        },
        [{ key: '10!1' }],
      ),
      [watch()],
      ['awaitingApproval'],
    );
    assert.deepEqual(rows, []);
  });

  it('works before the radar has data', () => {
    assert.deepEqual(buildRows(null, [watch()]).map((row) => row.key), ['10!1']);
  });

  it('merges approval info from the radar into a watch-only row', () => {
    const approvals = { '10!1': { approvedByMe: true, approvalsCount: 2, approverNames: ['me', 'alice'] } };
    const [row] = buildRows(view({}, [], approvals), [watch()]);
    assert.equal(row.radar.approvedByMe, true);
    assert.equal(row.radar.approvalsCount, 2);
    assert.deepEqual(row.radar.approverNames, ['me', 'alice']);
  });
});

describe('rowSections', () => {
  it('prefers radar sections and falls back to review roles', () => {
    const [radarRow] = buildRows(view({ assigned: [radarItem('10!1')] }), [watch()]);
    assert.deepEqual(rowSections(radarRow), ['assigned']);
    const [watchRow] = buildRows(null, [watch({ my_roles: ['author', 'mentioned', 'unknown'] })]);
    assert.deepEqual(rowSections(watchRow), ['authored', 'mentioned']);
  });
});

describe('filters', () => {
  const rows = buildRows(
    view({ reviewing: [radarItem('10!1')], authored: [radarItem('10!2')] }),
    [watch(), watch({ iid: 2, verdict: 'REQUEST_CHANGES', latest_review_status: 'reviewing', my_roles: ['author'] })],
  );
  const actionable = new Set(['10!1']);

  it('filters by actionable keys, sections and review state', () => {
    const keysFor = (filter) => rows.filter((row) => matchesFilter(row, filter, actionable)).map((row) => row.key).sort();
    assert.deepEqual(keysFor('action'), ['10!1']);
    assert.deepEqual(keysFor('all'), ['10!1', '10!2']);
    assert.deepEqual(keysFor('authored'), ['10!2']);
    assert.deepEqual(keysFor('changes'), ['10!2']);
    assert.deepEqual(keysFor('reviewRunning'), ['10!2']);
  });

  it('counts every filter', () => {
    const counts = filterCounts(rows, actionable);
    assert.equal(counts.all, 2);
    assert.equal(counts.action, 1);
    assert.equal(counts.reviewing, 1);
    assert.equal(counts.mentioned, 0);
  });

  it('searches titles, projects, authors and branches case-insensitively', () => {
    const [row] = buildRows(null, [watch()]);
    assert.equal(matchesSearch(row, ''), true);
    assert.equal(matchesSearch(row, 'G/P'), true);
    assert.equal(matchesSearch(row, 'FEATURE/x'), true);
    assert.equal(matchesSearch(row, '@alice'), false);
    assert.equal(matchesSearch(row, 'alice'), true);
    assert.equal(matchesSearch(row, 'nothing'), false);
  });
});

describe('visibleActionable', () => {
  it('ignores hidden sections like the extension badge did', async () => {
    const { actionableKeys } = await import('../../radar/src/view.js');
    const { visibleActionable } = await import('../../src/lpdev/web/static/board-model.js');
    const sections = {
      reviewing: [],
      assigned: [],
      mentioned: [{ key: '10!1' }],
      awaitingApproval: [],
      threads: [{ key: '10!2' }],
      authored: [{ key: '10!1' }, { key: '10!3', pipeline: { status: 'failed' } }],
    };
    assert.deepEqual([...visibleActionable({ sections }, ['mentioned', 'threads'], actionableKeys)], ['10!3']);
    assert.deepEqual([...visibleActionable({ sections }, [], actionableKeys)].sort(), ['10!1', '10!2', '10!3']);
    assert.deepEqual([...visibleActionable(null, [], actionableKeys)], []);
  });
});

describe('finishedReviewMarks', () => {
  it('marks finished and failed reviews by id and status', async () => {
    const { finishedReviewMarks } = await import('../../src/lpdev/web/static/board-model.js');
    const marks = finishedReviewMarks([
      watch({ latest_review_id: 5, latest_review_status: 'done' }),
      watch({ latest_review_id: 6, latest_review_status: 'error' }),
      watch({ latest_review_id: 7, latest_review_status: 'reviewing' }),
      watch({ latest_review_id: null, latest_review_status: null }),
    ]);
    assert.deepEqual([...marks], ['5:done', '6:error']);
  });
});

describe('reviewError filter', () => {
  it('selects rows whose latest review failed', () => {
    const rows = buildRows(null, [watch({ latest_review_status: 'error' }), watch({ iid: 2 })]);
    assert.deepEqual(rows.filter((row) => matchesFilter(row, 'reviewError', new Set())).map((row) => row.key), ['10!1']);
  });
});
