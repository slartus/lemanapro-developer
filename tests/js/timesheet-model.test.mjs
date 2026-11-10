import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  buildSubmitEntries,
  cellState,
  cellValue,
  dayTotals,
  formatActivity,
  formatHours,
  groupEntriesByDay,
  hiddenDays,
  isEditableDay,
  isSafeUrl,
  isUncertain,
  isValidIssueKey,
  parseHours,
  retryEntries,
  roundToStep,
  rowHasHours,
  rowTotals,
  splitRows,
  stepMinutes,
  stepOf,
  summarizeResults,
  visibleDays,
  workedOn,
} from '../../src/lpdev/web/static/timesheet-model.js';

const DATES = ['2026-11-09', '2026-11-10', '2026-11-11', '2026-11-12', '2026-11-13', '2026-11-14', '2026-11-15'];

const days = (overrides = {}) =>
  DATES.map((date, index) => ({
    date,
    kind: index >= 5 ? 'weekend' : 'work',
    norm: index >= 5 ? 0 : 480,
    logged: 0,
    submittable: index < 5,
    ...(overrides[date] ?? {}),
  }));

const cell = (overrides = {}) => ({ suggested: 0, edited: null, logged: 0, submission: null, ...overrides });

const row = (key, cells, overrides = {}) => ({
  key,
  title: `Title ${key}`,
  url: `https://tracker.example/${key}`,
  sources: ['push'],
  cells,
  ...overrides,
});

const data = (overrides = {}) => ({
  week: '2026-W46',
  prev_week: '2026-W45',
  next_week: '2026-W47',
  today: '2026-11-13',
  calendar_ok: true,
  tracker: { enabled: true, submit_enabled: true, error: null },
  days: days(),
  rows: [],
  unkeyed: null,
  ...overrides,
});

describe('parseHours', () => {
  it('parses dot and comma decimals into minutes', () => {
    assert.equal(parseHours('1.5'), 90);
    assert.equal(parseHours('1,5'), 90);
    assert.equal(parseHours('.5'), 30);
    assert.equal(parseHours('2'), 120);
    assert.equal(parseHours(' 3 '), 180);
    assert.equal(parseHours('2ч'), 120);
  });

  it('rounds to the nearest half hour', () => {
    assert.equal(parseHours('1.2'), 60);
    assert.equal(parseHours('1.25'), 90);
    assert.equal(parseHours('0.1'), 0);
  });

  it('treats empty input as zero', () => {
    assert.equal(parseHours(''), 0);
    assert.equal(parseHours('   '), 0);
    assert.equal(parseHours(null), 0);
  });

  it('rejects garbage, negatives and more than a day', () => {
    assert.equal(parseHours('abc'), null);
    assert.equal(parseHours('-1'), null);
    assert.equal(parseHours('1.5.5'), null);
    assert.equal(parseHours('1,5,5'), null);
    assert.equal(parseHours('25'), null);
    assert.equal(parseHours('24'), 1440);
  });
});

describe('formatHours', () => {
  it('formats minutes as short decimal hours', () => {
    assert.equal(formatHours(90), '1.5');
    assert.equal(formatHours(60), '1');
    assert.equal(formatHours(0), '0');
    assert.equal(formatHours(45), '0.75');
    assert.equal(formatHours(null), '0');
  });
});

describe('roundToStep', () => {
  it('rounds to the step and clamps negatives to zero', () => {
    assert.equal(roundToStep(44), 30);
    assert.equal(roundToStep(45), 60);
    assert.equal(roundToStep(-30), 0);
    assert.equal(roundToStep(NaN), 0);
    assert.equal(roundToStep(70, 60), 60);
  });
});

describe('stepMinutes', () => {
  it('moves to the next half-hour mark in either direction', () => {
    assert.equal(stepMinutes(60, 1), 90);
    assert.equal(stepMinutes(60, -1), 30);
    assert.equal(stepMinutes(45, 1), 60);
    assert.equal(stepMinutes(45, -1), 30);
    assert.equal(stepMinutes(0, -1), 0);
    assert.equal(stepMinutes(1440, 1), 1440);
  });
});

