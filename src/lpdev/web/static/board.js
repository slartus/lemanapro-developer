import { SECTION_META, actionableKeys, awaitsAuthor, deriveView, itemsByKey, tokenProblems } from '/static/radar/view.js';
import { createSnooze, nextSnoozeExpiry, pruneSnoozed, snoozeOptions } from '/static/radar/snooze.js';
import { failureUrl, isHttpsUrl, isUnderBaseUrl } from '/static/radar/links.js';
import { ACTIVE_REVIEW_STATUSES, MR_SECTIONS, buildRows, filterCounts, matchesFilter, matchesSearch, rowSections } from '/static/board-model.js';

const SECTION_TITLES = Object.fromEntries(SECTION_META.map(({ id, title }) => [id, title]));
const CHIP_LABELS = {
  reviewing: 'На ревью',
  assigned: 'Назначены',
  authored: 'Мои',
  awaitingApproval: 'Ждут апрува',
  mentioned: 'Упомянули',
  threads: 'Треды',
};
const ROLE_LABELS = {
  reviewing: 'ревьюер',
  assigned: 'назначен',
  authored: 'мой',
  awaitingApproval: 'ждёт апрува',
  mentioned: 'упомянули',
  threads: 'тред',
};
const ALLOWED_TOKEN_SCOPES = ['read_api', 'read_repository'];
const VERDICT_LABELS = { APPROVE: 'approve', APPROVE_WITH_NITS: 'approve · nits', REQUEST_CHANGES: 'changes' };
const SEVERITIES = [['crit', 'crit_count'], ['major', 'major_count'], ['minor', 'minor_count'], ['nit', 'nit_count']];
const FILTER_STORAGE_KEY = 'lpdev.board.filter';
const REFRESH_ACTIVE_MS = 5000;
const REFRESH_IDLE_MS = 30000;
const REFRESH_HIDDEN_MS = 60000;
const DAY_MS = 24 * 60 * 60 * 1000;

const STATUS_TAGS = {
  mergeable: ['можно мёржить', 'ok'],
  can_be_merged: ['можно мёржить', 'ok'],
  not_approved: ['нет апрувов', 'warn'],
  discussions_not_resolved: ['треды', 'warn'],
  ci_must_pass: ['CI не прошёл', 'bad'],
  ci_still_running: ['CI идёт', 'warn'],
  need_rebase: ['нужен rebase', 'bad'],
  conflict: ['конфликт', 'bad'],
  broken_status: ['сломан', 'bad'],
  cannot_be_merged: ['конфликт', 'bad'],
  requested_changes: ['просят правки', 'bad'],
  blocked_status: ['заблокирован', 'bad'],
  checking: ['проверяется', ''],
  unchecked: ['не проверен', ''],
  preparing: ['готовится', ''],
  approvals_syncing: ['апрувы синхронизируются', ''],
  not_open: ['закрыт', ''],
  commits_status: ['проблема с коммитами', 'bad'],
  merge_request_blocked: ['заблокирован другим MR', 'bad'],
  status_checks_must_pass: ['внешние проверки', 'bad'],
  jira_association_missing: ['нет задачи Jira', 'bad'],
  title_regex: ['название не по шаблону', 'bad'],
  locked_paths: ['заблокированы пути', 'bad'],
  locked_lfs_files: ['заблокированы LFS-файлы', 'bad'],
  security_policy_violations: ['нарушены политики', 'bad'],
  merge_time: ['ждёт времени мёржа', 'warn'],
};
const CI_MERGE_STATUSES = new Set(['ci_must_pass', 'ci_still_running']);
const PIPELINE_TAGS = {
  success: ['CI ок', 'ok'],
  failed: ['CI упал', 'bad'],
  running: ['CI идёт', 'warn'],
  pending: ['CI идёт', 'warn'],
  created: ['CI идёт', 'warn'],
  preparing: ['CI идёт', 'warn'],
  waiting_for_resource: ['CI идёт', 'warn'],
  waiting_for_callback: ['CI идёт', 'warn'],
  scheduled: ['CI запланирован', ''],
  manual: ['CI ждёт запуска', 'warn'],
  canceling: ['CI отменяется', ''],
  canceled: ['CI отменён', ''],
  skipped: ['CI пропущен', ''],
};
const FAILURE_REASONS = {
  script_failure: 'ошибка в скрипте',
  runner_system_failure: 'сбой раннера',
  stuck_or_timeout_failure: 'таймаут',
  job_execution_timeout: 'таймаут',
  missing_dependency_failure: 'нет артефактов зависимости',
  api_failure: 'ошибка API',
  scheduler_failure: 'ошибка планировщика',
  runner_unsupported: 'раннер не поддерживает джобу',
  unmet_prerequisites: 'не выполнены предусловия',
  archived_failure: 'джоба в архиве',
  downstream_pipeline_creation_failed: 'не создан дочерний пайплайн',
};

