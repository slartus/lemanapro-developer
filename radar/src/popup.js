import { DEFAULT_BASE_URL, PROJECT_LABEL_MODES, STORAGE_KEYS, UPDATE_REPOSITORY, compareVersions } from './config.js';
import { GitLabClient, fetchPipelineFailures } from './gitlab.js';
import { failureUrl, isHttpsUrl, isUnderBaseUrl } from './links.js';
import { snoozeOptions, createSnooze, pruneSnoozed } from './snooze.js';
import { REQUIRED_TOKEN_SCOPE, SECTION_META, awaitsAuthor, deriveView, itemsByKey, tokenProblems } from './view.js';

const EMPTY_TEXTS = {
  reviewing: 'Нет MR на ревью',
  assigned: 'Нет назначенных MR',
  mentioned: 'Нет новых упоминаний',
  awaitingApproval: 'Всё заапрувлено',
  threads: 'Нет неотвеченных тредов',
  authored: 'Нет открытых MR',
  possibleMrs: 'Нет веток без MR',
  brokenBranches: 'Default branch зелёные во всех проектах',
};

const SECTIONS = SECTION_META.map(({ id, title }) => ({ id, title, empty: EMPTY_TEXTS[id] }));

const STATUS_TAGS = {
  mergeable: { label: 'можно мёржить', tone: 'ok' },
  can_be_merged: { label: 'можно мёржить', tone: 'ok' },
  not_approved: { label: 'нет апрувов', tone: 'warn' },
  discussions_not_resolved: { label: 'треды', tone: 'warn' },
  ci_must_pass: { label: 'CI не прошёл', tone: 'bad' },
  ci_still_running: { label: 'CI идёт', tone: 'warn' },
  need_rebase: { label: 'нужен rebase', tone: 'bad' },
  conflict: { label: 'конфликт', tone: 'bad' },
  broken_status: { label: 'сломан', tone: 'bad' },
  cannot_be_merged: { label: 'конфликт', tone: 'bad' },
  requested_changes: { label: 'просят правки', tone: 'bad' },
  blocked_status: { label: 'заблокирован', tone: 'bad' },
  checking: { label: 'проверяется', tone: '' },
  unchecked: { label: 'не проверен', tone: '' },
  preparing: { label: 'готовится', tone: '' },
  approvals_syncing: { label: 'апрувы синхронизируются', tone: '' },
  draft_status: { label: 'draft', tone: 'warn' },
  not_open: { label: 'закрыт', tone: '' },
  commits_status: { label: 'проблема с коммитами', tone: 'bad' },
  merge_request_blocked: { label: 'заблокирован другим MR', tone: 'bad' },
  status_checks_must_pass: { label: 'внешние проверки', tone: 'bad' },
  jira_association_missing: { label: 'нет задачи Jira', tone: 'bad' },
  title_regex: { label: 'название не по шаблону', tone: 'bad' },
  locked_paths: { label: 'заблокированы пути', tone: 'bad' },
  locked_lfs_files: { label: 'заблокированы LFS-файлы', tone: 'bad' },
  security_policy_violations: { label: 'нарушены политики', tone: 'bad' },
  merge_time: { label: 'ждёт времени мёржа', tone: 'warn' },
};

const AGE_THRESHOLDS = [
  { days: 7, tone: 'bad' },
  { days: 3, tone: 'warn' },
];
const DAY_MS = 24 * 60 * 60 * 1000;

const PIPELINE_STATUS_TAGS = {
  success: { label: 'CI ок', tone: 'ok' },
  running: { label: 'CI идёт', tone: 'warn' },
  pending: { label: 'CI идёт', tone: 'warn' },
  created: { label: 'CI идёт', tone: 'warn' },
  preparing: { label: 'CI идёт', tone: 'warn' },
  waiting_for_resource: { label: 'CI идёт', tone: 'warn' },
  scheduled: { label: 'CI запланирован', tone: '' },
  manual: { label: 'CI ждёт ручного запуска', tone: 'warn' },
  waiting_for_callback: { label: 'CI идёт', tone: 'warn' },
  canceling: { label: 'CI отменяется', tone: '' },
  canceled: { label: 'CI отменён', tone: '' },
  skipped: { label: 'CI пропущен', tone: '' },
};

