import { SECTION_META } from '/static/radar/view.js';
import {
  addProjectByPath,
  buildTree,
  groupRadarState,
  matchesQuery,
  mergeProjects,
  projectPathFromInput,
  projectsUnder,
  settingsPayloadProjects,
} from '/static/settings-model.js';

const initial = JSON.parse(document.getElementById('settings-data').textContent);
const DEFAULT_AGENT = initial.agents[0] ?? 'mr-watch-reviewer';
const state = {
  projects: mergeProjects([], initial.reviewProjects, initial.radar.selected_project_ids),
  query: '',
  openGroups: new Set(),
  loaded: false,
  loadError: null,
};
let searchTimer = null;

const $ = (id) => document.getElementById(id);

function el(tag, { className, text, attrs } = {}, children = []) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  Object.entries(attrs ?? {}).forEach(([key, value]) => node.setAttribute(key, value));
  node.append(...children.filter(Boolean));
  return node;
}

function setStatus(text, kind) {
  const status = $('status');
  status.textContent = text;
  status.className = `small ${kind || 'muted'}`;
}

const radarSelectedMode = () => $('radar-mode-selected').checked;

function agentSelect(project) {
  const select = el('select', { className: 'wagent', attrs: { 'aria-label': `Агент: ${project.path}`, 'data-focus-id': `${project.key}:agent` } });
  const agents = initial.agents.includes(project.review.agent) || !project.review.agent ? initial.agents : [...initial.agents, project.review.agent];
  agents.forEach((agent) => select.append(el('option', { text: agent, attrs: { value: agent } })));
  select.value = project.review.agent || DEFAULT_AGENT;
  select.addEventListener('change', () => (project.review.agent = select.value));
  return select;
}

function checkbox(className, label, checked, onChange, disabled = false, focusId = null) {
  const input = el('input', { className, attrs: { type: 'checkbox', 'aria-label': label, ...(focusId && { 'data-focus-id': focusId }) } });
  input.checked = checked;
  input.disabled = disabled;
  input.addEventListener('change', () => onChange(input.checked));
  return input;
}

function renderProject(project) {
  const selected = radarSelectedMode();
  const radarTitle = project.maintainer
    ? selected ? 'Наблюдать в радаре' : 'В режиме «все проекты» радар смотрит все Maintainer+ проекты'
    : 'Радар смотрит только проекты, где у тебя Maintainer и выше';
  const radar = checkbox('wradar', `Радар: ${project.path}`, selected ? project.radar : project.maintainer, (value) => {
    project.radar = value;
    render();
  }, !selected || (!project.maintainer && !project.radar), `${project.key}:radar`);
  radar.title = radarTitle;
  const review = checkbox('wreview', `Авторевью: ${project.path}`, Boolean(project.review), (value) => {
    project.review = value ? { agent: DEFAULT_AGENT } : null;
    render();
  }, false, `${project.key}:review`);
  const extra = project.review
    ? el('span', { className: 'review-opts' }, [agentSelect(project)])
    : null;
  return el('div', { className: 'tree-row', attrs: { 'data-project-id': project.key } }, [
    el('span', { className: 'tree-name' }, [
      el('span', { text: project.name }),
      el('span', { className: 'muted small', text: project.known ? ` ${project.path}` : project.id === null ? ' добавлен по ссылке' : ' нет в списке GitLab' }),
    ]),
    el('label', { className: 'tree-check', attrs: { title: radarTitle } }, [radar, el('span', { className: 'tree-check-label', text: 'Радар' })]),
    el('label', { className: 'tree-check' }, [review, el('span', { className: 'tree-check-label', text: 'Авторевью' })]),
    extra ?? el('span'),
  ]);
}