const state = {
  gitlabUrl: '',
  radar: null,
  mrs: [],
  userState: { snoozed: {}, dismissedTokenProblems: {} },
  filter: loadFilter(),
  search: '',
  expandedPipelines: new Set(),
  openSnoozeKey: null,
};

const $ = (id) => document.getElementById(id);

function el(tag, { className, text, attrs } = {}, children = []) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  Object.entries(attrs ?? {}).forEach(([key, value]) => node.setAttribute(key, value));
  node.append(...children.filter(Boolean));
  return node;
}

const tag = (label, tone = '', attrs = {}) => el('span', { className: `tag ${tone}`.trim(), text: label, attrs });

function loadFilter() {
  try {
    return localStorage.getItem(FILTER_STORAGE_KEY) || 'action';
  } catch {
    return 'action';
  }
}

function saveFilter(value) {
  try {
    localStorage.setItem(FILTER_STORAGE_KEY, value);
  } catch {}
}

function safeGitlabUrl(url) {
  return isUnderBaseUrl(url, state.gitlabUrl) ? url : null;
}

function link(url, children, attrs = {}) {
  const safe = safeGitlabUrl(url);
  return safe ? el('a', { attrs: { href: safe, rel: 'noopener noreferrer', ...attrs } }, children) : el('span', { attrs }, children);
}