const CI_MERGE_STATUSES = new Set(['ci_must_pass', 'ci_still_running']);

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

const SNOOZED_SECTION = { id: 'snoozed', title: 'Отложено', empty: '' };

const pipelineFailures = new Map();
const expandedPipelines = new Set();
let currentDashboard = null;
let currentUserState = {};
let pendingUserStateWrite = Promise.resolve();
let selectedProject = null;
let filterIdentity = null;
let expandedSections = {};
let labelMode = PROJECT_LABEL_MODES.name;
let hiddenSections = [];

const elements = {
  meta: document.getElementById('meta'),
  error: document.getElementById('error'),
  update: document.getElementById('update'),
  token: document.getElementById('token'),
  filterBar: document.getElementById('filter-bar'),
  chip: document.getElementById('project-filter-chip'),
  menu: document.getElementById('project-filter-menu'),
  sections: document.getElementById('sections'),
  refresh: document.getElementById('refresh'),
  settings: document.getElementById('settings'),
};

function createElement(tag, { className, text, attrs } = {}, children = []) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  Object.entries(attrs ?? {}).forEach(([key, value]) => element.setAttribute(key, value));
  element.append(...children);
  return element;
}

const tag = (label, tone = '', attrs = {}) => createElement('span', { className: `tag ${tone}`.trim(), text: label, attrs });

