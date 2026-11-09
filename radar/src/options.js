import {
  DEFAULT_BASE_URL,
  DEFAULT_INTERVAL_MINUTES,
  MAX_APPROVAL_THRESHOLD,
  MAX_INTERVAL_MINUTES,
  MIN_INTERVAL_MINUTES,
  NOTIFICATIONS_PERMISSION,
  PROJECT_FILTER_MODES,
  PROJECT_LABEL_MODES,
  STORAGE_KEYS,
  UPDATE_CHECK_ORIGIN,
  hasHostPermission,
  hasNotificationsPermission,
  hostPermissionPattern,
  normalizeBaseUrl,
  parseCheckNames,
  requestHostPermission,
} from './config.js';
import { GitLabClient, fetchMaintainedProjects } from './gitlab.js';
import { DEFAULT_QUIET_HOURS, NOTIFICATION_TYPES } from './notifications.js';
import { SECTION_META } from './view.js';

const form = document.getElementById('form');
const baseUrlInput = document.getElementById('base-url');
const tokenInput = document.getElementById('token');
const intervalInput = document.getElementById('interval');
const ignoredChecksInput = document.getElementById('ignored-checks');
const checkUpdatesInput = document.getElementById('check-updates');
const fastRefreshInput = document.getElementById('fast-refresh');
const notificationsInput = document.getElementById('notifications');
const notificationDetailsInput = document.getElementById('notification-details');
const notificationTypesList = document.getElementById('notification-types');
const sectionVisibilityList = document.getElementById('section-visibility');
const approvalThresholdInput = document.getElementById('approval-threshold');
const quietEnabledInput = document.getElementById('quiet-enabled');
const quietFromInput = document.getElementById('quiet-from');
const quietToInput = document.getElementById('quiet-to');
const quietWeekendsInput = document.getElementById('quiet-weekends');
const status = document.getElementById('status');
const tokenLink = document.getElementById('token-link');
const modeInputs = [...document.querySelectorAll('input[name="project-mode"]')];
const labelModeInputs = [...document.querySelectorAll('input[name="project-label-mode"]')];
const projectPicker = document.getElementById('project-picker');
const projectSearch = document.getElementById('project-search');
const projectList = document.getElementById('projects');
const projectCounter = document.getElementById('project-counter');

intervalInput.min = String(MIN_INTERVAL_MINUTES);
intervalInput.max = String(MAX_INTERVAL_MINUTES);
approvalThresholdInput.max = String(MAX_APPROVAL_THRESHOLD);

const stored = (await chrome.storage.local.get(STORAGE_KEYS.settings))[STORAGE_KEYS.settings] ?? {};
let savedBaseUrl = stored.baseUrl ?? DEFAULT_BASE_URL;
baseUrlInput.value = savedBaseUrl;
tokenInput.value = stored.token ?? '';
intervalInput.value = String(stored.intervalMinutes ?? DEFAULT_INTERVAL_MINUTES);
fastRefreshInput.checked = stored.fastRefreshWhileCi ?? true;
ignoredChecksInput.value = (stored.ignoredCheckNames ?? []).join('\n');
checkUpdatesInput.checked =
  Boolean(stored.checkUpdates) && (await chrome.permissions.contains({ origins: [UPDATE_CHECK_ORIGIN] }));
notificationsInput.checked = Boolean(stored.notifications) && (await hasNotificationsPermission());
notificationDetailsInput.checked = Boolean(stored.notificationDetails);
approvalThresholdInput.value = String(stored.approvalThreshold ?? 0);
const quietHours = { ...DEFAULT_QUIET_HOURS, ...stored.quietHours };
quietEnabledInput.checked = quietHours.enabled;
quietFromInput.value = quietHours.from;
quietToInput.value = quietHours.to;
quietWeekendsInput.checked = quietHours.weekends;

const mutedNotificationTypes = new Set(stored.mutedNotificationTypes ?? []);
const notificationTypeInputs = NOTIFICATION_TYPES.map(({ type, title }) => {
  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.checked = !mutedNotificationTypes.has(type);
  checkbox.dataset.type = type;
  const label = document.createElement('label');
  label.className = 'checkbox';
  label.append(checkbox, title);
  notificationTypesList.append(label);
  return checkbox;
});

const hiddenSections = new Set(stored.hiddenSections ?? []);
const sectionVisibilityInputs = SECTION_META.map(({ id, title }) => {
  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.checked = !hiddenSections.has(id);
  checkbox.dataset.sectionId = id;
  const label = document.createElement('label');
  label.className = 'checkbox';
  label.append(checkbox, title);
  sectionVisibilityList.append(label);
  return checkbox;
});