describe('isValidIssueKey', () => {
  it('accepts tracker keys and rejects the rest', () => {
    assert.equal(isValidIssueKey('LMWORK-123'), true);
    assert.equal(isValidIssueKey('A_B2-1'), true);
    assert.equal(isValidIssueKey('lmwork-1'), false);
    assert.equal(isValidIssueKey('A-1'), false);
    assert.equal(isValidIssueKey('LMWORK-'), false);
    assert.equal(isValidIssueKey('LMWORK-1 '), false);
    assert.equal(isValidIssueKey(null), false);
  });
});

describe('cellValue', () => {
  it('prefers the edit over the suggestion', () => {
    assert.equal(cellValue(cell({ suggested: 60 })), 60);
    assert.equal(cellValue(cell({ suggested: 60, edited: 0 })), 0);
    assert.equal(cellValue(cell({ suggested: 60, edited: 120 })), 120);
    assert.equal(cellValue(undefined), 0);
  });
});

describe('dayTotals', () => {
  it('adds logged time and planned values of keyed rows only', () => {
    const totals = dayTotals(
      data({
        days: days({ '2026-11-09': { logged: 120 }, '2026-11-14': { logged: 30 } }),
        rows: [
          row('A-1', { '2026-11-09': cell({ suggested: 240 }), '2026-11-10': cell({ suggested: 60, edited: 300 }) }),
          row('B-2', { '2026-11-09': cell({ suggested: 180 }) }),
        ],
        unkeyed: { items: [{ title: 'x', url: 'https://x' }], cells: { '2026-11-09': { suggested: 600 } } },
      }),
    );
    assert.deepEqual(totals[0], { date: '2026-11-09', logged: 120, planned: 420, total: 540, norm: 480, over: 60, under: 0, unkeyed: 600 });
    assert.deepEqual(totals[1], { date: '2026-11-10', logged: 0, planned: 300, total: 300, norm: 480, over: 0, under: 180, unkeyed: 0 });
    assert.deepEqual(totals[5], { date: '2026-11-14', logged: 30, planned: 0, total: 30, norm: 0, over: 30, under: 0, unkeyed: 0 });
  });
});

describe('rowTotals', () => {
  it('sums logged and planned across the week', () => {
    const totals = rowTotals(
      row('A-1', { '2026-11-09': cell({ suggested: 60, logged: 30 }), '2026-11-10': cell({ suggested: 60, edited: 90 }) }),
    );
    assert.deepEqual(totals, { logged: 30, planned: 150, total: 180 });
  });
});

describe('visibleDays', () => {
  it('hides empty weekend days', () => {
    assert.deepEqual(
      visibleDays(data()).map((day) => day.date),
      DATES.slice(0, 5),
    );
    assert.deepEqual(
      hiddenDays(data()).map((day) => day.date),
      DATES.slice(5),
    );
  });

  it('shows a weekend day with logged, suggested, edited or unkeyed time', () => {
    const withLogged = data({ days: days({ '2026-11-14': { logged: 30 } }) });
    assert.ok(visibleDays(withLogged).some((day) => day.date === '2026-11-14'));
    const withSuggested = data({ rows: [row('A-1', { '2026-11-15': cell({ suggested: 30 }) })] });
    assert.ok(visibleDays(withSuggested).some((day) => day.date === '2026-11-15'));
    const withEdited = data({ rows: [row('A-1', { '2026-11-15': cell({ edited: 0 }) })] });
    assert.ok(visibleDays(withEdited).some((day) => day.date === '2026-11-15'));
    const withUnkeyed = data({ unkeyed: { items: [], cells: { '2026-11-14': { suggested: 60 } } } });
    assert.ok(visibleDays(withUnkeyed).some((day) => day.date === '2026-11-14'));
  });

  it('always shows a working weekend day and honours showWeekend', () => {
    const working = data({ days: days({ '2026-11-14': { kind: 'work', norm: 480 } }) });
    assert.equal(visibleDays(working).length, 6);
    assert.equal(visibleDays(data(), { showWeekend: true }).length, 7);
  });

  it('never hides weekdays even when empty or holidays', () => {
    const holiday = data({ days: days({ '2026-11-10': { kind: 'holiday', norm: 0 } }) });
    assert.ok(visibleDays(holiday).some((day) => day.date === '2026-11-10'));
  });
});