function formatRelative(isoDate) {
  const minutes = Math.round((Date.now() - new Date(isoDate).getTime()) / 60000);
  if (minutes < 1) return 'только что';
  if (minutes < 60) return `${minutes} мин назад`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} ч назад`;
  return `${Math.round(hours / 24)} д назад`;
}

function renderAge(isoDate) {
  const ageDays = (Date.now() - new Date(isoDate).getTime()) / DAY_MS;
  const tone = AGE_THRESHOLDS.find(({ days }) => ageDays >= days)?.tone;
  return createElement('span', {
    className: tone ? `age ${tone}` : 'age',
    text: formatRelative(isoDate),
    attrs: { title: new Date(isoDate).toLocaleString('ru-RU') },
  });
}

const openUrl = (url) => chrome.tabs.create({ url });

function deleteBranchUrl(item, baseUrl) {
  const url = new URL(baseUrl);
  url.pathname = `${url.pathname.replace(/\/+$/, '')}/${item.projectPath}/-/branches`;
  url.searchParams.set('state', 'all');
  url.searchParams.set('sort', 'updated_desc');
  url.searchParams.set('search', item.title);
  return url.href;
}

function describeFailure(failure) {
  if (failure.kind === 'downstream') {
    return 'дочерний пайплайн';
  }
  if (failure.kind === 'external') {
    const host = isHttpsUrl(failure.webUrl) ? new URL(failure.webUrl).hostname : null;
    return [failure.reason, host].filter(Boolean).join(' · ') || 'внешняя проверка';
  }
  return FAILURE_REASONS[failure.reason] ?? failure.reason ?? 'причина неизвестна';
}

async function loadPipelineFailures(item, baseUrl) {
  const { settings } = await chrome.storage.local.get(STORAGE_KEYS.settings);
  if ((settings?.baseUrl ?? DEFAULT_BASE_URL) !== baseUrl) {
    throw new Error('настройки изменились, обновите список');
  }
  const client = new GitLabClient(baseUrl, settings.token);
  return fetchPipelineFailures(client, item.pipeline);
}

function renderFailureRow(failure, baseUrl) {
  const url = failureUrl(failure, baseUrl);
  const title = failure.stage && failure.stage !== 'external' ? `${failure.stage} / ${failure.name}` : failure.name;
  const children = [
    createElement('span', { className: 'ci-job-name', text: `✖ ${title}` }),
    createElement('span', { className: 'ci-job-reason', text: describeFailure(failure), attrs: { title: describeFailure(failure) } }),
  ];
  if (!url) {
    return createElement('div', { className: 'ci-job' }, children);
  }
  const link = createElement('a', { className: 'ci-job', attrs: { href: url, title: url } }, children);
  link.addEventListener('click', (event) => {
    event.preventDefault();
    openUrl(url);
  });
  return link;
}

function renderCiPanel(item, baseUrl, rerender) {
  const state = pipelineFailures.get(item.pipeline.id);
  if (!state || state.status === 'loading') {
    return [createElement('div', { className: 'ci-note', text: 'Загружаю упавшие джобы…' })];
  }
  if (state.status === 'error') {
    const retry = createElement('button', { className: 'ci-retry', text: 'повторить', attrs: { type: 'button' } });
    retry.addEventListener('click', () => {
      pipelineFailures.delete(item.pipeline.id);
      rerender();
    });
    return [createElement('div', { className: 'ci-note bad', text: `Не удалось загрузить: ${state.message} ` }, [retry])];
  }
  if (state.failures.length === 0) {
    const pipelineUrl = isUnderBaseUrl(item.pipeline.webUrl, baseUrl) ? item.pipeline.webUrl : baseUrl;
    const link = createElement('a', { className: 'ci-job', text: 'Упавших джоб не найдено — открыть пайплайн', attrs: { href: pipelineUrl } });
    link.addEventListener('click', (event) => {
      event.preventDefault();
      openUrl(pipelineUrl);
    });
    return [link];
  }
  return state.failures.map((failure) => renderFailureRow(failure, baseUrl));
}

function rerenderCiBlocks(pipelineId) {
  document.querySelectorAll(`.ci[data-pipeline-id="${pipelineId}"]`).forEach((block) => block.rerender?.());
}

function renderCiBlock(item, baseUrl) {
  const pipelineId = item.pipeline.id;
  const block = createElement('div', { className: 'ci', attrs: { 'data-pipeline-id': String(pipelineId) } });
  if (Array.isArray(item.pipeline.failures)) {
    pipelineFailures.set(pipelineId, { status: 'loaded', failures: item.pipeline.failures });
  }
  const onlyIgnored = Boolean(item.pipeline.onlyIgnoredFailures);
  block.classList.toggle('ignored', onlyIgnored);
  const label = onlyIgnored
    ? `⏸ CI ждёт: ${[...new Set(item.pipeline.failures.map((failure) => failure.name))].join(', ')}`
    : '✖ CI упал';

  const rerender = () => {
    const expanded = expandedPipelines.has(pipelineId);
    const toggle = createElement('button', {
      className: onlyIgnored ? 'ci-toggle ignored' : 'ci-toggle',
      text: `${label} ${expanded ? '▾' : '▸'}`,
      attrs: { type: 'button', 'aria-expanded': String(expanded) },
    });
    toggle.addEventListener('click', () => {
      if (expanded) {
        expandedPipelines.delete(pipelineId);
      } else {
        expandedPipelines.add(pipelineId);
      }
      rerender();
    });
    const children = [toggle];
    if (expanded) {
      if (!pipelineFailures.has(pipelineId)) {
        pipelineFailures.set(pipelineId, { status: 'loading' });
        loadPipelineFailures(item, baseUrl)
          .then((failures) => pipelineFailures.set(pipelineId, { status: 'loaded', failures }))
          .catch((error) => pipelineFailures.set(pipelineId, { status: 'error', message: error.message }))
          .finally(() => rerenderCiBlocks(pipelineId));
      }
      children.push(createElement('div', { className: 'ci-panel' }, renderCiPanel(item, baseUrl, rerender)));
    }
    block.replaceChildren(...children);
  };
  block.rerender = rerender;
  rerender();
  return block;
}

function authorLabel(item, sectionId) {
  if (sectionId === 'mentioned') {
    return `от @${item.mentionedBy.join(', @')}`;
  }
  return item.author ? `@${item.author}` : null;
}

function statusTag(item) {
  const pipelineStatus = item.pipeline ? PIPELINE_STATUS_TAGS[item.pipeline.status] : null;
  const hasPipelineTag = Boolean(pipelineStatus) || item.pipeline?.status === 'failed';
  const status = hasPipelineTag && CI_MERGE_STATUSES.has(item.status) ? null : STATUS_TAGS[item.status];
  return status && !(item.draft && item.status === 'draft_status') ? tag(status.label, status.tone) : null;
}

function renderItem(item, sectionId, baseUrl) {
  const targetUrl =
    (sectionId === 'possibleMrs' && item.mrUrl) ||
    (sectionId === 'mentioned' && item.mentionUrl) ||
    item.webUrl;
  const webUrl = isUnderBaseUrl(targetUrl, baseUrl)
    ? targetUrl
    : isUnderBaseUrl(item.webUrl, baseUrl)
      ? item.webUrl
      : baseUrl;
  const tags = [];
  if (item.kind === 'issue') tags.push(tag('задача'));
  if (item.draft) tags.push(tag('draft'));
  if (item.author && item.author === currentDashboard?.username && sectionId !== 'authored') tags.push(tag('мой', 'mine'));
  if (sectionId === SNOOZED_SECTION.id) tags.push(tag(formatSnooze(item.snooze)));
  if (sectionId === 'mentioned' && item.mentionCount > 1) tags.push(tag(`упоминаний: ${item.mentionCount}`, 'warn'));
  if (['reviewing', 'assigned'].includes(sectionId) && item.approvedByMe) tags.push(tag('я апрувнул', 'ok'));
  if (['reviewing', 'assigned', 'awaitingApproval'].includes(sectionId) && !item.approvedByMe && awaitsAuthor(item)) {
    tags.push(tag(item.myReviewState === 'requested_changes' ? 'я попросил правки' : 'я отревьюил'));
  }
  if (item.threadsAwaitingReply > 0) tags.push(tag(`ждут ответа: ${item.threadsAwaitingReply}`, 'warn'));
  const showsPipeline = sectionId === 'authored' && Boolean(item.pipeline);
  if (sectionId === 'authored') {
    const approvers = item.approverNames?.length ? item.approverNames.join(', ') : null;
    tags.push(
      createElement('span', {
        className: `tag ${item.approvalsCount > 0 ? 'ok' : ''}`.trim(),
        text: `апрувов: ${item.approvalsCount}`,
        attrs: approvers ? { title: approvers } : {},
      }),
    );
    const pipelineStatus = showsPipeline ? PIPELINE_STATUS_TAGS[item.pipeline.status] : null;
    const status = statusTag(item);
    if (status) tags.push(status);
    if (pipelineStatus) tags.push(tag(pipelineStatus.label, pipelineStatus.tone));
  } else if (sectionId === 'assigned') {
    const status = statusTag(item);
    if (status) tags.push(status);
    if (item.hasConflicts && item.status !== 'conflict') tags.push(tag('конфликт', 'bad'));
  } else if (item.hasConflicts) {
    tags.push(tag('конфликт', 'bad'));
  }
  if (sectionId === 'brokenBranches') tags.push(tag('CI упал', 'bad'));
  if (sectionId === 'possibleMrs') {
    const mrTagAttrs = item.mrTitle ? { title: `!${item.mrIid} ${item.mrTitle}` } : {};
    if (item.mrState === 'merged') {
      tags.push(tag('смержена', 'warn', mrTagAttrs));
    } else if (item.mrState === 'closed') {
      tags.push(tag('MR закрыт', 'warn', mrTagAttrs));
    } else {
      tags.push(tag('создать MR', 'ok'));
    }
    if (item.merged && !item.mrState) tags.push(tag('смержена', 'warn'));
  }

  const author = sectionId === 'authored' ? null : authorLabel(item, sectionId);
  const subline = createElement('span', { className: 'sub' }, [
    createElement('span', { text: item.reference }),
    ...(author ? [createElement('span', { text: author })] : []),
    renderAge(sectionId === 'mentioned' ? (item.mentionedAt ?? item.updatedAt) : item.updatedAt),
    ...tags,
  ]);
  const link = createElement('a', { attrs: { href: webUrl, title: item.title } }, [
    createElement('span', { className: 'title', text: item.title }),
    subline,
  ]);
  link.addEventListener('click', (event) => {
    event.preventDefault();
    openUrl(webUrl);
  });
  const ciBlock = showsPipeline && item.pipeline.status === 'failed' ? [renderCiBlock(item, baseUrl)] : [];
  const row = createElement('div', { className: 'row' }, [link, ...renderRowAction(item, sectionId, baseUrl)]);
  return createElement('li', {}, [row, ...ciBlock]);
}

function formatSnooze(snooze) {
  if (snooze?.forever) {
    return 'навсегда';
  }
  if (!snooze?.until) {
    return 'до изменений';
  }
  const until = new Date(snooze.until);
  const sameDay = until.toDateString() === new Date().toDateString();
  const time = until.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  return sameDay ? `до ${time}` : `до ${until.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' })} ${time}`;
}

