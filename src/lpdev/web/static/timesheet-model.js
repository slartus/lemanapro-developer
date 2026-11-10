export const STEP_MINUTES = 30;
export const MAX_CELL_MINUTES = 24 * 60;
export const ISSUE_KEY_RE = /^[A-Z][A-Z0-9_]+-\d+$/;

const HOURS_RE = /^(?:\d+(?:\.\d*)?|\.\d+)$/;
const WORKING_KINDS = new Set(['work', 'short']);
const BLOCKING_SUBMISSIONS = new Set(['pending', 'unknown']);

export function roundToStep(minutes, step = STEP_MINUTES) {
  if (!Number.isFinite(minutes) || minutes <= 0) return 0;
  return Math.round(minutes / step) * step;
}

export function parseHours(input) {
  const text = String(input ?? '')
    .trim()
    .replace(/\s+/g, '')
    .replace(/[чh]$/i, '')
    .replace(',', '.');
  if (text === '') return 0;
  if (!HOURS_RE.test(text)) return null;
  const minutes = roundToStep(Number(text) * 60);
  return minutes > MAX_CELL_MINUTES ? null : minutes;
}

export function formatHours(minutes) {
  const value = Math.round(((minutes ?? 0) / 60) * 100) / 100;
  return String(Object.is(value, -0) ? 0 : value);
}

export function isValidIssueKey(key) {
  return typeof key === 'string' && ISSUE_KEY_RE.test(key);
}

export function cellValue(cell) {
  if (!cell) return 0;
  return cell.edited ?? cell.suggested ?? 0;
}

export function dayTotals(data) {
  return (data.days ?? []).map((day) => {
    const planned = (data.rows ?? []).reduce((sum, row) => sum + cellValue(row.cells?.[day.date]), 0);
    const logged = day.logged ?? 0;
    const total = logged + planned;
    const norm = day.norm ?? 0;
    return {
      date: day.date,
      logged,
      planned,
      total,
      norm,
      over: Math.max(0, total - norm),
      under: Math.max(0, norm - total),
    };
  });
}

export function rowTotals(row) {
  return Object.values(row.cells ?? {}).reduce(
    (acc, cell) => {
      const logged = cell.logged ?? 0;
      const planned = cellValue(cell);
      return { logged: acc.logged + logged, planned: acc.planned + planned, total: acc.total + logged + planned };
    },
    { logged: 0, planned: 0, total: 0 },
  );
}

function dayHasData(data, date) {
  const day = (data.days ?? []).find((item) => item.date === date);
  if (day && (day.logged ?? 0) > 0) return true;
  const rowHit = (data.rows ?? []).some((row) => {
    const cell = row.cells?.[date];
    return Boolean(cell) && ((cell.logged ?? 0) > 0 || (cell.suggested ?? 0) > 0 || cell.edited != null);
  });
  return rowHit || (data.unkeyed?.cells?.[date]?.suggested ?? 0) > 0;
}

export function isCollapsibleDay(day, index) {
  return index >= 5 && !WORKING_KINDS.has(day.kind);
}

export function visibleDays(data, { showWeekend = false } = {}) {
  return (data.days ?? []).filter(
    (day, index) => showWeekend || !isCollapsibleDay(day, index) || dayHasData(data, day.date),
  );
}

export function hiddenDays(data, options) {
  const shown = new Set(visibleDays(data, options).map((day) => day.date));
  return (data.days ?? []).filter((day) => !shown.has(day.date));
}

export function isEditableDay(day, today) {
  return Boolean(day?.submittable) && (!today || day.date <= today);
}

export function cellState(cell, day, today) {
  const submission = cell?.submission ?? null;
  if (submission === 'failed' || submission === 'unknown') return 'error';
  if (submission === 'pending') return 'pending';
  if (!isEditableDay(day, today)) return 'disabled';
  if (cell?.edited != null) return 'edited';
  if ((cell?.logged ?? 0) > 0 && cellValue(cell) === 0) return 'logged';
  return 'suggested';
}

export function buildSubmitEntries(data, scope) {
  const days = (data.days ?? []).filter(
    (day) => isEditableDay(day, data.today) && (scope?.type === 'week' || (scope?.type === 'day' && day.date === scope.date)),
  );
  return days.flatMap((day) =>
    (data.rows ?? [])
      .filter((row) => {
        const cell = row.cells?.[day.date];
        return cellValue(cell) > 0 && !BLOCKING_SUBMISSIONS.has(cell?.submission);
      })
      .map((row) => ({ key: row.key, date: day.date, minutes: cellValue(row.cells[day.date]) })),
  );
}

export function groupEntriesByDay(data, entries) {
  const totals = new Map(dayTotals(data).map((total) => [total.date, total]));
  const titles = new Map((data.rows ?? []).map((row) => [row.key, row.title]));
  return (data.days ?? [])
    .map((day) => {
      const dayEntries = entries
        .filter((entry) => entry.date === day.date)
        .map((entry) => ({ ...entry, title: titles.get(entry.key) ?? '' }));
      const total = totals.get(day.date);
      return {
        date: day.date,
        kind: day.kind,
        norm: total.norm,
        logged: total.logged,
        total: total.total,
        over: total.over,
        under: total.under,
        minutes: dayEntries.reduce((sum, entry) => sum + entry.minutes, 0),
        entries: dayEntries,
      };
    })
    .filter((group) => group.entries.length > 0);
}

export function summarizeResults(results) {
  const list = results ?? [];
  const count = (status) => list.filter((result) => result.status === status).length;
  return {
    done: count('done'),
    skipped: count('skipped'),
    failed: count('failed'),
    unknown: count('unknown'),
    errors: list.filter((result) => result.status === 'failed' || result.status === 'unknown'),
  };
}

export function retryEntries(results) {
  return (results ?? [])
    .filter((result) => result.status === 'failed')
    .map(({ key, date, minutes }) => ({ key, date, minutes }));
}

export function stepMinutes(minutes, direction) {
  const base = Math.max(0, minutes ?? 0);
  const next =
    direction > 0
      ? Math.floor(base / STEP_MINUTES) * STEP_MINUTES + STEP_MINUTES
      : Math.ceil(base / STEP_MINUTES) * STEP_MINUTES - STEP_MINUTES;
  return Math.min(MAX_CELL_MINUTES, Math.max(0, next));
}

export function isSafeUrl(url) {
  return typeof url === 'string' && /^https?:\/\//i.test(url);
}