function relTime(iso) {
  const time = Date.parse(iso);
  if (Number.isNaN(time)) return '';
  const minutes = Math.round((Date.now() - time) / 60000);
  if (minutes < 1) return 'только что';
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} ч`;
  return `${Math.round(hours / 24)} дн`;
}

function ageNode(iso) {
  const age = Date.now() - Date.parse(iso);
  const tone = age > 7 * DAY_MS ? 'stale-bad' : age > 3 * DAY_MS ? 'stale-warn' : '';
  return el('time', { className: tone, text: relTime(iso), attrs: { datetime: iso, title: new Date(iso).toLocaleString('ru-RU') } });
}


function currentView() {
  const dashboard = state.radar?.dashboard;
  if (!dashboard?.sections) return null;
  return deriveView(dashboard, state.userState, Date.now(), state.radar.hiddenSections ?? []);
}

function renderMrCell(row) {
  const radar = row.radar ?? {};
  const watch = row.watch ?? {};
  const title = radar.title ?? watch.title ?? `!${watch.iid}`;
  const reference = radar.reference ?? (watch.path_with_namespace ? `${watch.path_with_namespace}!${watch.iid}` : row.key);
  const author = radar.author ?? watch.author_username;
  const sub = el('div', { className: 'sub' }, [
    el('span', { text: reference }),
    author ? el('span', { text: `@${author}` }) : null,
    watch.source_branch ? el('span', { className: 'branch', text: watch.source_branch, attrs: { title: watch.source_branch } }) : null,
    radar.draft || watch.is_draft ? tag('draft') : null,
    radar.hasConflicts ? tag('конфликт', 'bad') : null,
  ]);
  return el('td', { className: 'mr-cell' }, [
    link(radar.webUrl ?? watch.web_url, [el('span', { className: 'mr-title', text: title })], { title }),
    sub,
  ]);
}

function renderRoleCell(row) {
  const radar = row.radar ?? {};
  const sections = rowSections(row);
  const primary = sections[0];
  const badges = [];
  if (primary) badges.push(tag(ROLE_LABELS[primary], `role ${primary}`, { title: SECTION_TITLES[primary] }));
  if (primary !== 'mentioned' && sections.includes('mentioned')) {
    const mentionUrl = radar.mentionUrl ?? row.watch?.mention_url;
    badges.push(link(mentionUrl, [tag('@', 'warn', { title: 'Меня упомянули — открыть комментарий' })]));
  }
  if (radar.threadsAwaitingReply > 0) badges.push(tag(`ответить: ${radar.threadsAwaitingReply}`, 'warn'));
  if (radar.approvedByMe) badges.push(tag('апрувнул', 'ok'));
  else if (awaitsAuthor(radar)) badges.push(tag(radar.myReviewState === 'requested_changes' ? 'попросил правки' : 'отревьюил'));
  return el('td', { className: 'tags' }, badges);
}

function describeFailure(failure) {
  if (failure.kind === 'downstream') return 'дочерний пайплайн';
  if (failure.kind === 'external') {
    const host = isHttpsUrl(failure.webUrl) ? new URL(failure.webUrl).hostname : null;
    return [failure.reason, host].filter(Boolean).join(' · ') || 'внешняя проверка';
  }
  return FAILURE_REASONS[failure.reason] ?? failure.reason ?? 'причина неизвестна';
}

function renderFailures(pipeline) {
  const failures = pipeline.failures ?? [];
  if (!failures.length) {
    return [el('div', { className: 'ci-job' }, [link(pipeline.webUrl, [el('span', { text: 'Упавших джоб не найдено — открыть пайплайн' })])])];
  }
  return failures.map((failure) => {
    const url = failureUrl(failure, state.gitlabUrl);
    const name = failure.stage && failure.stage !== 'external' ? `${failure.stage} / ${failure.name}` : failure.name;
    const body = [el('span', { className: 'ci-job-name', text: `✖ ${name}` }), el('span', { className: 'muted', text: describeFailure(failure) })];
    const target = url && (failure.kind === 'external' ? url : safeGitlabUrl(url));
    return target
      ? el('a', { className: 'ci-job', attrs: { href: target, rel: 'noopener noreferrer' } }, body)
      : el('div', { className: 'ci-job' }, body);
  });
}

function renderGitlabCell(row) {
  const radar = row.radar ?? {};
  const cell = el('td', { className: 'tags' });
  const pipeline = radar.pipeline;
  if (pipeline?.status === 'failed') {
    const expanded = state.expandedPipelines.has(row.key);
    const label = pipeline.onlyIgnoredFailures
      ? `⏸ CI ждёт: ${[...new Set((pipeline.failures ?? []).map((failure) => failure.name))].join(', ')}`
      : '✖ CI упал';
    const toggle = el('button', {
      className: `tag ci-toggle ${pipeline.onlyIgnoredFailures ? '' : 'bad'}`.trim(),
      text: `${label} ${expanded ? '▾' : '▸'}`,
      attrs: { type: 'button', 'aria-expanded': String(expanded) },
    });
    toggle.addEventListener('click', () => {
      if (expanded) state.expandedPipelines.delete(row.key);
      else state.expandedPipelines.add(row.key);
      render();
    });
    cell.append(toggle);
    if (expanded) cell.append(el('div', { className: 'ci-panel' }, renderFailures(pipeline)));
  } else if (pipeline && PIPELINE_TAGS[pipeline.status]) {
    const [label, tone] = PIPELINE_TAGS[pipeline.status];
    cell.append(link(pipeline.webUrl, [tag(label, tone)]));
  }
  const hasPipelineTag = Boolean(pipeline && (PIPELINE_TAGS[pipeline.status] || pipeline.status === 'failed'));
  const status = hasPipelineTag && CI_MERGE_STATUSES.has(radar.status) ? null : STATUS_TAGS[radar.status];
  if (status && !(radar.draft && radar.status === 'draft_status')) cell.append(tag(status[0], status[1]));
  if (typeof radar.approvalsCount === 'number' && row.sections.has('authored')) {
    cell.append(tag(`апрувов: ${radar.approvalsCount}`, radar.approvalsCount > 0 ? 'ok' : '', radar.approverNames?.length ? { title: radar.approverNames.join(', ') } : {}));
  }
  if (!cell.childNodes.length) cell.append(el('span', { className: 'muted', text: row.radar ? '—' : '' }));
  return cell;
}

function renderReviewCell(row) {
  const watch = row.watch;
  const cell = el('td', { className: 'tags review-cell' });
  if (!watch?.latest_review_id) {
    cell.append(el('span', { className: 'muted', text: '—' }));
    return cell;
  }
  const reviewLink = el('a', { attrs: { href: `/reviews/${watch.latest_review_id}` } }, [
    el('span', { className: `badge status ${watch.latest_review_status}`, text: watch.latest_review_status }),
  ]);
  cell.append(reviewLink);
  if (watch.latest_done_review_id) {
    const findings = SEVERITIES.filter(([, field]) => watch[field]);
    const done = el('a', { className: 'findings', attrs: { href: `/reviews/${watch.latest_done_review_id}` } });
    if (findings.length) findings.forEach(([name, field]) => done.append(el('span', { className: `sev ${name}`, text: String(watch[field]) })));
    else done.append(el('span', { className: 'sev clean', text: '✓' }));
    if (VERDICT_LABELS[watch.verdict]) done.append(el('span', { className: `verdict ${watch.verdict}`, text: VERDICT_LABELS[watch.verdict] }));
    cell.append(done);
  }
  return cell;
}

function renderSnoozeControl(row) {
  const section = rowSections(row)[0];
  if (!row.radar || !section) return el('td', { className: 'actions' });
  const isOpen = state.openSnoozeKey === row.key;
  const button = el('button', {
    className: 'icon-btn',
    text: '⏰',
    attrs: { type: 'button', title: 'Отложить', 'aria-expanded': String(isOpen), 'aria-label': 'Отложить MR' },
  });
  button.addEventListener('click', () => {
    state.openSnoozeKey = isOpen ? null : row.key;
    render();
  });
  const children = [button];
  if (isOpen) {
    const menu = el('div', { className: 'snooze-menu', attrs: { role: 'group', 'aria-label': 'Отложить MR' } });
    Object.entries(snoozeOptions(section)).forEach(([option, { label, title }]) => {
      const item = el('button', { text: label, attrs: { type: 'button', ...(title && { title }) } });
      item.addEventListener('click', () => {
        state.openSnoozeKey = null;
        updateSnoozed((snoozed) => ({ ...snoozed, [row.key]: createSnooze(option, row.radar) }));
      });
      menu.append(item);
    });
    children.push(menu);
  }
  return el('td', { className: 'actions' }, children);
}

function renderRow(row) {
  const tr = el('tr', { attrs: { id: `mr-${row.key.replace('!', '-')}`, 'data-key': row.key } }, [
    renderMrCell(row),
    renderRoleCell(row),
    renderGitlabCell(row),
    renderReviewCell(row),
    el('td', { className: 'nowrap' }, [row.updatedAt ? ageNode(row.updatedAt) : null]),
    renderSnoozeControl(row),
  ]);
  if (location.hash === `#mr=${row.key}`) tr.classList.add('highlight');
  return tr;
}

