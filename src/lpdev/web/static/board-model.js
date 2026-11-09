export const MR_SECTIONS = ['reviewing', 'assigned', 'authored', 'awaitingApproval', 'mentioned', 'threads'];
export const ACTIVE_REVIEW_STATUSES = new Set(['queued', 'cloning', 'reviewing']);

const WATCH_ROLE_SECTIONS = { reviewer: 'reviewing', assignee: 'assigned', author: 'authored', mentioned: 'mentioned' };

const keyOfWatch = (mr) => `${mr.gitlab_project_id}!${mr.iid}`;
const latestIso = (...values) => values.filter(Boolean).sort().at(-1) ?? null;

export function buildRows(view, mrs, hiddenSections = []) {
  const rows = new Map();
  const hidden = new Set(hiddenSections);
  const rowFor = (key) => {
    if (!rows.has(key)) rows.set(key, { key, radar: null, sections: new Set(), watch: null });
    return rows.get(key);
  };
  if (view) {
    MR_SECTIONS.filter((id) => !hidden.has(id)).forEach((id) =>
      (view.sections[id] ?? [])
        .filter((item) => item.kind === 'mr')
        .forEach((item) => {
          const row = rowFor(item.key);
          row.radar = { ...row.radar, ...item };
          row.sections.add(id);
        }),
    );
  }
  const snoozedKeys = new Set((view?.snoozed ?? []).map((item) => item.key));
  mrs.forEach((mr) => {
    const key = keyOfWatch(mr);
    if (!snoozedKeys.has(key)) rowFor(key).watch = mr;
  });
  return [...rows.values()]
    .map((row) => ({ ...row, updatedAt: latestIso(row.radar?.updatedAt, row.watch?.updated_at) }))
    .sort((a, b) => (b.updatedAt ?? '').localeCompare(a.updatedAt ?? ''));
}

export function rowSections(row) {
  if (row.sections.size) return [...row.sections];
  return (row.watch?.my_roles ?? []).map((role) => WATCH_ROLE_SECTIONS[role]).filter(Boolean);
}

export function matchesFilter(row, filter, actionable) {
  if (filter === 'all') return true;
  if (filter === 'action') return actionable.has(row.key);
  if (filter === 'changes') return row.watch?.verdict === 'REQUEST_CHANGES';
  if (filter === 'reviewRunning') return ACTIVE_REVIEW_STATUSES.has(row.watch?.latest_review_status);
  if (filter === 'reviewError') return row.watch?.latest_review_status === 'error';
  return rowSections(row).includes(filter);
}

export function visibleActionable(view, hiddenSections, actionableKeys) {
  if (!view) return new Set();
  const hidden = new Set(hiddenSections);
  const sections = Object.fromEntries(Object.entries(view.sections).map(([id, items]) => [id, hidden.has(id) ? [] : items]));
  const { waiting, failedCi } = actionableKeys(sections);
  return new Set([...waiting, ...failedCi]);
}

const FINISHED_REVIEW_STATUSES = new Set(['done', 'error']);

export function finishedReviewMarks(mrs) {
  return new Set(
    mrs
      .filter((mr) => mr.latest_review_id && FINISHED_REVIEW_STATUSES.has(mr.latest_review_status))
      .map((mr) => `${mr.latest_review_id}:${mr.latest_review_status}`),
  );
}

export function matchesSearch(row, search) {
  const needle = search.trim().toLowerCase();
  if (!needle) return true;
  return [
    row.radar?.title ?? row.watch?.title,
    row.radar?.reference,
    row.watch?.path_with_namespace,
    row.radar?.author ?? row.watch?.author_username,
    row.watch?.author_name,
    row.watch?.source_branch,
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
    .includes(needle);
}

export function filterCounts(rows, actionable) {
  const counts = Object.fromEntries(
    ['all', 'action', 'changes', 'reviewRunning', 'reviewError', ...MR_SECTIONS].map((filter) => [
      filter,
      rows.filter((row) => matchesFilter(row, filter, actionable)).length,
    ]),
  );
  return counts;
}