function renderGroup(node, depth) {
  const all = projectsUnder(node);
  const visible = all.filter((project) => matchesQuery(project, state.query));
  if (!visible.length) return null;
  const selected = radarSelectedMode();
  const maintained = all.filter((project) => project.maintainer);
  const radarState = selected ? groupRadarState(node) : maintained.length ? 'all' : 'none';
  const groupBox = checkbox(
    'wradar-group',
    `Радар: вся группа ${node.path}`,
    radarState === 'all',
    (value) => {
      maintained.forEach((project) => (project.radar = value));
      render();
    },
    !selected || !maintained.length,
    `group:${node.path}`,
  );
  groupBox.indeterminate = radarState === 'some';
  const reviewCount = all.filter((project) => project.review).length;
  const radarCount = selected ? all.filter((project) => project.radar).length : 0;
  const details = el('details', { className: 'tree-group', attrs: { 'data-group': node.path } }, [
    el('summary', {}, [
      groupBox,
      el('span', { className: 'tree-group-name', text: node.name }),
      el('span', { className: 'muted small', text: ` ${visible.length}` }),
      radarCount ? el('span', { className: 'tag', text: `радар ${radarCount}` }) : null,
      reviewCount ? el('span', { className: 'tag ok', text: `авторевью ${reviewCount}` }) : null,
    ]),
    el('div', { className: 'tree-children' }, [
      ...[...node.groups.values()].map((child) => renderGroup(child, depth + 1)),
      ...node.projects.filter((project) => matchesQuery(project, state.query)).map(renderProject),
    ]),
  ]);
  details.open = Boolean(state.query) || state.openGroups.has(node.path) || radarCount > 0 || reviewCount > 0;
  details.addEventListener('toggle', () => {
    if (state.query) return;
    if (details.open) state.openGroups.add(node.path);
    else state.openGroups.delete(node.path);
  });
  return details;
}

function restoreFocus(focus) {
  const target = document.querySelector(`[data-focus-id="${CSS.escape(focus)}"]`);
  const fallback = document.querySelector(`[data-focus-id="${CSS.escape(focus.replace(/:[a-z]+$/, ':review'))}"]`);
  (target && !target.disabled ? target : fallback)?.focus({ preventScroll: true });
}

function focusedId() {
  return document.activeElement?.getAttribute?.('data-focus-id') ?? null;
}

function render() {
  const focus = focusedId();
  const tree = buildTree(state.projects);
  const groups = [...tree.groups.values()].map((group) => renderGroup(group, 0));
  const loose = tree.projects.filter((project) => matchesQuery(project, state.query)).map(renderProject);
  const content = [...groups, ...loose].filter(Boolean);
  const notice = state.loadError
    ? el('p', { className: 'muted' }, [
        el('span', { text: `Список проектов из GitLab не загрузился (${state.loadError}). Показаны только уже выбранные. ` }),
        retryButton(),
      ])
    : null;
  const empty = el('p', { className: 'muted', text: state.loaded ? 'Ничего не найдено' : 'Загружаю проекты из GitLab…' });
  $('tree').replaceChildren(...[notice, ...(content.length ? content : [empty])].filter(Boolean));
  if (focus) restoreFocus(focus);
  const { review, radarIds } = settingsPayloadProjects(state.projects, DEFAULT_AGENT);
  $('tree-summary').textContent = `авторевью: ${review.length} · радар: ${radarSelectedMode() ? radarIds.length : 'все Maintainer+'}`;
}

function retryButton() {
  const button = el('button', { text: 'Повторить', attrs: { type: 'button' } });
  button.addEventListener('click', () => {
    state.loadError = null;
    state.loaded = false;
    render();
    loadProjects();
  });
  return button;
}

async function loadProjects() {
  try {
    const response = await fetch('/api/settings/projects', { cache: 'no-store' });
    if (!response.ok) throw new Error(await response.text());
    const { review, radarIds } = settingsPayloadProjects(state.projects, DEFAULT_AGENT);
    state.projects = mergeProjects(await response.json(), review, radarIds);
  } catch (error) {
    state.loadError = error.message || 'ошибка сети';
  }
  state.loaded = true;
  render();
}

function readInt(id, min, max) {
  const input = $(id);
  const err = input.parentElement.querySelector('.err');
  const raw = input.value.trim();
  const value = Number(raw);
  const ok = raw !== '' && Number.isInteger(value) && value >= min && (max === undefined || value <= max);
  input.classList.toggle('invalid', !ok);
  if (err) err.textContent = ok ? '' : max === undefined ? `целое число от ${min}` : `целое число от ${min} до ${max}`;
  return ok ? value : null;
}