const syncNotificationTypes = () =>
  [...notificationTypeInputs, quietEnabledInput, quietFromInput, quietToInput, quietWeekendsInput].forEach((input) => {
    input.disabled = !notificationsInput.checked;
  });
syncNotificationTypes();

const selectedProjectIds = new Set(stored.selectedProjectIds ?? []);
let selectionBaseUrl = savedBaseUrl;
let projects = [];
let loadedForKey = null;
let loadSequence = 0;
let prunedCount = 0;

const selectedMode = () => modeInputs.find((input) => input.checked)?.value ?? PROJECT_FILTER_MODES.all;
const visibleProjectCheckboxes = (container) =>
  [...container.querySelectorAll('.project-row:not([hidden]) input')];

function readBaseUrl() {
  try {
    return normalizeBaseUrl(baseUrlInput.value);
  } catch {
    return null;
  }
}

function updateTokenLink() {
  const baseUrl = readBaseUrl();
  tokenLink.hidden = !baseUrl;
  if (baseUrl) {
    tokenLink.href = `${baseUrl}/-/user_settings/personal_access_tokens?name=MR+Radar&scopes=read_api`;
    tokenLink.textContent = `${baseUrl}/-/user_settings/personal_access_tokens`;
  }
}

function updateCounter() {
  const pruned = prunedCount ? ` · убрано недоступных: ${prunedCount}` : '';
  projectCounter.textContent = `выбрано ${selectedProjectIds.size} из ${projects.length}${pruned}`;
}

function refreshGroupCheckboxes() {
  projectList.querySelectorAll('.group').forEach((group) => {
    const checkboxes = visibleProjectCheckboxes(group.querySelector('.children'));
    const checkedCount = checkboxes.filter((checkbox) => checkbox.checked).length;
    const groupCheckbox = group.querySelector('.group-row input');
    groupCheckbox.checked = checkboxes.length > 0 && checkedCount === checkboxes.length;
    groupCheckbox.indeterminate = checkedCount > 0 && checkedCount < checkboxes.length;
  });
}

function setProjectsChecked(checkboxes, checked) {
  checkboxes.forEach((checkbox) => {
    checkbox.checked = checked;
    const id = Number(checkbox.dataset.projectId);
    if (checked) {
      selectedProjectIds.add(id);
    } else {
      selectedProjectIds.delete(id);
    }
  });
  refreshGroupCheckboxes();
  updateCounter();
}

function applySearch() {
  const query = projectSearch.value.trim().toLowerCase();
  projectList.classList.toggle('searching', Boolean(query));
  projectList.querySelectorAll('.project-row').forEach((row) => {
    row.hidden = Boolean(query) && !row.dataset.path.includes(query);
  });
  projectList.querySelectorAll('.group').forEach((group) => {
    group.hidden = !group.querySelector('.project-row:not([hidden])');
  });
  refreshGroupCheckboxes();
}

function buildTree() {
  const root = { groups: new Map(), projects: [] };
  projects.forEach((project) => {
    const segments = project.path_with_namespace.split('/');
    let node = root;
    segments.slice(0, -1).forEach((segment) => {
      if (!node.groups.has(segment)) {
        node.groups.set(segment, { groups: new Map(), projects: [] });
      }
      node = node.groups.get(segment);
    });
    node.projects.push(project);
  });
  return root;
}

function renderProjectRow(project) {
  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.checked = selectedProjectIds.has(project.id);
  checkbox.dataset.projectId = String(project.id);
  checkbox.addEventListener('change', () => setProjectsChecked([checkbox], checkbox.checked));
  const row = document.createElement('label');
  row.className = 'project-row';
  row.dataset.path = project.path_with_namespace.toLowerCase();
  row.append(checkbox, project.path_with_namespace.split('/').at(-1));
  return row;
}

function renderGroup(name, node, depth) {
  const children = document.createElement('div');
  children.className = 'children';
  children.append(...renderNodeChildren(node, depth + 1));
  children.hidden = depth > 0;

  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'toggle';
  const syncToggle = () => {
    toggle.textContent = children.hidden ? '▸' : '▾';
  };
  toggle.addEventListener('click', () => {
    children.hidden = !children.hidden;
    syncToggle();
  });
  syncToggle();

  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.addEventListener('change', () => setProjectsChecked(visibleProjectCheckboxes(children), checkbox.checked));

  const title = document.createElement('label');
  title.append(checkbox, `${name}/`);
  const row = document.createElement('div');
  row.className = 'group-row';
  row.append(toggle, title);

  const group = document.createElement('div');
  group.className = 'group';
  group.append(row, children);
  return group;
}