const isCompleteDashboard = (dashboard) =>
  Boolean(dashboard?.sections) &&
  !dashboard.error &&
  !dashboard.skippedMrs &&
  !dashboard.skippedProjects &&
  !dashboard.projectsUnavailable &&
  !dashboard.mentionsUnavailable &&
  !dashboard.assignedUnavailable &&
  !dashboard.defaultBranchesUnavailable &&
  !dashboard.possibleMrsUnavailable;

async function writeUserState(change) {
  const stored = await chrome.storage.local.get(STORAGE_KEYS.userState);
  await chrome.storage.local.set({ [STORAGE_KEYS.userState]: change(stored[STORAGE_KEYS.userState] ?? {}) });
}

function updateUserState(change) {
  pendingUserStateWrite = pendingUserStateWrite.then(() => writeUserState(change)).catch(console.error);
  return pendingUserStateWrite;
}

const persistUi = () =>
  updateUserState((userState) => ({
    ...userState,
    ui: { ...userState.ui, [filterIdentity]: { project: selectedProject, sections: expandedSections } },
  }));

const updateSnoozed = (change) =>
  updateUserState((userState) => {
    const current = isCompleteDashboard(currentDashboard)
      ? pruneSnoozed(userState.snoozed, itemsByKey(currentDashboard))
      : (userState.snoozed ?? {});
    return { ...userState, snoozed: change(current) };
  });

