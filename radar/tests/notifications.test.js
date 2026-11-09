import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  NOTIFICATION_TYPES,
  enabledEvents,
  formatNotification,
  isQuietTime,
  mergeEvents,
  notificationTarget,
} from '../src/notifications.js';
import { SECTION_IDS, collectNotificationEvents, sortPendingEvents, withStableStatuses } from '../src/view.js';

const BASE_URL = 'https://gitlab.example.com';
const NOW = Date.parse('2026-03-10T15:00:00');

const mrItem = (key, extra = {}) => ({
  key,
  title: `MR ${key}`,
  webUrl: `${BASE_URL}/group/project/-/merge_requests/${key}`,
  updatedAt: '2026-03-10T10:00:00Z',
  approvedByMe: false,
  draft: false,
  ...extra,
});

const dashboard = (sections, extra = {}) => ({
  baseUrl: BASE_URL,
  username: 'me',
  filters: null,
  sections: { ...Object.fromEntries(SECTION_IDS.map((id) => [id, []])), ...sections },
  ...extra,
});
const eventsOf = (events) => events.map(({ type, item }) => `${type}:${item.key}`);
const changes = (previous, next, userState = {}) => eventsOf(collectNotificationEvents(previous, next, userState, NOW));

describe('collectNotificationEvents', () => {
  it('reports new MRs for review, mentions, approvals and threads', () => {
    const previous = dashboard({ reviewing: [mrItem('1')] });
    const next = dashboard({
      reviewing: [mrItem('1'), mrItem('2'), mrItem('3', { draft: true })],
      mentioned: [mrItem('4', { mentionCount: 1 })],
      awaitingApproval: [mrItem('5')],
      threads: [mrItem('6', { threadsAwaitingReply: 1 })],
    });
    assert.deepEqual(changes(previous, next), [
      'review:2',
      'mention:4',
      'approval:5',
      'thread:6',
    ]);
  });

  it('reports MRs newly assigned to me', () => {
    const previous = dashboard({ assigned: [mrItem('1')] });
    const next = dashboard({ assigned: [mrItem('1'), mrItem('2'), mrItem('3', { approvedByMe: true })] });
    assert.deepEqual(changes(previous, next), ['assigned:2']);
    assert.deepEqual(changes({ ...previous, assignedUnavailable: true }, next), []);
  });

  it('stays silent when an MR moves out of my assigned MRs because they could not be loaded', () => {
    const previous = dashboard({ assigned: [mrItem('1')] });
    const next = dashboard(
      { awaitingApproval: [mrItem('1')], mentioned: [mrItem('1', { mentionCount: 1 })] },
      { assignedUnavailable: true },
    );
    assert.deepEqual(changes(previous, next), []);
  });

  it('skips sections the previous dashboard did not have', () => {
    const previous = { baseUrl: BASE_URL, username: 'me', filters: null, sections: { reviewing: [] } };
    const next = dashboard({ assigned: [mrItem('1')], reviewing: [mrItem('2')] });
    assert.deepEqual(changes(previous, next), ['review:2']);
  });

  it('reports an approval again when the author asks for it after my requested changes', () => {
    const previous = dashboard({ awaitingApproval: [mrItem('1', { myReviewState: 'requested_changes' })] });
    const asked = dashboard({ awaitingApproval: [mrItem('1', { myReviewState: null })] });
    assert.deepEqual(changes(previous, asked), ['approval:1']);
    assert.deepEqual(changes(asked, asked), []);
  });

  it('reports a review again when the author asks for it after my requested changes', () => {
    const previous = dashboard({ reviewing: [mrItem('1', { myReviewState: 'requested_changes' })] });
    const asked = dashboard({ reviewing: [mrItem('1', { myReviewState: 'unreviewed' })] });
    assert.deepEqual(changes(previous, asked), ['review:1']);
    assert.deepEqual(changes(dashboard({ reviewing: [] }), previous), []);
  });

  it('keeps my last known review state while GitLab does not report it', () => {
    const previous = dashboard({
      reviewing: [mrItem('1', { kind: 'mr', myReviewState: 'reviewed' }), mrItem('2', { kind: 'mr', myReviewState: null })],
    });
    const stored = JSON.parse(JSON.stringify(previous));
    assert.equal(stored.sections.reviewing[1].myReviewState, null);
    assert.deepEqual(withStableStatuses(stored, dashboard({ reviewing: [mrItem('1', { kind: 'mr' })] })).sections.reviewing[0].myReviewState, 'reviewed');
    const unknown = withStableStatuses(previous, dashboard({ reviewing: [mrItem('1', { kind: 'mr' }), mrItem('2', { kind: 'mr' })] }));
    assert.deepEqual(unknown.sections.reviewing.map((item) => item.myReviewState), ['reviewed', null]);
    assert.deepEqual(changes(previous, unknown), []);
    const reset = withStableStatuses(
      unknown,
      dashboard({ reviewing: [mrItem('1', { kind: 'mr', myReviewState: null }), mrItem('2', { kind: 'mr', myReviewState: null })] }),
    );
    assert.deepEqual(changes(unknown, reset), ['review:1']);
  });

  it('reports a review again when my approval was reset', () => {
    const previous = dashboard({ reviewing: [mrItem('1', { approvedByMe: true })] });
    const next = dashboard({ reviewing: [mrItem('1')] });
    assert.deepEqual(changes(previous, next), ['review:1']);
  });

  it('reports repeated mentions and new replies in known threads', () => {
    const previous = dashboard({
      mentioned: [
        mrItem('1', { mentionCount: 1, mentionedAt: '2026-03-10T10:00:00Z' }),
        mrItem('4', { mentionCount: 1, mentionedAt: '2026-03-10T10:00:00Z' }),
      ],
      threads: [mrItem('2', { threadsAwaitingReply: 1 }), mrItem('3', { threadsAwaitingReply: 2 })],
    });
    const next = dashboard({
      mentioned: [
        mrItem('1', { mentionCount: 2, mentionedAt: '2026-03-10T10:00:00Z' }),
        mrItem('4', { mentionCount: 1, mentionedAt: '2026-03-10T12:00:00Z' }),
      ],
      threads: [mrItem('2', { threadsAwaitingReply: 2 }), mrItem('3', { threadsAwaitingReply: 1 })],
    });
    assert.deepEqual(changes(previous, next), ['mention:1', 'mention:4', 'thread:2']);
  });

  it('reports a newly failed pipeline and an MR that became mergeable', () => {
    const failed = (id, extra = {}) => ({ status: 'failed', id, onlyIgnoredFailures: false, ...extra });
    const previous = dashboard({
      authored: [
        mrItem('1', { pipeline: { status: 'running', id: 1 } }),
        mrItem('2', { pipeline: failed(2) }),
        mrItem('3', { pipeline: failed(3) }),
        mrItem('4', { status: 'not_approved' }),
        mrItem('5', { status: 'mergeable' }),
      ],
    });
    const next = dashboard({
      authored: [
        mrItem('1', { pipeline: failed(1) }),
        mrItem('2', { pipeline: failed(2) }),
        mrItem('3', { pipeline: failed(30) }),
        mrItem('4', { status: 'mergeable' }),
        mrItem('5', { status: 'mergeable' }),
        mrItem('6', { pipeline: failed(6, { onlyIgnoredFailures: true }) }),
        mrItem('7', { status: 'mergeable' }),
      ],
    });
    assert.deepEqual(changes(previous, next), [
      'ciFailed:1',
      'ciFailed:3',
      'mergeable:4',
    ]);
  });

  it('reports a mergeable MR only once its pipeline has passed', () => {
    const pipeline = (status) => ({ status, id: 1 });
    const running = dashboard({
      authored: [
        mrItem('1', { status: 'not_approved', pipeline: pipeline('running') }),
        mrItem('2', { status: 'mergeable', pipeline: pipeline('running') }),
        mrItem('3', { status: 'mergeable', pipeline: pipeline('running') }),
      ],
    });
    const mergeableRunning = dashboard({
      authored: [
        mrItem('1', { status: 'mergeable', pipeline: pipeline('running') }),
        mrItem('2', { status: 'mergeable', pipeline: pipeline('success') }),
        mrItem('3', { status: 'mergeable', pipeline: pipeline('canceled') }),
      ],
    });
    assert.deepEqual(changes(running, mergeableRunning), ['mergeable:2']);
    const passed = dashboard({ authored: [mrItem('1', { status: 'mergeable', pipeline: pipeline('success') })] });
    assert.deepEqual(changes(mergeableRunning, passed), ['mergeable:1']);
    assert.deepEqual(changes({ ...mergeableRunning, pipelinesUnavailable: true }, passed), []);
    assert.deepEqual(changes(mergeableRunning, { ...passed, pipelinesUnavailable: true }), []);
  });

  it('treats the legacy can_be_merged status as mergeable', () => {
    const previous = dashboard({ authored: [mrItem('1', { status: 'not_approved' })] });
    const next = dashboard({ authored: [mrItem('1', { status: 'can_be_merged' })] });
    assert.deepEqual(changes(previous, next), ['mergeable:1']);
  });

  it('does not report a mergeable MR again after GitLab rechecks its status', () => {
    const first = dashboard({ authored: [mrItem('1', { status: 'mergeable' })] });
    const checking = withStableStatuses(first, dashboard({ authored: [mrItem('1', { status: 'checking' })] }));
    const again = withStableStatuses(checking, dashboard({ authored: [mrItem('1', { status: 'mergeable' })] }));
    assert.equal(checking.sections.authored[0].status, 'mergeable');
    assert.deepEqual(changes(first, checking), []);
    assert.deepEqual(changes(checking, again), []);
  });

  it('keeps the stable status across several rechecks, including the legacy one', () => {
    const first = dashboard({ authored: [mrItem('1', { status: 'can_be_merged' })] });
    const second = withStableStatuses(first, dashboard({ authored: [mrItem('1', { status: 'cannot_be_merged_recheck' })] }));
    const third = withStableStatuses(second, dashboard({ authored: [mrItem('1', { status: 'unchecked' })] }));
    assert.equal(third.sections.authored[0].status, 'can_be_merged');
  });

  it('keeps a transient status when nothing stable is known', () => {
    const previous = dashboard({ authored: [mrItem('1', { status: 'unchecked' })] });
    const next = withStableStatuses(
      previous,
      dashboard({ authored: [mrItem('1', { status: 'checking' }), mrItem('2', { status: 'checking' })] }),
    );
    assert.deepEqual(next.sections.authored.map((item) => item.status), ['checking', 'checking']);
    const stable = dashboard({ authored: [mrItem('1', { status: 'mergeable' })] });
    const checking = dashboard({ authored: [mrItem('1', { status: 'checking' })] });
    [{ username: 'other' }, { baseUrl: 'https://other.example.com' }, { filters: { selectedProjectIds: [1] } }].forEach(
      (source) => {
        assert.equal(withStableStatuses({ ...stable, ...source }, checking).sections.authored[0].status, 'checking');
      },
    );
  });

  it('reports a default branch that turned red', () => {
    const branch = (key) => ({
      key,
      title: `group/${key}`,
      webUrl: `${BASE_URL}/group/${key}/-/pipelines/1`,
      updatedAt: 't',
    });
    const previous = dashboard({ brokenBranches: [branch('a')] });
    const next = dashboard({ brokenBranches: [branch('a'), branch('b')] });
    assert.deepEqual(changes(previous, next), ['branchBroken:b']);
    assert.deepEqual(changes({ ...previous, defaultBranchesUnavailable: true }, next), []);
  });

  it('stays silent about snoozed MRs, including when the snooze expires', () => {
    const userState = { snoozed: { 2: { until: '2026-03-10T16:00:00' }, 3: { until: '2026-03-10T14:00:00' } } };
    const previous = dashboard({ reviewing: [mrItem('3')] });
    const next = dashboard({ reviewing: [mrItem('2'), mrItem('3')] });
    assert.deepEqual(changes(previous, next, userState), []);
  });

  it('compares an MR snoozed until change with its real previous state', () => {
    const userState = { snoozed: { 1: { updatedAt: '2026-03-10T10:00:00Z' }, 2: { updatedAt: '2026-03-10T10:00:00Z' } } };
    const failed = { status: 'failed', id: 1, onlyIgnoredFailures: false };
    const previous = dashboard({ authored: [mrItem('1', { pipeline: failed })], awaitingApproval: [mrItem('2')] });
    const next = dashboard({
      authored: [mrItem('1', { pipeline: failed, updatedAt: '2026-03-10T11:00:00Z' })],
      awaitingApproval: [mrItem('2', { updatedAt: '2026-03-10T11:00:00Z' })],
    });
    assert.deepEqual(changes(previous, next, userState), []);
  });

  it('skips rules whose source was unavailable on either side', () => {
    const failed = { status: 'failed', id: 1, onlyIgnoredFailures: false };
    const previous = dashboard(
      { authored: [mrItem('1', { pipeline: null })] },
      { pipelinesUnavailable: true, mentionsUnavailable: true },
    );
    const next = dashboard({ authored: [mrItem('1', { pipeline: failed })], mentioned: [mrItem('2', { mentionCount: 1 })] });
    assert.deepEqual(changes(previous, next), []);
  });

  it('reports only changes of known MRs after an incomplete refresh', () => {
    const previous = dashboard({ threads: [mrItem('1', { threadsAwaitingReply: 1 })] }, { skippedMrs: 1 });
    const next = dashboard({
      reviewing: [mrItem('2')],
      threads: [mrItem('1', { threadsAwaitingReply: 2 }), mrItem('3', { threadsAwaitingReply: 1 })],
    });
    assert.deepEqual(changes(previous, next), ['thread:1']);
  });

  it('stays silent on the first refresh and when the user, instance or filters change', () => {
    const next = dashboard({ reviewing: [mrItem('1')] });
    assert.deepEqual(changes(undefined, next), []);
    assert.deepEqual(changes({ error: 'x' }, next), []);
    assert.deepEqual(changes(dashboard({}, { username: 'other' }), next), []);
    assert.deepEqual(changes(dashboard({}, { baseUrl: 'https://other.example.com' }), next), []);
    assert.deepEqual(changes(dashboard({}, { filters: { selectedProjectIds: [1] } }), next), []);
  });

  it('stays silent when the approval threshold changes and reports as usual while it stays the same', () => {
    const filters = (approvalThreshold) => ({ selectedProjectIds: null, ignoredCheckNames: [], approvalThreshold });
    const next = dashboard({ awaitingApproval: [mrItem('1')] }, { filters: filters(2) });
    assert.deepEqual(changes(dashboard({}, { filters: { selectedProjectIds: null, ignoredCheckNames: [] } }), next), []);
    assert.deepEqual(changes(dashboard({}, { filters: filters(0) }), next), []);
    assert.deepEqual(changes(dashboard({}, { filters: filters(2) }), next), ['approval:1']);
  });
});