describe('cellState', () => {
  const workday = { date: '2026-11-10', submittable: true };
  const today = '2026-11-13';

  it('reports submission problems first', () => {
    assert.equal(cellState(cell({ submission: 'failed' }), workday, today), 'error');
    assert.equal(cellState(cell({ submission: 'unknown' }), { ...workday, submittable: false }, today), 'error');
    assert.equal(cellState(cell({ submission: 'pending', edited: 60 }), workday, today), 'pending');
  });

  it('disables days that are not submittable or are in the future', () => {
    assert.equal(cellState(cell({ suggested: 60 }), { ...workday, submittable: false }, today), 'disabled');
    assert.equal(cellState(cell({ suggested: 60 }), { date: '2026-11-14', submittable: true }, today), 'disabled');
  });

  it('distinguishes edited, logged and suggested', () => {
    assert.equal(cellState(cell({ suggested: 60, edited: 0 }), workday, today), 'edited');
    assert.equal(cellState(cell({ logged: 60 }), workday, today), 'logged');
    assert.equal(cellState(cell({ logged: 60, suggested: 30 }), workday, today), 'suggested');
    assert.equal(cellState(cell({ suggested: 60 }), workday, today), 'suggested');
    assert.equal(cellState(undefined, workday, today), 'suggested');
  });
});

describe('isEditableDay', () => {
  it('requires submittable and not in the future', () => {
    assert.equal(isEditableDay({ date: '2026-11-13', submittable: true }, '2026-11-13'), true);
    assert.equal(isEditableDay({ date: '2026-11-14', submittable: true }, '2026-11-13'), false);
    assert.equal(isEditableDay({ date: '2026-11-12', submittable: false }, '2026-11-13'), false);
    assert.equal(isEditableDay(undefined, '2026-11-13'), false);
  });
});

describe('buildSubmitEntries', () => {
  const sample = () =>
    data({
      days: days({ '2026-11-09': { submittable: false } }),
      rows: [
        row('A-1', {
          '2026-11-09': cell({ suggested: 60 }),
          '2026-11-10': cell({ suggested: 60, edited: 90 }),
          '2026-11-11': cell({ suggested: 120, edited: 0 }),
          '2026-11-12': cell({ suggested: 60, submission: 'pending' }),
          '2026-11-13': cell({ suggested: 30, submission: 'failed' }),
        }),
        row('B-2', {
          '2026-11-10': cell({ suggested: 30 }),
          '2026-11-12': cell({ suggested: 60, submission: 'unknown' }),
        }),
      ],
      unkeyed: { items: [], cells: { '2026-11-10': { suggested: 300 } } },
    });

  it('collects positive values of submittable days for the week', () => {
    assert.deepEqual(buildSubmitEntries(sample(), { type: 'week' }), [
      { key: 'A-1', date: '2026-11-10', minutes: 90 },
      { key: 'B-2', date: '2026-11-10', minutes: 30 },
      { key: 'A-1', date: '2026-11-13', minutes: 30 },
    ]);
  });

  it('limits to one day', () => {
    assert.deepEqual(buildSubmitEntries(sample(), { type: 'day', date: '2026-11-13' }), [
      { key: 'A-1', date: '2026-11-13', minutes: 30 },
    ]);
    assert.deepEqual(buildSubmitEntries(sample(), { type: 'day', date: '2026-11-09' }), []);
  });

  it('returns nothing for an unknown scope', () => {
    assert.deepEqual(buildSubmitEntries(sample(), null), []);
  });
});