const dismissTokenProblem = ({ kind, dismissKey }) =>
  updateUserState((userState) => ({
    ...userState,
    dismissedTokenProblems: { ...userState.dismissedTokenProblems, [kind]: dismissKey },
  }));

const snoozeItem = (item, option) =>
  updateSnoozed((snoozed) => ({ ...snoozed, [item.key]: createSnooze(option, item) }));

const unsnoozeItem = (item) =>
  updateSnoozed(({ [item.key]: _removed, ...snoozed }) => snoozed);

function renderSnoozeMenu(item, sectionId) {
  return createElement(
    'div',
    { className: 'snooze-menu', attrs: { role: 'group', 'aria-label': 'Отложить MR' } },
    Object.entries(snoozeOptions(sectionId)).map(([option, { label, title }]) => {
      const button = createElement('button', { text: label, attrs: { type: 'button', ...(title && { title }) } });
      button.addEventListener('click', () => snoozeItem(item, option));
      return button;
    }),
  );
}

function renderRowAction(item, sectionId, baseUrl) {
  if (sectionId === SNOOZED_SECTION.id) {
    const button = createElement('button', {
      className: 'row-action visible',
      text: '↩',
      attrs: { type: 'button', title: 'Вернуть', 'aria-label': `Вернуть: ${item.title}` },
    });
    button.addEventListener('click', () => unsnoozeItem(item));
    return [button];
  }
  const snooze = createElement('button', {
    className: 'row-action',
    text: '⏰',
    attrs: { type: 'button', title: 'Отложить', 'aria-label': `Отложить: ${item.title}`, 'aria-expanded': 'false' },
  });
  snooze.addEventListener('click', () => {
    const li = snooze.closest('li');
    const existing = li.querySelector('.snooze-menu');
    if (existing) {
      existing.remove();
      snooze.setAttribute('aria-expanded', 'false');
      return;
    }
    li.querySelector('.row').after(renderSnoozeMenu(item, sectionId));
    snooze.setAttribute('aria-expanded', 'true');
  });
  if (sectionId !== 'possibleMrs') {
    return [snooze];
  }
  const removeBranch = createElement('button', {
    className: 'row-action',
    text: '🗑',
    attrs: { type: 'button', title: 'Удалить ветку в GitLab', 'aria-label': `Удалить ветку: ${item.title}` },
  });
  removeBranch.addEventListener('click', () => openUrl(deleteBranchUrl(item, baseUrl)));
  return [snooze, removeBranch];
}