function parseCheckNames(text) {
  return [...new Set(text.split(/[\n,]/).map((name) => name.trim()).filter(Boolean))];
}

async function save() {
  const button = $('save');
  if (button.disabled) return;
  const values = {
    max_parallel: readInt('max_parallel', 1),
    timeout_sec: readInt('timeout_sec', 1),
    clone_depth: readInt('clone_depth', 1),
    workspace_ttl_days: readInt('workspace_ttl_days', 1),
    push_debounce_sec: readInt('push_debounce_sec', 0),
    approval_threshold: readInt('radar-threshold', 0, 20),
  };
  if (Object.values(values).some((value) => value === null)) {
    setStatus('Исправь выделенные поля', 'err');
    return;
  }
  const { review, radarIds } = settingsPayloadProjects(state.projects, DEFAULT_AGENT);
  if (!review.length) {
    setStatus('Нужен хотя бы один проект с авторевью', 'err');
    return;
  }
  if (radarSelectedMode() && !radarIds.length) {
    setStatus('Отметь хотя бы один проект для радара или выбери «все проекты»', 'err');
    return;
  }
  const payload = {
    review: {
      max_parallel: values.max_parallel,
      timeout_sec: values.timeout_sec,
      clone_depth: values.clone_depth,
      workspace_ttl_days: values.workspace_ttl_days,
      push_debounce_sec: values.push_debounce_sec,
    },
    projects: review,
    radar: {
      enabled: $('radar-enabled').checked,
      project_filter_mode: radarSelectedMode() ? 'selected' : 'all',
      selected_project_ids: radarIds,
      ignored_check_names: parseCheckNames($('radar-ignored').value),
      approval_threshold: values.approval_threshold,
      hidden_sections: [...document.querySelectorAll('.radar-hidden:checked')].map((input) => input.value),
    },
  };
  button.disabled = true;
  setStatus('Сохраняю…');
  try {
    const response = await fetch('/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'lpdev' },
      body: JSON.stringify(payload),
    });
    if (response.ok) {
      setStatus('Сохранено, обновляю страницу…', 'ok');
      location.href = response.redirected ? response.url : '/settings?saved=1';
      return;
    }
    setStatus(`Ошибка ${response.status}: ${await response.text()}`, 'err');
  } catch (error) {
    setStatus(`Ошибка сети: ${error}`, 'err');
  }
  button.disabled = false;
}

function renderHiddenSections() {
  const hidden = new Set(initial.radar.hidden_sections);
  $('radar-hidden').replaceChildren(
    ...SECTION_META.map(({ id, title }) => {
      const input = el('input', { className: 'radar-hidden', attrs: { type: 'checkbox', value: id } });
      input.checked = hidden.has(id);
      return el('label', { className: 'check' }, [input, el('span', { text: ` ${title}` })]);
    }),
  );
}

function init() {
  $('radar-mode-selected').checked = initial.radar.project_filter_mode === 'selected';
  $('radar-mode-all').checked = initial.radar.project_filter_mode !== 'selected';
  document.querySelectorAll('input[name="radar-mode"]').forEach((input) => input.addEventListener('change', render));
  $('tree-search').addEventListener('input', (event) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.query = event.target.value;
      render();
    }, 150);
  });
  $('add-project').addEventListener('submit', (event) => {
    event.preventDefault();
    const input = $('add-project-input');
    const path = projectPathFromInput(input.value, initial.gitlabUrl);
    if (!path) {
      setStatus('Укажи ссылку на проект или путь вида group/project', 'err');
      return;
    }
    const already = state.projects.some((project) => project.path.toLowerCase() === path.toLowerCase() && project.review);
    state.projects = addProjectByPath(state.projects, path, DEFAULT_AGENT);
    input.value = '';
    setStatus(already ? `${path}: авторевью уже включено` : `${path}: авторевью включится после сохранения`, 'muted');
    render();
  });
  $('save').addEventListener('click', save);
  document.addEventListener('input', (event) => {
    if (event.target.classList?.contains('invalid')) event.target.classList.remove('invalid');
  });
  renderHiddenSections();
  render();
  loadProjects();
}

init();