describe('enabledEvents', () => {
  it('drops events of muted types', () => {
    const events = [
      { type: 'review', item: mrItem('1') },
      { type: 'approval', item: mrItem('2') },
      { type: 'ciFailed', item: mrItem('3') },
    ];
    assert.deepEqual(eventsOf(enabledEvents(events, ['approval', 'ciFailed'])), ['review:1']);
    assert.deepEqual(eventsOf(enabledEvents(events)), ['review:1', 'approval:2', 'ciFailed:3']);
  });

  it('offers every event type in settings', () => {
    assert.deepEqual(
      NOTIFICATION_TYPES.map(({ type }) => type),
      ['review', 'assigned', 'mention', 'approval', 'thread', 'ciFailed', 'mergeable', 'branchBroken'],
    );
  });
});

describe('isQuietTime', () => {
  const quiet = (extra = {}) => ({ enabled: true, from: '20:00', to: '09:00', weekends: false, ...extra });
  const at = (time) => new Date(`2026-03-10T${time}:00`);

  it('covers a period that crosses midnight', () => {
    assert.equal(isQuietTime(quiet(), at('19:59')), false);
    assert.equal(isQuietTime(quiet(), at('20:00')), true);
    assert.equal(isQuietTime(quiet(), at('03:00')), true);
    assert.equal(isQuietTime(quiet(), at('09:00')), false);
  });

  it('covers a period within one day', () => {
    const lunch = quiet({ from: '13:00', to: '14:00' });
    assert.equal(isQuietTime(lunch, at('13:30')), true);
    assert.equal(isQuietTime(lunch, at('14:00')), false);
    assert.equal(isQuietTime(lunch, at('12:59')), false);
  });

  it('joins weekends with the night before Monday', () => {
    const nightsAndWeekends = quiet({ weekends: true });
    assert.equal(isQuietTime(nightsAndWeekends, new Date('2026-03-15T23:00:00')), true);
    assert.equal(isQuietTime(nightsAndWeekends, new Date('2026-03-16T08:59:00')), true);
    assert.equal(isQuietTime(nightsAndWeekends, new Date('2026-03-16T09:00:00')), false);
    assert.equal(isQuietTime(nightsAndWeekends, new Date('2026-03-13T19:00:00')), false);
  });

  it('covers whole weekends when asked and nothing when disabled or malformed', () => {
    const saturdayNoon = new Date('2026-03-14T12:00:00');
    assert.equal(isQuietTime(quiet({ weekends: true }), saturdayNoon), true);
    assert.equal(isQuietTime(quiet(), saturdayNoon), false);
    assert.equal(isQuietTime(quiet({ enabled: false, weekends: true }), saturdayNoon), false);
    assert.equal(isQuietTime(quiet({ from: '10:00', to: '10:00' }), at('10:00')), false);
    assert.equal(isQuietTime(quiet({ from: 'x' }), at('22:00')), false);
    assert.equal(isQuietTime(undefined, at('22:00')), false);
  });
});