function renderNodeChildren(node, depth) {
  const byName = (a, b) => a[0].localeCompare(b[0]);
  return [
    ...[...node.groups.entries()].sort(byName).map(([name, child]) => renderGroup(name, child, depth)),
    ...node.projects.map(renderProjectRow),
  ];
}

function renderProjects() {
  projectList.replaceChildren(...renderNodeChildren(buildTree(), 0));
  applySearch();
  updateCounter();
}

function resetProjects() {
  loadSequence++;
  projects = [];
  loadedForKey = null;
  prunedCount = 0;
  projectList.replaceChildren();
}

async function loadProjects({ permissionGranted = false } = {}) {
  const baseUrl = readBaseUrl();
  const token = tokenInput.value.trim();
  if (!baseUrl) {
    projectCounter.textContent = 'Укажите корректный https URL GitLab';
    return;
  }
  if (!token) {
    projectCounter.textContent = 'Сначала укажите токен';
    return;
  }
  const key = `${baseUrl}\n${token}`;
  if (loadedForKey === key) {
    return;
  }
  const sequence = ++loadSequence;
  const isStale = () => sequence !== loadSequence || readBaseUrl() !== baseUrl || tokenInput.value.trim() !== token;
  if (!permissionGranted && !(await hasHostPermission(baseUrl))) {
    if (!isStale()) {
      projectCounter.textContent = 'Нажмите «Загрузить проекты» и разрешите доступ к хосту';
    }
    return;
  }
  projectCounter.textContent = 'Загружаю проекты…';
  try {
    const loaded = await fetchMaintainedProjects(new GitLabClient(baseUrl, token));
    if (isStale()) {
      return;
    }
    projects = loaded;
    loadedForKey = key;
    const accessibleIds = new Set(projects.map((project) => project.id));
    const inaccessibleIds = [...selectedProjectIds].filter((id) => !accessibleIds.has(id));
    inaccessibleIds.forEach((id) => selectedProjectIds.delete(id));
    prunedCount = inaccessibleIds.length;
    renderProjects();
  } catch (error) {
    if (isStale()) {
      return;
    }
    resetProjects();
    projectCounter.textContent = `Не удалось загрузить проекты: ${error.message}`;
  }
}

function syncPickerVisibility() {
  const isSelectedMode = selectedMode() === PROJECT_FILTER_MODES.selected;
  projectPicker.hidden = !isSelectedMode;
  if (isSelectedMode) {
    loadProjects();
  }
}

function onBaseUrlChange() {
  updateTokenLink();
  const baseUrl = readBaseUrl();
  if (baseUrl && baseUrl !== selectionBaseUrl) {
    if (hostPermissionPattern(baseUrl) !== hostPermissionPattern(selectionBaseUrl) && tokenInput.value) {
      tokenInput.value = '';
      status.textContent = 'Токен привязан к хосту — введите токен для нового GitLab';
    }
    selectionBaseUrl = baseUrl;
    selectedProjectIds.clear();
    resetProjects();
    updateCounter();
  }
  syncPickerVisibility();
}

const initialMode = Object.values(PROJECT_FILTER_MODES).includes(stored.projectFilterMode)
  ? stored.projectFilterMode
  : PROJECT_FILTER_MODES.all;
modeInputs.forEach((input) => {
  input.checked = input.value === initialMode;
  input.addEventListener('change', syncPickerVisibility);
});
const initialLabelMode = Object.values(PROJECT_LABEL_MODES).includes(stored.projectLabelMode)
  ? stored.projectLabelMode
  : PROJECT_LABEL_MODES.name;