describe('groupEntriesByDay', () => {
  it('groups entries with titles and day totals', () => {
    const sample = data({
      days: days({ '2026-11-10': { logged: 60 } }),
      rows: [row('A-1', { '2026-11-10': cell({ suggested: 480 }) }), row('B-2', { '2026-11-11': cell({ suggested: 60 }) })],
    });
    const groups = groupEntriesByDay(sample, buildSubmitEntries(sample, { type: 'week' }));
    assert.equal(groups.length, 2);
    assert.deepEqual(groups[0], {
      date: '2026-11-10',
      kind: 'work',
      norm: 480,
      logged: 60,
      total: 540,
      over: 60,
      under: 0,
      unkeyed: 0,
      minutes: 480,
      entries: [{ key: 'A-1', date: '2026-11-10', minutes: 480, title: 'Title A-1' }],
    });
    assert.equal(groups[1].under, 420);
  });
});

describe('summarizeResults and retryEntries', () => {
  const results = [
    { key: 'A-1', date: '2026-11-10', minutes: 60, status: 'done', error: null },
    { key: 'A-1', date: '2026-11-11', minutes: 30, status: 'failed', error: 'boom' },
    { key: 'B-2', date: '2026-11-11', minutes: 90, status: 'unknown', error: 'timeout' },
    { key: 'B-2', date: '2026-11-12', minutes: 30, status: 'skipped', error: null },
  ];

  it('counts statuses and lists errors', () => {
    const summary = summarizeResults(results);
    assert.equal(summary.done, 1);
    assert.equal(summary.skipped, 1);
    assert.equal(summary.failed, 1);
    assert.equal(summary.unknown, 1);
    assert.deepEqual(
      summary.errors.map((result) => result.status),
      ['failed', 'unknown'],
    );
    assert.deepEqual(summarizeResults(undefined), { done: 0, skipped: 0, failed: 0, unknown: 0, errors: [] });
  });

  it('retries only definitely failed entries', () => {
    assert.deepEqual(retryEntries(results), [{ key: 'A-1', date: '2026-11-11', minutes: 30 }]);
  });
});

describe('isSafeUrl', () => {
  it('allows only http links', () => {
    assert.equal(isSafeUrl('https://tracker.example/A-1'), true);
    assert.equal(isSafeUrl('http://x'), true);
    assert.equal(isSafeUrl('javascript:alert(1)'), false);
    assert.equal(isSafeUrl(null), false);
  });
});

describe('custom step', () => {
  it('takes the step from data and falls back to 30', () => {
    assert.equal(stepOf({ step: 60 }), 60);
    assert.equal(stepOf({ step: 15 }), 15);
    assert.equal(stepOf({}), 30);
    assert.equal(stepOf({ step: 0 }), 30);
    assert.equal(stepOf({ step: 'x' }), 30);
    assert.equal(stepOf(null), 30);
  });

  it('parses and rounds with a 60-minute step', () => {
    assert.equal(parseHours('1.4', 60), 60);
    assert.equal(parseHours('1.5', 60), 120);
    assert.equal(parseHours('0.4', 60), 0);
    assert.equal(roundToStep(89, 60), 60);
    assert.equal(stepMinutes(60, 1, 60), 120);
    assert.equal(stepMinutes(90, -1, 60), 60);
    assert.equal(stepMinutes(90, 1, 60), 120);
  });

  it('parses and rounds with a 15-minute step', () => {
    assert.equal(parseHours('0.25', 15), 15);
    assert.equal(parseHours('1,1', 15), 60);
    assert.equal(parseHours('1.2', 15), 75);
    assert.equal(stepMinutes(60, 1, 15), 75);
    assert.equal(stepMinutes(60, -1, 15), 45);
    assert.equal(stepMinutes(50, -1, 15), 45);
    assert.equal(stepMinutes(0, -1, 15), 0);
  });
});