describe('deferred events', () => {
  it('merges events by type and MR, keeping the newer copy', () => {
    const merged = mergeEvents(
      [{ type: 'review', item: mrItem('1') }, { type: 'thread', item: mrItem('2', { threadsAwaitingReply: 1 }) }],
      [{ type: 'thread', item: mrItem('2', { threadsAwaitingReply: 2 }) }, { type: 'review', item: mrItem('3') }],
    );
    assert.deepEqual(eventsOf(merged), ['review:1', 'thread:2', 'review:3']);
    assert.equal(merged[1].item.threadsAwaitingReply, 2);
  });

  it('keeps only events that still apply to the current dashboard', () => {
    const events = [
      { type: 'review', item: mrItem('1') },
      { type: 'review', item: mrItem('2') },
      { type: 'mergeable', item: mrItem('3', { status: 'mergeable' }) },
      { type: 'approval', item: mrItem('4') },
      { type: 'unknown', item: mrItem('5') },
    ];
    const current = dashboard({
      reviewing: [mrItem('1', { title: 'renamed' }), mrItem('2', { approvedByMe: true })],
      authored: [mrItem('3', { status: 'not_approved' })],
      awaitingApproval: [mrItem('4')],
    });
    const { relevant, waiting } = sortPendingEvents(
      events,
      current,
      { snoozed: { 4: { until: '2026-03-10T16:00:00' } } },
      NOW,
    );
    assert.deepEqual(eventsOf(relevant), ['review:1']);
    assert.deepEqual(waiting, []);
    assert.equal(relevant[0].item.title, 'renamed');
    assert.deepEqual(sortPendingEvents(events, { error: 'x' }, {}, NOW), { relevant: [], waiting: [] });
  });

  it('keeps events whose source is unavailable until it loads again', () => {
    const failed = { status: 'failed', id: 1, onlyIgnoredFailures: false };
    const events = [{ type: 'ciFailed', item: mrItem('1', { pipeline: failed }) }, { type: 'review', item: mrItem('2') }];
    const partial = dashboard({ authored: [mrItem('1', { pipeline: null })] }, { pipelinesUnavailable: true });
    const { relevant, waiting } = sortPendingEvents(events, partial, {}, NOW);
    assert.deepEqual(eventsOf(waiting), ['ciFailed:1']);
    assert.deepEqual(relevant, []);
  });
});