function renderChips(rows, actionable) {
  const counts = filterCounts(rows, actionable);
  const hidden = new Set(state.radar?.hiddenSections ?? []);
  const chips = [
    ['action', 'Требуют действия'],
    ['all', 'Все'],
    ...MR_SECTIONS.filter((id) => !hidden.has(id)).map((id) => [id, CHIP_LABELS[id]]),
    ['changes', 'Ревью: правки'],
    ['reviewRunning', 'Ревью идёт'],
  ];
  $('chips').replaceChildren(
    ...chips.map(([id, label]) => {
      const chip = el('button', {
        className: `chip ${state.filter === id ? 'active' : ''}`.trim(),
        attrs: { type: 'button', 'aria-pressed': String(state.filter === id), title: SECTION_TITLES[id] ?? label },
      }, [el('span', { text: label }), el('span', { className: 'chip-count', text: String(counts[id] ?? 0) })]);
      chip.addEventListener('click', () => {
        state.filter = id;
        saveFilter(id);
        render();
      });
      return chip;
    }),
  );
}

function renderExtraItem(item, sectionId, { unsnooze = false } = {}) {
  const target = (sectionId === 'possibleMrs' && item.mrUrl) || (sectionId === 'mentioned' && item.mentionUrl) || item.webUrl;
  const tags = [];
  if (item.kind === 'issue') tags.push(tag('задача'));
  if (sectionId === 'possibleMrs') {
    if (item.mrState === 'merged' || (item.merged && !item.mrState)) tags.push(tag('смержена', 'warn'));
    else if (item.mrState === 'closed') tags.push(tag('MR закрыт', 'warn'));
    else tags.push(tag('создать MR', 'ok'));
  }
  if (sectionId === 'brokenBranches') tags.push(tag('CI упал', 'bad'));
  if (item.snooze) tags.push(tag(formatSnooze(item.snooze)));
  const row = el('li', { className: 'extra-item' }, [
    link(target, [el('span', { className: 'mr-title', text: item.title })], { title: item.title }),
    el('div', { className: 'sub' }, [el('span', { text: item.reference ?? '' }), item.updatedAt ? ageNode(item.updatedAt) : null, ...tags]),
  ]);
  if (unsnooze) {
    const back = el('button', { className: 'icon-btn', text: '↩', attrs: { type: 'button', title: 'Вернуть' } });
    back.addEventListener('click', () => updateSnoozed(({ [item.key]: _removed, ...rest }) => rest));
    row.append(back);
  } else if (sectionId === 'possibleMrs') {
    const hide = el('button', { className: 'icon-btn', text: '⏰', attrs: { type: 'button', title: 'Скрыть навсегда' } });
    hide.addEventListener('click', () => updateSnoozed((snoozed) => ({ ...snoozed, [item.key]: createSnooze('forever', item) })));
    row.append(hide);
  }
  return row;
}