labelModeInputs.forEach((input) => {
  input.checked = input.value === initialLabelMode;
});
baseUrlInput.addEventListener('change', onBaseUrlChange);
tokenInput.addEventListener('change', syncPickerVisibility);
projectSearch.addEventListener('input', applySearch);
document.getElementById('select-visible').addEventListener('click', () =>
  setProjectsChecked(visibleProjectCheckboxes(projectList), true),
);
document.getElementById('clear-visible').addEventListener('click', () =>
  setProjectsChecked(visibleProjectCheckboxes(projectList), false),
);
document.getElementById('load-projects').addEventListener('click', async () => {
  const baseUrl = readBaseUrl();
  if (!baseUrl) {
    projectCounter.textContent = 'Укажите корректный https URL GitLab';
    return;
  }
  if (!(await requestHostPermission(baseUrl))) {
    projectCounter.textContent = 'Доступ к хосту не выдан';
    return;
  }
  loadedForKey = null;
  await loadProjects({ permissionGranted: true });
});
checkUpdatesInput.addEventListener('change', async () => {
  if (!checkUpdatesInput.checked) {
    await chrome.permissions.remove({ origins: [UPDATE_CHECK_ORIGIN] }).catch(() => false);
    return;
  }
  const granted = await chrome.permissions.request({ origins: [UPDATE_CHECK_ORIGIN] });
  if (!granted) {
    checkUpdatesInput.checked = false;
    status.textContent = 'Без доступа к api.github.com проверка обновлений не работает';
  }
});
notificationsInput.addEventListener('change', async () => {
  syncNotificationTypes();
  if (!notificationsInput.checked) {
    await chrome.permissions.remove({ permissions: [NOTIFICATIONS_PERMISSION] }).catch(() => false);
    return;
  }
  const granted = await chrome.permissions.request({ permissions: [NOTIFICATIONS_PERMISSION] });
  if (!granted) {
    notificationsInput.checked = false;
    syncNotificationTypes();
    status.textContent = 'Без разрешения браузера уведомления не работают';
  }
});
updateTokenLink();
syncPickerVisibility();

async function revokeHostPermission(baseUrl) {
  try {
    await chrome.permissions.remove({ origins: [hostPermissionPattern(baseUrl)] });
  } catch (error) {
    console.warn('Failed to revoke host permission', error);
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const baseUrl = readBaseUrl();
  if (!baseUrl) {
    status.textContent = 'Некорректный URL GitLab — нужен https';
    return;
  }
  const projectFilterMode = selectedMode();
  if (projectFilterMode === PROJECT_FILTER_MODES.selected && selectedProjectIds.size === 0) {
    status.textContent = 'Выберите хотя бы один проект';
    return;
  }
  if (!(await requestHostPermission(baseUrl))) {
    status.textContent = 'Без доступа к хосту расширение не сможет запрашивать GitLab';
    return;
  }
  if (hostPermissionPattern(savedBaseUrl) !== hostPermissionPattern(baseUrl)) {
    await revokeHostPermission(savedBaseUrl);
  }
  if (savedBaseUrl !== baseUrl) {
    await chrome.storage.local.remove([
      STORAGE_KEYS.dashboard,
      STORAGE_KEYS.userState,
      STORAGE_KEYS.threadCache,
      STORAGE_KEYS.pendingNotifications,
    ]);
  }
  await chrome.storage.local.set({
    [STORAGE_KEYS.settings]: {
      baseUrl,
      token: tokenInput.value.trim(),
      intervalMinutes: Math.min(
        MAX_INTERVAL_MINUTES,
        Math.max(MIN_INTERVAL_MINUTES, Number(intervalInput.value) || DEFAULT_INTERVAL_MINUTES),
      ),
      fastRefreshWhileCi: fastRefreshInput.checked,
      projectFilterMode,
      projectLabelMode: labelModeInputs.find((input) => input.checked)?.value ?? PROJECT_LABEL_MODES.name,
      selectedProjectIds: [...selectedProjectIds],
      ignoredCheckNames: parseCheckNames(ignoredChecksInput.value),
      checkUpdates: checkUpdatesInput.checked,
      notifications: notificationsInput.checked,
      notificationDetails: notificationDetailsInput.checked,
      mutedNotificationTypes: notificationTypeInputs
        .filter((checkbox) => !checkbox.checked)
        .map((checkbox) => checkbox.dataset.type),
      hiddenSections: sectionVisibilityInputs
        .filter((checkbox) => !checkbox.checked)
        .map((checkbox) => checkbox.dataset.sectionId),
      quietHours: {
        enabled: quietEnabledInput.checked,
        from: quietFromInput.value || DEFAULT_QUIET_HOURS.from,
        to: quietToInput.value || DEFAULT_QUIET_HOURS.to,
        weekends: quietWeekendsInput.checked,
      },
      approvalThreshold: Math.min(
        MAX_APPROVAL_THRESHOLD,
        Math.max(0, Math.trunc(Number(approvalThresholdInput.value) || 0)),
      ),
    },
  });
  savedBaseUrl = baseUrl;
  baseUrlInput.value = baseUrl;
  status.textContent = 'Сохранено, обновляю…';
  try {
    await chrome.runtime.sendMessage({ type: 'refresh', settingsChanged: true });
  } finally {
    status.textContent = 'Сохранено';
  }
});