function renderSection(section, items, baseUrl, { collapsed = false, emptyText } = {}) {
  const summary = createElement('summary', { text: section.title }, [
    createElement('span', { className: 'count', text: String(items.length) }),
  ]);
  const body = items.length
    ? createElement('ul', {}, items.map((item) => renderItem(item, section.id, baseUrl)))
    : createElement('div', { className: 'empty', text: emptyText ?? section.empty });
  const details = createElement('details', {}, [summary, body]);
  const defaultOpen = items.length > 0 && !collapsed;
  details.open = expandedSections[section.id] ?? defaultOpen;
  details.addEventListener('toggle', () => {
    if (details.open === (expandedSections[section.id] ?? defaultOpen)) {
      return;
    }
    expandedSections[section.id] = details.open;
    persistUi();
  });
  return details;
}

function renderTokenProblem(problem) {
  const text = createElement('span', { text: problem.text });
  if (!problem.dismissKey) {
    return createElement('div', { className: 'notice-row' }, [text]);
  }
  const dismiss = createElement('button', {
    className: 'notice-dismiss',
    text: 'Скрыть',
    attrs: { type: 'button', 'aria-label': `Скрыть: ${problem.text}` },
  });
  dismiss.addEventListener('click', () => dismissTokenProblem(problem));
  return createElement('div', { className: 'notice-row' }, [text, dismiss]);
}

function renderTokenNotice(dashboard, userState) {
  const problems = dashboard?.baseUrl ? tokenProblems(dashboard.token, userState?.dismissedTokenProblems) : [];
  elements.token.hidden = problems.length === 0;
  if (!problems.length) {
    elements.token.replaceChildren();
    return;
  }
  const url = `${dashboard.baseUrl}/-/user_settings/personal_access_tokens?name=MR+Radar&scopes=${REQUIRED_TOKEN_SCOPE}`;
  const link = createElement('a', { text: 'Создать новый токен', attrs: { href: url } });
  link.addEventListener('click', (event) => {
    event.preventDefault();
    openUrl(url);
  });
  elements.token.replaceChildren(...problems.map(renderTokenProblem), link);
}