function formatSnooze(snooze) {
  if (snooze?.forever) return 'навсегда';
  if (!snooze?.until) return 'до изменений';
  const until = new Date(snooze.until);
  const time = until.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  return until.toDateString() === new Date().toDateString()
    ? `до ${time}`
    : `до ${until.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' })} ${time}`;
}

function renderExtras(view) {
  const hidden = new Set(state.radar?.hiddenSections ?? []);
  const blocks = [];
  const add = (id, title, items, options) => {
    if (!items.length) return;
    const details = el('details', { className: 'extra', attrs: { 'data-block': id } }, [
      el('summary', {}, [el('span', { text: title }), el('span', { className: 'chip-count', text: String(items.length) })]),
      el('ul', {}, items.map((item) => renderExtraItem(item, options?.section ?? id, options))),
    ]);
    if (openBlocks.has(id)) details.open = true;
    details.addEventListener('toggle', () => (details.open ? openBlocks.add(id) : openBlocks.delete(id)));
    blocks.push(details);
  };
  if (view) {
    if (!hidden.has('mentioned')) add('issues', 'Задачи с упоминанием', view.sections.mentioned.filter((item) => item.kind === 'issue'), { section: 'mentioned' });
    if (!hidden.has('possibleMrs')) add('possibleMrs', SECTION_TITLES.possibleMrs, view.sections.possibleMrs);
    if (!hidden.has('brokenBranches')) add('brokenBranches', SECTION_TITLES.brokenBranches, view.sections.brokenBranches);
    add('snoozed', 'Отложено', view.snoozed, { section: 'snoozed', unsnooze: true });
  }
  $('extras').replaceChildren(...blocks);
}

const openBlocks = new Set();

