import { SECTION_META, actionableKeys, awaitsAuthor, deriveView, itemsByKey, tokenProblems } from '/static/radar/view.js';
import { createSnooze, nextSnoozeExpiry, pruneSnoozed, snoozeOptions } from '/static/radar/snooze.js';
import { failureUrl, isHttpsUrl, isUnderBaseUrl } from '/static/radar/links.js';
import {
  ACTIVE_REVIEW_STATUSES,
  MR_SECTIONS,
  buildRows,
  filterCounts,
  finishedReviewMarks,
  matchesFilter,
  matchesSearch,
  rowSections,
  visibleActionable,
} from '/static/board-model.js';

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
const PARTIAL_FLAGS = {
  projectsUnavailable: 'проекты',
  pipelinesUnavailable: 'статусы CI',
  mentionsUnavailable: 'упоминания',
  assignedUnavailable: 'назначенные на меня',
  defaultBranchesUnavailable: 'статусы default-веток',
  possibleMrsUnavailable: 'возможные MR',
};
const ALLOWED_TOKEN_SCOPES = ['read_api', 'read_repository'];
const VERDICT_LABELS = { APPROVE: 'approve', APPROVE_WITH_NITS: 'approve · nits', REQUEST_CHANGES: 'changes' };
const SEVERITIES = [['crit', 'crit_count'], ['major', 'major_count'], ['minor', 'minor_count'], ['nit', 'nit_count']];
const FILTER_STORAGE_KEY = 'lpdev.board.filter';
const REFRESH_ACTIVE_MS = 5000;
const REFRESH_IDLE_MS = 30000;
const REFRESH_HIDDEN_MS = 60000;
const DAY_MS = 24 * 60 * 60 * 1000;
const BASE_TITLE = document.title;
const iconLink = document.querySelector('link[rel="icon"]');
const ORIGINAL_ICON = iconLink?.getAttribute('href') ?? null;

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
  openBlocks: new Set(),
  forceCheck: null,
  pendingRender: false,
  finishedBaseline: null,
  userStateVersion: 0,
  forceCheckTimer: null,
  reviewProjects: new Set(),
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