function projectLabel(item) {
  if (item.projectPath) {
    return item.projectPath;
  }
  if (item.kind === 'branch') {
    return item.title;
  }
  const reference = item.reference ?? '';
  const cut = reference.search(/[!#]/);
  return cut > 0 ? reference.slice(0, cut) : null;
}

function projectCounts(dashboard) {
  const counts = new Map();
  SECTIONS.filter(({ id }) => !hiddenSections.includes(id)).forEach(({ id }) => {
    (dashboard.sections?.[id] ?? []).forEach((item) => {
      const label = projectLabel(item);
      if (label) {
        counts.set(label, (counts.get(label) ?? 0) + 1);
      }
    });
  });
  return [...counts].sort(([a], [b]) => a.localeCompare(b, 'ru'));
}

function projectName(path) {
  return path.split('/').at(-1);
}

function projectDisplay(path) {
  return labelMode === PROJECT_LABEL_MODES.fullPath ? path : projectName(path);
}

const readLabelMode = (settings) =>
  PROJECT_LABEL_MODES[settings?.projectLabelMode] ?? PROJECT_LABEL_MODES.name;

function chooseProject(name) {
  selectedProject = name;
  render(currentDashboard, currentUserState);
  persistUi();
}

function renderProjectFilter(dashboard, userState) {
  const identity = `${dashboard.baseUrl ?? ''}/${dashboard.username ?? ''}`;
  if (identity !== filterIdentity) {
    filterIdentity = identity;
    const ui = userState.ui?.[identity] ?? {};
    selectedProject = ui.project ?? null;
    expandedSections = ui.sections ?? {};
  }
  const projects = projectCounts(dashboard);
  if (selectedProject && !projects.some(([name]) => name === selectedProject)) {
    selectedProject = null;
  }
  elements.filterBar.hidden = projects.length === 0;
  elements.chip.textContent = selectedProject ? projectDisplay(selectedProject) : 'Все проекты';
  elements.chip.setAttribute('aria-expanded', 'false');
  elements.menu.hidden = true;
  const option = (name, count) => {
    const button = createElement('button', {
      className: 'chip-option',
      text: count === undefined ? 'Все проекты' : `${projectDisplay(name)} (${count})`,
      attrs: {
        type: 'button',
        role: 'option',
        'aria-selected': String(name === selectedProject),
      },
    });
    button.addEventListener('click', () => chooseProject(name));
    return button;
  };
  elements.menu.replaceChildren(option(null), ...projects.map(([name, count]) => option(name, count)));
}

function render(dashboard, userState) {
  currentDashboard = dashboard;
  currentUserState = userState;
  renderTokenNotice(dashboard, userState);
  const problem = dashboard?.error ?? dashboard?.warning;
  elements.error.hidden = !problem;
  elements.error.textContent = problem ?? '';
  const skipped = [
    dashboard?.watchedProjects !== undefined && !dashboard.projectsUnavailable
      ? ` · проектов для апрува: ${dashboard.watchedProjects}`
      : '',
    dashboard?.skippedProjects ? ` · пропущено проектов: ${dashboard.skippedProjects}` : '',
    dashboard?.skippedMrs ? ` · пропущено MR: ${dashboard.skippedMrs}` : '',
    dashboard?.projectsUnavailable ? ' · проекты для апрува недоступны' : '',
    dashboard?.pipelinesUnavailable ? ' · статусы CI недоступны' : '',
    dashboard?.mentionsUnavailable ? ' · упоминания недоступны' : '',
    dashboard?.assignedUnavailable ? ' · назначенные MR недоступны' : '',
    dashboard?.defaultBranchesUnavailable ? ' · статусы default branch недоступны' : '',
    dashboard?.possibleMrsUnavailable ? ' · ветки недоступны' : '',
  ].join('');
  elements.meta.textContent = dashboard?.fetchedAt
    ? `@${dashboard.username} · ${formatRelative(dashboard.fetchedAt)}${skipped}`
    : '';
  elements.meta.title = dashboard?.stats
    ? `Последнее успешное обновление: запросов к GitLab — ${dashboard.stats.requests}, ${(dashboard.stats.durationMs / 1000).toFixed(1)} с`
    : '';
  if (!dashboard?.sections || !dashboard.baseUrl) {
    elements.filterBar.hidden = true;
    elements.chip.textContent = 'Все проекты';
    elements.menu.replaceChildren();
    elements.menu.hidden = true;
    elements.sections.replaceChildren();
    return;
  }
  const view = deriveView(dashboard, userState, Date.now(), hiddenSections);
  renderProjectFilter(dashboard, userState);
  const matchesProject = (item) => {
    if (!selectedProject) {
      return true;
    }
    const label = projectLabel(item);
    return label === null || label === selectedProject;
  };
  const sections = Object.fromEntries(
    Object.entries(view.sections).map(([id, items]) => [id, items.filter(matchesProject)]),
  );
  const snoozed = view.snoozed.filter(matchesProject);
  const emptyText = selectedProject ? 'Нет MR в этом проекте' : undefined;
  elements.sections.replaceChildren(
    ...SECTIONS.filter((section) => !hiddenSections.includes(section.id)).map((section) =>
      renderSection(section, sections[section.id], dashboard.baseUrl, { emptyText }),
    ),
    ...(snoozed.length
      ? [renderSection(SNOOZED_SECTION, snoozed, dashboard.baseUrl, { collapsed: true, emptyText })]
      : []),
  );
}

function renderUpdate(update) {
  const currentVersion = chrome.runtime.getManifest().version;
  const isNewer =
    Boolean(update?.version) &&
    String(update.url).startsWith(`https://github.com/${UPDATE_REPOSITORY}/releases/`) &&
    compareVersions(update.version, currentVersion) > 0;
  elements.update.hidden = !isNewer;
  if (!isNewer) {
    return;
  }
  const link = createElement('a', { text: `Доступна версия ${update.version}`, attrs: { href: update.url } });
  link.addEventListener('click', (event) => {
    event.preventDefault();
    openUrl(update.url);
  });
  elements.update.replaceChildren(
    link,
    createElement('span', { className: 'muted', text: `у вас ${currentVersion} · git pull → ↻ в chrome://extensions` }),
  );
}

async function loadAndRender() {
  const stored = await chrome.storage.local.get([
    STORAGE_KEYS.dashboard,
    STORAGE_KEYS.settings,
    STORAGE_KEYS.update,
    STORAGE_KEYS.userState,
  ]);
  labelMode = readLabelMode(stored[STORAGE_KEYS.settings]);
  hiddenSections = stored[STORAGE_KEYS.settings]?.hiddenSections ?? [];
  render(stored[STORAGE_KEYS.dashboard], stored[STORAGE_KEYS.userState] ?? {});
  renderUpdate(stored[STORAGE_KEYS.update]);
}

elements.refresh.addEventListener('click', async () => {
  elements.refresh.disabled = true;
  try {
    await chrome.runtime.sendMessage({ type: 'refresh' });
  } finally {
    elements.refresh.disabled = false;
  }
});

elements.settings.addEventListener('click', () => chrome.runtime.openOptionsPage());

elements.chip.addEventListener('click', () => {
  const open = elements.menu.hidden;
  elements.menu.hidden = !open;
  elements.chip.setAttribute('aria-expanded', String(!open));
});

document.addEventListener('click', (event) => {
  if (!elements.filterBar.contains(event.target)) {
    elements.menu.hidden = true;
    elements.chip.setAttribute('aria-expanded', 'false');
  }
});

const onlyUiChanged = ({ oldValue, newValue }) =>
  JSON.stringify({ ...(oldValue ?? {}), ui: undefined }) === JSON.stringify({ ...(newValue ?? {}), ui: undefined });

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local') {
    return;
  }
  const userStateChanged = changes[STORAGE_KEYS.userState];
  const userStateChangedBeyondUi = userStateChanged && !onlyUiChanged(userStateChanged);
  if (changes[STORAGE_KEYS.dashboard] || userStateChangedBeyondUi || changes[STORAGE_KEYS.settings]) {
    if (changes[STORAGE_KEYS.settings]) {
      labelMode = readLabelMode(changes[STORAGE_KEYS.settings].newValue);
      hiddenSections = changes[STORAGE_KEYS.settings].newValue?.hiddenSections ?? [];
    }
    render(
      changes[STORAGE_KEYS.dashboard] ? changes[STORAGE_KEYS.dashboard].newValue : currentDashboard,
      userStateChanged ? (userStateChanged.newValue ?? {}) : currentUserState,
    );
  }
  if (changes[STORAGE_KEYS.update]) {
    renderUpdate(changes[STORAGE_KEYS.update].newValue);
  }
});

loadAndRender();