function renderStatus(view) {
  const radar = state.radar;
  const dashboard = radar?.dashboard;
  const notes = [];
  if (!radar?.enabled) notes.push(el('span', { text: 'Радар выключен в настройках' }));
  else if (!dashboard) notes.push(el('span', { text: radar?.error ? '' : 'Радар ещё не обновлялся…' }));
  else {
    const fetched = ageNode(dashboard.fetchedAt);
    fetched.textContent = `${fetched.textContent} назад`.replace('только что назад', 'только что');
    notes.push(el('span', { text: `@${dashboard.username} · радар обновлён` }), fetched);
    const partial = ['projectsUnavailable', 'pipelinesUnavailable', 'mentionsUnavailable', 'assignedUnavailable', 'defaultBranchesUnavailable', 'possibleMrsUnavailable']
      .some((flag) => dashboard[flag]) || dashboard.skippedMrs || dashboard.skippedProjects;
    if (partial) notes.push(tag('часть данных не загрузилась', 'warn'));
  }
  $('radar-meta').replaceChildren(...notes);
  const banners = [];
  if (radar?.error) banners.push(el('div', { className: 'flash err', text: `Радар: ${radar.error}` }));
  if (dashboard?.warning) banners.push(el('div', { className: 'flash warn', text: dashboard.warning }));
  tokenProblems(dashboard?.token, state.userState.dismissedTokenProblems ?? {}, Date.now(), ALLOWED_TOKEN_SCOPES).forEach((problem) => {
    const banner = el('div', { className: 'flash warn', text: `${problem.text} ` });
    if (problem.dismissKey) {
      const dismiss = el('button', { text: 'Скрыть', attrs: { type: 'button' } });
      dismiss.addEventListener('click', () =>
        updateUserState((current) => ({
          ...current,
          dismissedTokenProblems: { ...current.dismissedTokenProblems, [problem.kind]: problem.dismissKey },
        })),
      );
      banner.append(dismiss);
    }
    banners.push(banner);
  });
  $('banners').replaceChildren(...banners);
  return view;
}

function updateCounters(actionableCount) {
  const counter = $('nav-count-mr');
  if (counter) counter.textContent = actionableCount ? String(actionableCount) : '';
  document.title = `${actionableCount ? `(${actionableCount}) ` : ''}${BASE_TITLE}`;
}

const BASE_TITLE = document.title;

function render() {
  const view = currentView();
  const rows = buildRows(view, state.mrs, state.radar?.hiddenSections ?? []);
  const keys = view ? actionableKeys(view.sections) : { waiting: new Set(), failedCi: new Set() };
  const actionable = new Set([...keys.waiting, ...keys.failedCi]);
  renderStatus(view);
  renderChips(rows, actionable);
  const visible = rows.filter((row) => matchesFilter(row, state.filter, actionable) && matchesSearch(row, state.search));
  $('mr-body').replaceChildren(...visible.map(renderRow));
  const empty = $('no-match');
  empty.hidden = visible.length > 0;
  empty.textContent = rows.length ? (state.filter === 'action' ? 'Ничего не требует действия' : 'Ничего не найдено') : 'Открытых MR нет';
  renderExtras(view);
  updateCounters([...actionable].filter((key) => rows.some((row) => row.key === key)).length);
  scheduleSnoozeExpiry(view);
}

let snoozeTimer = null;

function scheduleSnoozeExpiry() {
  clearTimeout(snoozeTimer);
  const dashboard = state.radar?.dashboard;
  if (!dashboard?.sections) return;
  const next = nextSnoozeExpiry(state.userState.snoozed ?? {}, itemsByKey(dashboard), Date.now());
  if (next) snoozeTimer = setTimeout(render, Math.max(1000, next - Date.now() + 500));
}

const isCompleteDashboard = (dashboard) =>
  Boolean(dashboard?.sections) &&
  !dashboard.skippedMrs &&
  !dashboard.skippedProjects &&
  !dashboard.projectsUnavailable &&
  !dashboard.mentionsUnavailable &&
  !dashboard.assignedUnavailable &&
  !dashboard.defaultBranchesUnavailable &&
  !dashboard.possibleMrsUnavailable;

let userStateWrite = Promise.resolve();