function relTime(iso, suffix = '') {
  const time = Date.parse(iso);
  if (Number.isNaN(time)) return '';
  const minutes = Math.round((Date.now() - time) / 60000);
  if (minutes < 1) return 'только что';
  if (minutes < 60) return `${minutes} мин${suffix}`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} ч${suffix}`;
  return `${Math.round(hours / 24)} дн${suffix}`;
}

function ageNode(iso, suffix = '') {
  const time = Date.parse(iso);
  if (Number.isNaN(time)) return null;
  const age = Date.now() - time;
  const tone = age > 7 * DAY_MS ? 'stale-bad' : age > 3 * DAY_MS ? 'stale-warn' : '';
  return el('time', { className: tone, text: relTime(iso, suffix), attrs: { datetime: iso, title: new Date(time).toLocaleString('ru-RU') } });
}

function currentView() {
  const dashboard = state.radar?.dashboard;
  if (!state.radar?.enabled || !dashboard?.sections) return null;
  return deriveView(dashboard, state.userState, Date.now(), state.radar.hiddenSections ?? []);
}

function renderMrCell(row) {
  const radar = row.radar ?? {};
  const watch = row.watch ?? {};
  const title = radar.title ?? watch.title ?? `!${watch.iid}`;
  const reference = radar.reference ?? (watch.path_with_namespace ? `${watch.path_with_namespace}!${watch.iid}` : row.key);
  const sub = el('div', { className: 'sub' }, [
    el('span', { text: reference }),
    watch.source_branch ? el('span', { className: 'branch', text: watch.source_branch, attrs: { title: watch.source_branch } }) : null,
    radar.draft || watch.is_draft ? tag('draft') : null,
    radar.hasConflicts ? tag('конфликт', 'bad') : null,
  ]);
  return el('td', { className: 'mr-cell' }, [
    link(radar.webUrl ?? watch.web_url, [el('span', { className: 'mr-title', text: title })], { title }),
    sub,
  ]);
}

function renderAuthorCell(row) {
  const username = row.radar?.author ?? row.watch?.author_username;
  const name = row.radar?.authorName ?? row.watch?.author_name;
  const label = name ?? (username ? `@${username}` : '—');
  return el('td', { className: 'author', text: label, attrs: username ? { title: `@${username}` } : {} });
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
  if (!Array.isArray(pipeline.failures)) {
    return [el('div', { className: 'ci-job' }, [link(pipeline.webUrl, [el('span', { text: 'Не удалось загрузить джобы — открыть пайплайн' })])])];
  }
  if (!pipeline.failures.length) {
    return [el('div', { className: 'ci-job' }, [link(pipeline.webUrl, [el('span', { text: 'Упавших джоб не найдено — открыть пайплайн' })])])];
  }
  return pipeline.failures.map((failure) => {
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
      attrs: { type: 'button', 'aria-expanded': String(expanded), 'data-focus-id': `ci:${row.key}` },
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
    const approvers = radar.approverNames?.length ? { title: radar.approverNames.join(', ') } : {};
    cell.append(tag(`апрувов: ${radar.approvalsCount}`, radar.approvalsCount > 0 ? 'ok' : '', approvers));
  }
  if (!cell.childNodes.length) cell.append(el('span', { className: 'muted', text: row.radar ? '—' : '' }));
  return cell;
}

async function startReview(row, button) {
  button.disabled = true;
  const reviewId = row.watch?.latest_review_id;
  const [projectId, iid] = row.key.split('!');
  const url = state.reviewProjects.has(Number(projectId))
    ? `/api/mr/${Number(projectId)}/${Number(iid)}/review`
    : `/reviews/${Number(reviewId)}/retry`;
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'lpdev' },
      body: '{}',
    });
    showStatus(response.ok ? { text: 'ревью поставлено в очередь' } : { text: (await response.text()) || `ошибка ${response.status}`, error: true });
    refresh();
  } catch {
    showStatus({ text: 'сервис не отвечает', error: true });
  } finally {
    button.disabled = false;
  }
}

async function cancelReview(row, button) {
  button.disabled = true;
  const reviewId = row.watch?.latest_review_id;
  try {
    const response = await fetch(`/reviews/${Number(reviewId)}/cancel`, {
      method: 'POST',
      headers: { 'X-Requested-With': 'lpdev' },
    });
    showStatus(response.ok ? { text: 'ревью остановлено' } : { text: (await response.text()) || `ошибка ${response.status}`, error: true });
    refresh();
  } catch {
    showStatus({ text: 'сервис не отвечает', error: true });
  } finally {
    button.disabled = false;
  }
}

function reviewButton(row) {
  const watch = row.watch;
  const projectId = Number(row.key.split('!')[0]);
  const title = row.radar?.title ?? watch?.title ?? row.key;
  if (ACTIVE_REVIEW_STATUSES.has(watch?.latest_review_status)) {
    if (!watch?.latest_review_id) return null;
    const stop = el('button', {
      className: 'icon-btn review-btn',
      text: '■ Стоп',
      attrs: {
        type: 'button',
        title: 'Остановить ревью',
        'aria-label': `Остановить ревью: ${title}`,
        'data-focus-id': `review:${row.key}`,
      },
    });
    stop.addEventListener('click', () => cancelReview(row, stop));
    return stop;
  }
  if (!watch?.latest_review_id && !state.reviewProjects.has(projectId)) return null;
  const rerun = Boolean(watch?.latest_review_id);
  const button = el('button', {
    className: 'icon-btn review-btn',
    text: rerun ? '↻' : '▶ Ревью',
    attrs: {
      type: 'button',
      title: rerun ? 'Запустить ревью заново' : 'Запустить авторевью',
      'aria-label': `${rerun ? 'Ревью заново' : 'Запустить ревью'}: ${title}`,
      'data-focus-id': `review:${row.key}`,
    },
  });
  button.addEventListener('click', () => startReview(row, button));
  return button;
}

function renderReviewCell(row) {
  const watch = row.watch;
  const cell = el('td', { className: 'tags review-cell' });
  if (!watch?.latest_review_id) {
    cell.append(el('span', { className: 'muted', text: '—' }), reviewButton(row) ?? '');
    return cell;
  }
  cell.append(
    el('a', { attrs: { href: `/reviews/${Number(watch.latest_review_id)}` } }, [
      el('span', { className: `badge status ${watch.latest_review_status}`, text: watch.latest_review_status }),
    ]),
  );
  if (watch.latest_done_review_id) {
    const findings = SEVERITIES.filter(([, field]) => watch[field]);
    const done = el('a', { className: 'findings', attrs: { href: `/reviews/${Number(watch.latest_done_review_id)}`, title: 'Находки последнего завершённого ревью' } });
    if (findings.length) findings.forEach(([name, field]) => done.append(el('span', { className: `sev ${name}`, text: String(watch[field]) })));
    else done.append(el('span', { className: 'sev clean', text: '✓' }));
    if (VERDICT_LABELS[watch.verdict]) done.append(el('span', { className: `verdict ${watch.verdict}`, text: VERDICT_LABELS[watch.verdict] }));
    cell.append(done);
  }
  const button = reviewButton(row);
  if (button) cell.append(button);
  return cell;
}

function focusById(id) {
  const target = id ? document.querySelector(`[data-focus-id="${CSS.escape(id)}"]`) : null;
  target?.focus({ preventScroll: true });
  return Boolean(target);
}

function focusTargetsAfterRemoval(node, prefix) {
  const item = node.closest('tr, li');
  const neighbour = item?.nextElementSibling ?? item?.previousElementSibling;
  const sameKind = neighbour?.querySelector(`[data-focus-id^="${prefix}"]`) ?? neighbour?.querySelector('[data-focus-id]');
  const block = node.closest('details');
  const nearbyBlock = block?.nextElementSibling ?? block?.previousElementSibling;
  return [
    sameKind?.getAttribute('data-focus-id'),
    nearbyBlock?.querySelector('summary')?.getAttribute('data-focus-id'),
  ].filter(Boolean);
}

function focusAfterRemoval(targets) {
  if (!targets.some((id) => focusById(id))) $('chips').querySelector('.chip.active')?.focus({ preventScroll: true });
}

function closeSnoozeMenu({ focusButton = false } = {}) {
  const key = state.openSnoozeKey;
  if (key === null) return;
  state.openSnoozeKey = null;
  render();
  if (focusButton) focusById(`snooze:${key}`);
}

function snoozeControl(key, item, sectionId, title) {
  const isOpen = state.openSnoozeKey === key;
  const button = el('button', {
    className: 'icon-btn',
    text: '⏰',
    attrs: {
      type: 'button',
      title: 'Отложить',
      'aria-haspopup': 'true',
      'aria-expanded': String(isOpen),
      'aria-label': `Отложить: ${title}`,
      'data-focus-id': `snooze:${key}`,
    },
  });
  button.addEventListener('click', () => {
    state.openSnoozeKey = isOpen ? null : key;
    render();
    if (!isOpen) {
      const first = document.querySelector('.snooze-menu button');
      first?.focus({ preventScroll: true });
      first?.closest('.snooze-menu')?.scrollIntoView({ block: 'nearest' });
    }
  });
  const wrap = el('span', { className: 'snooze-wrap' }, [button]);
  if (isOpen) {
    const menu = el('div', { className: 'snooze-menu', attrs: { role: 'group', 'aria-label': `Отложить: ${title}` } });
    Object.entries(snoozeOptions(sectionId)).forEach(([option, { label, title: hint }]) => {
      const choice = el('button', { text: label, attrs: { type: 'button', ...(hint && { title: hint }) } });
      choice.addEventListener('click', () => {
        const targets = focusTargetsAfterRemoval(choice, 'snooze:');
        state.openSnoozeKey = null;
        updateSnoozed((snoozed) => ({ ...snoozed, [key]: createSnooze(option, item) }));
        focusAfterRemoval(targets);
      });
      menu.append(choice);
    });
    wrap.append(menu);
  }
  return wrap;
}

function renderRow(row) {
  const section = rowSections(row)[0];
  const title = row.radar?.title ?? row.watch?.title ?? row.key;
  const actions = el('td', { className: 'actions' }, [row.radar && section ? snoozeControl(row.key, row.radar, section, title) : null]);
  const tr = el('tr', { attrs: { id: `mr-${row.key.replace('!', '-')}`, 'data-key': row.key } }, [
    renderMrCell(row),
    renderAuthorCell(row),
    renderRoleCell(row),
    renderGitlabCell(row),
    renderReviewCell(row),
    el('td', { className: 'nowrap' }, [row.updatedAt ? ageNode(row.updatedAt) : null]),
    actions,
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
    ['reviewError', 'Ревью: ошибка'],
  ];
  $('chips').replaceChildren(
    ...chips.map(([id, label]) => {
      const chip = el(
        'button',
        {
          className: `chip ${state.filter === id ? 'active' : ''}`.trim(),
          attrs: { type: 'button', 'aria-pressed': String(state.filter === id), title: SECTION_TITLES[id] ?? label, 'data-focus-id': `chip:${id}` },
        },
        [el('span', { text: label }), el('span', { className: 'chip-count', text: String(counts[id] ?? 0) })],
      );
      chip.addEventListener('click', () => {
        state.filter = id;
        saveFilter(id);
        render();
      });
      return chip;
    }),
  );
}

function branchesUrl(item) {
  const url = new URL(state.gitlabUrl);
  url.pathname = `${url.pathname.replace(/\/+$/, '')}/${item.projectPath}/-/branches`;
  url.searchParams.set('state', 'all');
  url.searchParams.set('sort', 'updated_desc');
  url.searchParams.set('search', item.title);
  return url.href;
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

function renderExtraItem(item, sectionId, { snoozedBlock = false } = {}) {
  const target = (sectionId === 'possibleMrs' && item.mrUrl) || (sectionId === 'mentioned' && item.mentionUrl) || item.webUrl;
  const tags = [];
  if (item.kind === 'issue') tags.push(tag('задача'));
  if (sectionId === 'possibleMrs') {
    if (item.mrState === 'merged' || (item.merged && !item.mrState)) tags.push(tag('смержена', 'warn'));
    else if (item.mrState === 'closed') tags.push(tag('MR закрыт', 'warn'));
    else tags.push(tag('создать MR', 'ok'));
  }
  if (sectionId === 'brokenBranches') tags.push(tag('CI упал', 'bad'));
  if (snoozedBlock) tags.push(tag(formatSnooze(item.snooze)));
  const actions = el('span', { className: 'extra-actions' });
  if (snoozedBlock) {
    const back = el('button', { className: 'icon-btn', text: '↩', attrs: { type: 'button', title: 'Вернуть', 'aria-label': `Вернуть: ${item.title}`, 'data-focus-id': `unsnooze:${item.key}` } });
    back.addEventListener('click', () => {
      const targets = focusTargetsAfterRemoval(back, 'unsnooze:');
      updateSnoozed(({ [item.key]: _removed, ...rest }) => rest);
      focusAfterRemoval(targets);
    });
    actions.append(back);
  } else {
    actions.append(snoozeControl(item.key, item, sectionId, item.title));
    if (sectionId === 'possibleMrs' && item.projectPath) {
      actions.append(
        link(branchesUrl(item), [el('span', { text: '🗑', attrs: { 'aria-hidden': 'true' } })], {
          class: 'icon-btn',
          title: 'Открыть ветки в GitLab, чтобы удалить ветку',
          'aria-label': `Открыть ветки: ${item.title}`,
        }),
      );
    }
  }
  return el('li', { className: 'extra-item' }, [
    el('div', {}, [
      link(target, [el('span', { className: 'mr-title', text: item.title })], { title: item.title }),
      el('div', { className: 'sub' }, [el('span', { text: item.reference ?? '' }), item.updatedAt ? ageNode(item.updatedAt) : null, ...tags]),
    ]),
    actions,
  ]);
}

function renderExtras(view) {
  const hidden = new Set(state.radar?.hiddenSections ?? []);
  const blocks = [];
  const add = (id, title, items, sectionId, options) => {
    if (!items.length) return;
    const details = el('details', { className: 'extra', attrs: { 'data-block': id } }, [
      el('summary', { attrs: { 'data-focus-id': `block:${id}` } }, [el('span', { text: title }), el('span', { className: 'chip-count', text: String(items.length) })]),
      el('ul', {}, items.map((item) => renderExtraItem(item, sectionId ?? item.sectionId, options))),
    ]);
    if (state.openBlocks.has(id)) details.open = true;
    details.addEventListener('toggle', () => (details.open ? state.openBlocks.add(id) : state.openBlocks.delete(id)));
    blocks.push(details);
  };
  if (view) {
    if (!hidden.has('mentioned')) add('issues', 'Задачи с упоминанием', view.sections.mentioned.filter((item) => item.kind === 'issue'), 'mentioned');
    if (!hidden.has('possibleMrs')) add('possibleMrs', SECTION_TITLES.possibleMrs, view.sections.possibleMrs, 'possibleMrs');
    if (!hidden.has('brokenBranches')) add('brokenBranches', SECTION_TITLES.brokenBranches, view.sections.brokenBranches, 'brokenBranches');
    add('snoozed', 'Отложено', view.snoozed, 'snoozed', { snoozedBlock: true });
  }
  $('extras').replaceChildren(...blocks);
}

function partialSummary(dashboard) {
  const parts = Object.entries(PARTIAL_FLAGS).filter(([flag]) => dashboard[flag]).map(([, label]) => label);
  if (dashboard.skippedProjects) parts.push(`пропущено проектов: ${dashboard.skippedProjects}`);
  if (dashboard.skippedMrs) parts.push(`пропущено MR: ${dashboard.skippedMrs}`);
  return parts;
}

function renderStatus() {
  const radar = state.radar;
  const dashboard = radar?.dashboard;
  const notes = [];
  if (!radar?.enabled) notes.push(el('span', { text: 'Радар выключен в настройках' }));
  else if (!dashboard) notes.push(el('span', { text: radar?.error ? '' : 'Радар ещё не обновлялся…' }));
  else {
    const stats = dashboard.stats ? `запросов: ${dashboard.stats.requests}, ${Math.round(dashboard.stats.durationMs / 100) / 10} с` : '';
    const watched = typeof dashboard.watchedProjects === 'number' ? `проектов: ${dashboard.watchedProjects}` : '';
    notes.push(
      el('span', { text: `@${dashboard.username} · радар обновлён`, attrs: { title: [watched, stats].filter(Boolean).join(' · ') } }),
      ageNode(dashboard.fetchedAt, ' назад'),
    );
    const partial = partialSummary(dashboard);
    if (partial.length) notes.push(tag(`не загрузилось: ${partial.join(', ')}`, 'warn'));
  }
  if (state.forceCheck) notes.push(el('span', { className: state.forceCheck.error ? 'tag bad' : 'muted', text: state.forceCheck.text }));
  $('radar-meta').replaceChildren(...notes);
  const banners = [];
  if (radar?.enabled && radar?.error) banners.push(el('div', { className: 'flash err', text: `Радар: ${radar.error}` }));
  if (radar?.enabled && dashboard?.warning) banners.push(el('div', { className: 'flash warn', text: dashboard.warning }));
  const problems = radar?.enabled ? tokenProblems(dashboard?.token, state.userState.dismissedTokenProblems ?? {}, Date.now(), ALLOWED_TOKEN_SCOPES) : [];
  problems.forEach((problem) => {
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
}

function updateCounters(count) {
  const counter = $('nav-count-mr');
  if (counter) counter.textContent = count ? String(count) : '';
  document.title = `${count ? `(${count}) ` : ''}${BASE_TITLE}`;
}

function focusedId() {
  return document.activeElement?.closest?.('[data-focus-id]')?.getAttribute('data-focus-id') ?? null;
}

function render() {
  state.pendingRender = false;
  const focus = focusedId();
  const view = currentView();
  const rows = buildRows(view, state.mrs, state.radar?.hiddenSections ?? []);
  const actionable = visibleActionable(view, state.radar?.hiddenSections ?? [], actionableKeys);
  renderStatus();
  renderChips(rows, actionable);
  const visible = rows.filter((row) => matchesFilter(row, state.filter, actionable) && matchesSearch(row, state.search));
  $('mr-body').replaceChildren(...visible.map(renderRow));
  const empty = $('no-match');
  empty.hidden = visible.length > 0;
  empty.textContent = rows.length ? (state.filter === 'action' ? 'Ничего не требует действия' : 'Ничего не найдено') : 'Открытых MR нет';
  renderExtras(view);
  updateCounters(rows.filter((row) => actionable.has(row.key)).length);
  scheduleSnoozeExpiry();
  if (focus) focusById(focus);
}

function hasSelection() {
  return Boolean(window.getSelection()?.toString());
}

function renderWhenIdle() {
  if (hasSelection() || state.openSnoozeKey !== null) {
    state.pendingRender = true;
    return;
  }
  render();
}

let snoozeTimer = null;

function scheduleSnoozeExpiry() {
  clearTimeout(snoozeTimer);
  const next = nextSnoozeExpiry(state.userState.snoozed ?? {}, Date.now());
  if (next) snoozeTimer = setTimeout(renderWhenIdle, Math.max(1000, next - Date.now() + 500));
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

const normalizeUserState = (value) => ({ snoozed: {}, dismissedTokenProblems: {}, ...(value ?? {}) });

async function fetchUserState() {
  const response = await fetch('/api/ui-state/radar', { cache: 'no-store' });
  if (!response.ok) throw new Error(`ui-state: ${response.status}`);
  return normalizeUserState(await response.json());
}

let userStateWrite = Promise.resolve();

function updateUserState(change) {
  state.userStateVersion += 1;
  state.userState = change(state.userState);
  render();
  userStateWrite = userStateWrite
    .then(async () => {
      const fresh = await fetchUserState().catch(() => state.userState);
      state.userState = change(fresh);
      const response = await fetch('/api/ui-state/radar', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'lpdev' },
        body: JSON.stringify(state.userState),
      });
      if (!response.ok) throw new Error(String(response.status));
      state.userStateVersion += 1;
      renderWhenIdle();
    })
    .catch(() => showStatus({ text: 'не удалось сохранить отложенные — повтори позже', error: true }));
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

function trackFinishedReviews() {
  const marks = finishedReviewMarks(state.mrs);
  if (!document.hidden || !state.finishedBaseline) {
    state.finishedBaseline = marks;
    return;
  }
  if ([...marks].some((mark) => !state.finishedBaseline.has(mark)) && iconLink) {
    iconLink.setAttribute('href', '/static/favicon-dot.svg');
  }
}

let refreshTimer = null;
let inFlight = false;

async function refresh() {
  if (inFlight) return;
  inFlight = true;
  const userStateVersion = state.userStateVersion;
  try {
    const [mrsResponse, radarResponse, userState] = await Promise.all([
      fetch('/api/mrs', { cache: 'no-store' }),
      fetch('/api/radar', { cache: 'no-store' }),
      fetchUserState().catch(() => null),
    ]);
    if (mrsResponse.ok) state.mrs = await mrsResponse.json();
    if (radarResponse.ok) state.radar = await radarResponse.json();
    if (userState && userStateVersion === state.userStateVersion) state.userState = userState;
    trackFinishedReviews();
    renderWhenIdle();
  } catch {
  } finally {
    inFlight = false;
    clearTimeout(refreshTimer);
    const active = state.mrs.some((mr) => ACTIVE_REVIEW_STATUSES.has(mr.latest_review_status));
    refreshTimer = setTimeout(refresh, document.hidden ? REFRESH_HIDDEN_MS : active ? REFRESH_ACTIVE_MS : REFRESH_IDLE_MS);
  }
}

function showStatus(status, clearAfterMs = 12000) {
  clearTimeout(state.forceCheckTimer);
  state.forceCheck = status;
  renderWhenIdle();
  state.forceCheckTimer = setTimeout(() => {
    state.forceCheck = null;
    renderWhenIdle();
  }, clearAfterMs);
}

async function forceCheck(button) {
  button.disabled = true;
  showStatus({ text: 'проверяю…' });
  try {
    const response = await fetch('/force-check', { method: 'POST', headers: { 'X-Requested-With': 'lpdev' } });
    showStatus(
      response.ok ? { text: 'запрошено, обновляю…' } : { text: (await response.text()) || `ошибка ${response.status}`, error: true },
    );
    if (response.ok) [2000, 5000, 10000].forEach((delay) => setTimeout(refresh, delay));
  } catch {
    showStatus({ text: 'сервис не отвечает', error: true });
  } finally {
    setTimeout(() => (button.disabled = false), 2000);
  }
}

function init() {
  const initial = JSON.parse($('initial-data').textContent);
  state.gitlabUrl = initial.gitlabUrl;
  state.mrs = initial.mrs;
  state.radar = initial.radar;
  state.userState = normalizeUserState(initial.userState);
  state.reviewProjects = new Set(initial.reviewProjects ?? []);
  state.finishedBaseline = finishedReviewMarks(state.mrs);
  $('search').addEventListener('input', (event) => {
    state.search = event.target.value;
    render();
  });
  $('force-check').addEventListener('click', (event) => forceCheck(event.currentTarget));
  document.addEventListener('click', (event) => {
    if (state.openSnoozeKey !== null && !event.target.closest('.snooze-wrap')) closeSnoozeMenu();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && state.openSnoozeKey !== null) closeSnoozeMenu({ focusButton: true });
  });
  document.addEventListener('selectionchange', () => {
    if (state.pendingRender) renderWhenIdle();
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    state.finishedBaseline = finishedReviewMarks(state.mrs);
    if (iconLink && ORIGINAL_ICON !== null) iconLink.setAttribute('href', ORIGINAL_ICON);
    refresh();
  });
  render();
  if (location.hash.startsWith('#mr=')) document.querySelector('tr.highlight')?.scrollIntoView({ block: 'center' });
  refreshTimer = setTimeout(refresh, REFRESH_IDLE_MS);
  setInterval(renderWhenIdle, 60000);
}

init();
