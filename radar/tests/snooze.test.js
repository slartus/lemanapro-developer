import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { createSnooze, isSnoozeActive, nextSnoozeExpiry, pruneSnoozed, snoozeOptions } from '../src/snooze.js';
import { deriveView } from '../src/view.js';

const NOW = Date.parse('2026-03-10T15:00:00');
const item = (key, updatedAt = '2026-03-10T10:00:00Z', extra = {}) => ({ key, updatedAt, ...extra });

describe('snooze', () => {
  it('snoozes for an hour', () => {
    const snooze = createSnooze('hour', item('a'), NOW);
    assert.equal(isSnoozeActive(snooze, item('a'), NOW + 59 * 60 * 1000), true);
    assert.equal(isSnoozeActive(snooze, item('a'), NOW + 60 * 60 * 1000), false);
  });

  it('snoozes until the next morning in local time', () => {
    const until = new Date(createSnooze('tomorrow', item('a'), NOW).until);
    assert.equal(until.getDate(), 11);
    assert.equal(until.getHours(), 9);
    assert.equal(until.getMinutes(), 0);
  });

  it('skips the weekend when snoozing until the morning', () => {
    const untilDate = (now) => new Date(createSnooze('tomorrow', item('a'), Date.parse(now)).until).getDate();
    assert.equal(untilDate('2026-03-13T18:00:00'), 16);
    assert.equal(untilDate('2026-03-14T12:00:00'), 16);
    assert.equal(untilDate('2026-03-15T23:00:00'), 16);
  });

  it('snoozes until the next Monday morning', () => {
    const until = (now) => new Date(createSnooze('monday', item('a'), Date.parse(now)).until);
    assert.equal(until('2026-03-10T15:00:00').getDate(), 16);
    assert.equal(until('2026-03-16T08:00:00').getDate(), 23);
    assert.equal(until('2026-03-15T23:00:00').getHours(), 9);
  });

  it('snoozes until the MR changes', () => {
    const snooze = createSnooze('untilChange', item('a', '2026-03-10T10:00:00Z'), NOW);
    assert.equal(isSnoozeActive(snooze, item('a', '2026-03-10T10:00:00Z'), NOW), true);
    assert.equal(isSnoozeActive(snooze, item('a', '2026-03-10T10:00:01Z'), NOW), false);
  });

  it('mutes forever', () => {
    const snooze = createSnooze('forever', item('a'), NOW);
    assert.deepEqual(snooze, { forever: true });
    assert.equal(isSnoozeActive(snooze, item('a'), NOW), true);
    assert.equal(isSnoozeActive(snooze, item('a'), NOW + 365 * 24 * 60 * 60 * 1000), true);
  });

  it('shows the forever option only for possible MRs', () => {
    assert.deepEqual(Object.keys(snoozeOptions('possibleMrs')), [
      'hour',
      'tomorrow',
      'monday',
      'untilChange',
      'forever',
    ]);
    assert.deepEqual(Object.keys(snoozeOptions('reviewing')), [
      'hour',
      'tomorrow',
      'monday',
      'untilChange',
    ]);
  });

  it('rejects unknown options and treats empty entries as inactive', () => {
    assert.throws(() => createSnooze('nope', item('a'), NOW));
    assert.equal(isSnoozeActive(undefined, item('a'), NOW), false);
    assert.equal(isSnoozeActive({}, item('a'), NOW), false);
  });

  it('finds the next timed snooze expiry', () => {
    const snoozed = {
      a: { until: '2026-03-10T18:00:00' },
      b: { until: '2026-03-10T16:00:00' },
      c: { until: '2026-03-10T14:00:00' },
      d: { updatedAt: '2026-03-10T10:00:00Z' },
    };
    assert.equal(nextSnoozeExpiry(snoozed, NOW), Date.parse('2026-03-10T16:00:00'));
    assert.equal(nextSnoozeExpiry({ d: snoozed.d }, NOW), null);
    assert.equal(nextSnoozeExpiry(undefined, NOW), null);
    assert.equal(nextSnoozeExpiry({ broken: null }, NOW), null);
    assert.equal(nextSnoozeExpiry({ a: { forever: true } }, NOW), null);
    assert.equal(nextSnoozeExpiry({ a: { forever: true }, b: { until: '2026-03-10T16:00:00' } }, NOW), Date.parse('2026-03-10T16:00:00'));
  });

  it('prunes expired snoozes and MRs that are gone, keeping forever mutes', () => {
    const items = new Map([
      ['active', item('active')],
      ['expired', item('expired')],
      ['changed', item('changed', '2026-03-10T12:00:00Z')],
      ['forever', item('forever')],
    ]);
    const pruned = pruneSnoozed(
      {
        active: { until: '2026-03-10T16:00:00' },
        expired: { until: '2026-03-10T14:00:00' },
        changed: { updatedAt: '2026-03-10T10:00:00Z' },
        gone: { until: '2026-03-10T16:00:00' },
        forever: { forever: true },
        goneForever: { forever: true },
      },
      items,
      NOW,
    );
    assert.deepEqual(Object.keys(pruned), ['active', 'forever', 'goneForever']);
  });
});

describe('deriveView with snoozed MRs', () => {
  const dashboard = {
    sections: {
      reviewing: [item('a', undefined, { approvedByMe: false, draft: false })],
      mentioned: [],
      awaitingApproval: [item('b')],
      threads: [item('a', undefined, { threadsAwaitingReply: 1 })],
      authored: [item('c', undefined, { pipeline: { status: 'failed' } })],
    },
  };

  it('hides a snoozed MR from every section and from the badge', () => {
    const view = deriveView(dashboard, { snoozed: { a: { until: '2026-03-10T16:00:00' } } }, NOW);
    assert.deepEqual(view.sections.reviewing, []);
    assert.deepEqual(view.sections.threads, []);
    assert.deepEqual(view.counts, { waiting: 1, failedCi: 1 });
    assert.deepEqual(view.snoozed.map((snoozedItem) => snoozedItem.key), ['a']);
    assert.deepEqual(view.snoozed[0].snooze, { until: '2026-03-10T16:00:00' });
  });

  it('brings the MR back once the snooze expires', () => {
    const view = deriveView(dashboard, { snoozed: { c: { until: '2026-03-10T14:00:00' } } }, NOW);
    assert.equal(view.counts.failedCi, 1);
    assert.deepEqual(view.snoozed, []);
  });

  it('hides a forever-muted branch and surfaces it as snoozed forever', () => {
    const branch = item('branch:1:feature', undefined, { kind: 'possibleMr' });
    const view = deriveView({ sections: { possibleMrs: [branch] } }, { snoozed: { 'branch:1:feature': { forever: true } } }, NOW);
    assert.deepEqual(view.sections.possibleMrs, []);
    assert.deepEqual(view.snoozed.map(({ key, snooze }) => ({ key, snooze })), [
      { key: 'branch:1:feature', snooze: { forever: true } },
    ]);
  });
});