function updateUserState(change) {
  state.userState = change(state.userState);
  render();
  const snapshot = state.userState;
  userStateWrite = userStateWrite
    .then(() =>
      fetch('/api/ui-state/radar', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'lpdev' },
        body: JSON.stringify(snapshot),
      }),
    )
    .catch(() => {});
  return userStateWrite;
}

function updateSnoozed(change) {
  return updateUserState((current) => {
    const dashboard = state.radar?.dashboard;
    const snoozed = isCompleteDashboard(dashboard) && !state.radar?.error
      ? pruneSnoozed(current.snoozed, itemsByKey(dashboard))
      : (current.snoozed ?? {});
    return { ...current, snoozed: change(snoozed) };
  });
}

const iconLink = document.querySelector('link[rel="icon"]');
const ORIGINAL_ICON = iconLink?.getAttribute('href') ?? null;
let finishedBaseline = null;
let unseenFinished = 0;

function trackFinishedReviews() {
  const done = new Set(state.mrs.map((mr) => mr.latest_done_review_id).filter(Boolean).map(String));
  if (!document.hidden) {
    finishedBaseline = done;
    return;
  }
  if (!finishedBaseline) {
    finishedBaseline = done;
    return;
  }
  const fresh = [...done].filter((id) => !finishedBaseline.has(id));
  if (!fresh.length) return;
  fresh.forEach((id) => finishedBaseline.add(id));
  unseenFinished += fresh.length;
  if (iconLink) iconLink.setAttribute('href', '/static/favicon-dot.svg');
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  unseenFinished = 0;
  finishedBaseline = null;
  if (iconLink && ORIGINAL_ICON !== null) iconLink.setAttribute('href', ORIGINAL_ICON);
  refresh();
});

let refreshTimer = null;
let inFlight = false;

async function refresh() {
  if (inFlight) return;
  inFlight = true;
  try {
    const [mrsResponse, radarResponse] = await Promise.all([
      fetch('/api/mrs', { cache: 'no-store' }),
      fetch('/api/radar', { cache: 'no-store' }),
    ]);
    if (mrsResponse.ok) state.mrs = await mrsResponse.json();
    if (radarResponse.ok) state.radar = await radarResponse.json();
    trackFinishedReviews();
    render();
  } catch {
  } finally {
    inFlight = false;
    clearTimeout(refreshTimer);
    const active = state.mrs.some((mr) => ACTIVE_REVIEW_STATUSES.has(mr.latest_review_status));
    refreshTimer = setTimeout(refresh, document.hidden ? REFRESH_HIDDEN_MS : active ? REFRESH_ACTIVE_MS : REFRESH_IDLE_MS);
  }
}

async function forceCheck(button) {
  button.disabled = true;
  try {
    const response = await fetch('/force-check', { method: 'POST', headers: { 'X-Requested-With': 'lpdev' } });
    if (!response.ok) $('banners').prepend(el('div', { className: 'flash err', text: await response.text() }));
    [2000, 5000, 10000].forEach((delay) => setTimeout(refresh, delay));
  } finally {
    setTimeout(() => (button.disabled = false), 2000);
  }
}

function init() {
  const initial = JSON.parse($('initial-data').textContent);
  state.gitlabUrl = initial.gitlabUrl;
  state.mrs = initial.mrs;
  state.radar = initial.radar;
  state.userState = { snoozed: {}, dismissedTokenProblems: {}, ...(initial.userState ?? {}) };
  $('search').addEventListener('input', (event) => {
    state.search = event.target.value;
    render();
  });
  $('force-check').addEventListener('click', (event) => forceCheck(event.currentTarget));
  document.addEventListener('click', (event) => {
    if (state.openSnoozeKey && !event.target.closest('.actions')) {
      state.openSnoozeKey = null;
      render();
    }
  });
  render();
  if (location.hash.startsWith('#mr=')) document.querySelector('tr.highlight')?.scrollIntoView({ block: 'center' });
  refreshTimer = setTimeout(refresh, REFRESH_IDLE_MS);
  setInterval(render, 60000);
}

init();