describe('formatNotification', () => {
  const events = [
    { type: 'review', item: mrItem('1') },
    { type: 'review', item: mrItem('2') },
    { type: 'mention', item: mrItem('3', { mentionUrl: `${BASE_URL}/x#note_1` }) },
  ];

  it('hides MR titles unless details are enabled', () => {
    const notification = formatNotification(events, { showDetails: false, iconUrl: 'icon.png' });
    assert.equal(notification.type, 'basic');
    assert.equal(notification.message, 'на ревью: 2 · упоминаний: 1');
    assert.ok(!JSON.stringify(notification).includes('MR 1'));
    assert.equal(notification.priority, 1);
  });

  it('lists MR titles when details are enabled', () => {
    const notification = formatNotification(events, { showDetails: true, iconUrl: 'icon.png' });
    assert.equal(notification.type, 'list');
    assert.deepEqual(notification.items[2], { title: 'Упоминание', message: 'MR 3' });
  });
});

describe('notificationTarget', () => {
  it('opens the MR, or the mention note, when the notification is about one MR', () => {
    assert.equal(notificationTarget([{ type: 'review', item: mrItem('1') }], BASE_URL), mrItem('1').webUrl);
    assert.equal(
      notificationTarget([{ type: 'mention', item: mrItem('1', { mentionUrl: `${BASE_URL}/x#note_1` }) }], BASE_URL),
      `${BASE_URL}/x#note_1`,
    );
  });

  it('has no single target for several MRs or links outside GitLab', () => {
    assert.equal(
      notificationTarget([{ type: 'review', item: mrItem('1') }, { type: 'review', item: mrItem('2') }], BASE_URL),
      null,
    );
    assert.equal(
      notificationTarget([{ type: 'review', item: mrItem('1', { webUrl: 'https://evil.example/x' }) }], BASE_URL),
      null,
    );
  });
});