describe('splitRows', () => {
  it('moves rows without any hours to the quiet group, keeping manual ones active', () => {
    const sample = data({
      rows: [
        row('A-1', { '2026-11-09': cell({ suggested: 30 }) }),
        row('B-2', { '2026-11-09': cell() }, { sources: ['review'] }),
        row('C-3', {}, { sources: ['comment', 'review'] }),
        row('D-4', {}, { sources: ['manual'] }),
        row('E-5', { '2026-11-10': cell({ edited: 0 }) }, { sources: ['review'] }),
        row('F-6', { '2026-11-10': cell({ logged: 60 }) }, { sources: ['comment'] }),
      ],
    });
    const { active, quiet } = splitRows(sample);
    assert.deepEqual(
      active.map((item) => item.key),
      ['A-1', 'D-4', 'E-5', 'F-6'],
    );
    assert.deepEqual(
      quiet.map((item) => item.key),
      ['B-2', 'C-3'],
    );
    assert.equal(rowHasHours(row('X-1', {})), false);
    assert.deepEqual(splitRows({}), { active: [], quiet: [] });
  });
});

describe('isUncertain', () => {
  it('flags pending and unknown submissions', () => {
    assert.equal(isUncertain(cell({ submission: 'pending' })), true);
    assert.equal(isUncertain(cell({ submission: 'unknown' })), true);
    assert.equal(isUncertain(cell({ submission: 'failed' })), false);
    assert.equal(isUncertain(undefined), false);
  });
});

describe('formatActivity', () => {
  it('formats single days and ranges with the status letter', () => {
    assert.deepEqual(
      formatActivity([
        { status: 'development', letter: 'd', from: '2026-11-19', to: '2026-11-19' },
        { status: 'development', letter: 'd', from: '2026-11-21', to: '2026-11-22' },
        { status: 'review', letter: 'r', from: '2026-11-22', to: '2026-11-22' },
      ]),
      [
        { text: '19.11d', title: 'development: 19.11' },
        { text: '21.11–22.11d', title: 'development: 21.11–22.11' },
        { text: '22.11r', title: 'review: 22.11' },
      ],
    );
  });

  it('tolerates missing or broken data', () => {
    assert.deepEqual(formatActivity(undefined), []);
    assert.deepEqual(formatActivity(null), []);
    assert.deepEqual(formatActivity([]), []);
    assert.deepEqual(formatActivity([{ status: 'x', letter: 'x', from: 'bad', to: '2026-11-19' }, null]), []);
    assert.deepEqual(formatActivity([{ from: '2026-12-31' }]), [{ text: '31.12', title: '31.12' }]);
  });

  it('leaves the end open while the issue is still in the status', () => {
    assert.deepEqual(
      formatActivity([
        { status: 'development', letter: 'd', from: '2026-11-09', to: '2026-11-09', open: false },
        { status: 'codereview', letter: 'r', from: '2026-11-09', to: '2026-11-10', open: true },
      ]),
      [
        { text: '09.11d', title: 'development: 09.11' },
        { text: '09.11–r', title: 'codereview: с 09.11 по сейчас' },
      ],
    );
  });
});

describe('workedOn', () => {
  const activity = [
    { status: 'development', from: '2026-11-03', to: '2026-11-04' },
    { status: 'codereview', from: '2026-11-06', to: '2026-11-10', open: true },
  ];

  it('tells whether the issue was in a working status that day', () => {
    assert.equal(workedOn(activity, '2026-11-03'), true);
    assert.equal(workedOn(activity, '2026-11-04'), true);
    assert.equal(workedOn(activity, '2026-11-05'), false);
    assert.equal(workedOn(activity, '2026-11-10'), true);
    assert.equal(workedOn([], '2026-11-05'), false);
  });

  it('is unknown when the history was not loaded', () => {
    assert.equal(workedOn(null, '2026-11-05'), null);
    assert.equal(workedOn(undefined, '2026-11-05'), null);
  });
});
