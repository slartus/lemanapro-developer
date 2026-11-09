import {
  DEFAULT_BASE_URL,
  DEFAULT_INTERVAL_MINUTES,
  PROJECT_FILTER_MODES,
  STORAGE_KEYS,
  UPDATE_CHECK_INTERVAL_MINUTES,
  UPDATE_CHECK_ORIGIN,
  UPDATE_REPOSITORY,
  compareVersions,
  hasHostPermission,
  hasNotificationsPermission,
  notificationTargetKey,
  refreshDelayMinutes,
  refreshPeriodMinutes,
} from './config.js';
import { collectDashboard } from './dashboard.js';
import { GitLabClient } from './gitlab.js';
import { isUnderBaseUrl } from './links.js';
import {
  DEFAULT_QUIET_HOURS,
  enabledEvents,
  formatNotification,
  isQuietTime,
  mergeEvents,
  notificationTarget,
} from './notifications.js';
import { nextSnoozeExpiry } from './snooze.js';
import {
  collectNotificationEvents,
  deriveView,
  hasActivePipelines,
  sectionsToSkip,
  sortPendingEvents,
  withStableStatuses,
} from './view.js';

const ALARM_NAME = 'refresh';
const UPDATE_ALARM_NAME = 'update-check';
const SNOOZE_ALARM_NAME = 'snooze-expiry';
const BADGE_COLOR_OK = '#1f75cb';
const BADGE_COLOR_ERROR = '#dd2b0e';
const BADGE_TITLE = 'GitLab MR Radar';
const FAST_REFRESH_SINCE_KEY = 'fastRefreshSince';

let inFlightRefresh = null;
let queuedRefresh = null;
let notificationListenerRegistered = false;

async function loadSettings() {
  const stored = await chrome.storage.local.get(STORAGE_KEYS.settings);
  return {
    baseUrl: DEFAULT_BASE_URL,
    token: '',
    intervalMinutes: DEFAULT_INTERVAL_MINUTES,
    projectFilterMode: PROJECT_FILTER_MODES.all,
    selectedProjectIds: [],
    ignoredCheckNames: [],
    approvalThreshold: 0,
    mutedNotificationTypes: [],
    hiddenSections: [],
    fastRefreshWhileCi: true,
    quietHours: DEFAULT_QUIET_HOURS,
    checkUpdates: false,
    notifications: false,
    notificationDetails: false,
    ...stored[STORAGE_KEYS.settings],
  };
}

async function loadUserState() {
  return (await chrome.storage.local.get(STORAGE_KEYS.userState))[STORAGE_KEYS.userState] ?? {};
}

async function setBadge(text, color, title = BADGE_TITLE) {
  await chrome.action.setBadgeBackgroundColor({ color });
  await chrome.action.setBadgeText({ text });
  await chrome.action.setTitle({ title });
}

async function showCounts({ waiting, failedCi }) {
  const text = [waiting, failedCi]
    .filter((count) => count > 0)
    .join('·');
  const title = `${BADGE_TITLE}\nЖдут действия: ${waiting}\nУпал CI: ${failedCi}`;
  await setBadge(text, failedCi > 0 ? BADGE_COLOR_ERROR : BADGE_COLOR_OK, title);
}

function describeError(error) {
  if (error.status === 401) {
    return 'Токен невалиден или истёк — обновите его в настройках';
  }
  if (error.status === 403 && error.path === '/user') {
    return 'Недостаточно прав у токена — нужен scope read_api';
  }
  if (error.status === 429) {
    return 'GitLab ограничил частоту запросов — увеличьте интервал обновления';
  }
  if (error.status >= 500) {
    return 'GitLab временно недоступен — показаны данные последнего успешного обновления';
  }
  if (error instanceof TypeError || error.name === 'TimeoutError') {
    return 'GitLab недоступен — проверьте сеть или VPN';
  }
  return error.message;
}

async function notifyChanges(previous, dashboard, userState, settings) {
  if (!settings.notifications || !(await hasNotificationsPermission())) {
    await chrome.storage.local.remove(STORAGE_KEYS.pendingNotifications);
    return;
  }
  const stored = (await chrome.storage.local.get(STORAGE_KEYS.pendingNotifications))[STORAGE_KEYS.pendingNotifications];
  const { relevant, waiting } =
    stored?.baseUrl === dashboard.baseUrl && stored.username === dashboard.username
      ? sortPendingEvents(stored.events, dashboard, userState)
      : { relevant: [], waiting: [] };
  const events = enabledEvents(
    mergeEvents(relevant, collectNotificationEvents(previous, dashboard, userState)),
    settings.mutedNotificationTypes,
  );
  const quiet = isQuietTime(settings.quietHours);
  const deferred = enabledEvents(quiet ? mergeEvents(waiting, events) : waiting, settings.mutedNotificationTypes);
  if (deferred.length) {
    await chrome.storage.local.set({
      [STORAGE_KEYS.pendingNotifications]: {
        baseUrl: dashboard.baseUrl,
        username: dashboard.username,
        events: deferred,
      },
    });
  } else if (stored) {
    await chrome.storage.local.remove(STORAGE_KEYS.pendingNotifications);
  }
  if (quiet || events.length === 0) {
    return;
  }
  const notificationId = `changes-${Date.now()}`;
  await chrome.storage.session.set({
    [notificationTargetKey(notificationId)]: {
      baseUrl: dashboard.baseUrl,
      url: notificationTarget(events, dashboard.baseUrl),
    },
  });
  await chrome.notifications.create(
    notificationId,
    formatNotification(events, {
      showDetails: settings.notificationDetails,
      iconUrl: chrome.runtime.getURL('icons/128.png'),
    }),
  );
}

async function takeNotificationTarget(notificationId) {
  const key = notificationTargetKey(notificationId);
  const { [key]: target } = await chrome.storage.session.get(key);
  await chrome.storage.session.remove(key);
  return target;
}

async function openNotificationTarget(notificationId) {
  const target = await takeNotificationTarget(notificationId);
  await chrome.notifications.clear(notificationId);
  const { baseUrl } = await loadSettings();
  if (!target || target.baseUrl !== baseUrl) {
    return;
  }
  if (isUnderBaseUrl(target.url, baseUrl)) {
    await chrome.tabs.create({ url: target.url });
    return;
  }
  try {
    await chrome.action.openPopup();
  } catch {
    await chrome.tabs.create({ url: baseUrl });
  }
}

function registerNotificationListeners() {
  if (notificationListenerRegistered || !chrome.notifications) {
    return;
  }
  notificationListenerRegistered = true;
  chrome.notifications.onClicked.addListener((notificationId) => {
    openNotificationTarget(notificationId).catch(console.error);
  });
  chrome.notifications.onClosed.addListener((notificationId, byUser) => {
    if (byUser) {
      takeNotificationTarget(notificationId).catch(console.error);
    }
  });
}

async function scheduleSnoozeExpiry(userState) {
  const when = nextSnoozeExpiry(userState.snoozed);
  if (when) {
    await chrome.alarms.create(SNOOZE_ALARM_NAME, { when });
  } else {
    await chrome.alarms.clear(SNOOZE_ALARM_NAME);
  }
}

async function showCountsFromStorage() {
  const stored = await chrome.storage.local.get([
    STORAGE_KEYS.dashboard,
    STORAGE_KEYS.userState,
    STORAGE_KEYS.settings,
  ]);
  const dashboard = stored[STORAGE_KEYS.dashboard];
  const userState = stored[STORAGE_KEYS.userState] ?? {};
  const settings = stored[STORAGE_KEYS.settings] ?? {};
  await scheduleSnoozeExpiry(userState);
  if (!dashboard?.sections || dashboard.error) {
    return;
  }
  await showCounts(deriveView(dashboard, userState, Date.now(), settings.hiddenSections ?? []).counts);
}

async function performRefresh() {
  const settings = await loadSettings();
  const { baseUrl, token, projectFilterMode, selectedProjectIds, ignoredCheckNames, approvalThreshold } = settings;
  const setupError = !token
    ? 'Укажите токен в настройках'
    : !(await hasHostPermission(baseUrl))
      ? `Нет доступа к ${baseUrl} — сохраните настройки и разрешите доступ`
      : null;
  if (setupError) {
    await chrome.storage.local.set({ [STORAGE_KEYS.dashboard]: { error: setupError } });
    await setBadge('?', BADGE_COLOR_ERROR);
    return;
  }
  const previous = (await chrome.storage.local.get(STORAGE_KEYS.dashboard))[STORAGE_KEYS.dashboard];
  const isStale = async () => {
    const current = await loadSettings();
    return current.baseUrl !== baseUrl || current.token !== token;
  };
  try {
    const isSelectedMode = projectFilterMode === PROJECT_FILTER_MODES.selected;
    const storedCache = (await chrome.storage.local.get(STORAGE_KEYS.threadCache))[STORAGE_KEYS.threadCache];
    const client = new GitLabClient(baseUrl, token);
    const startedAt = Date.now();
    const { threadCache, ...collected } = await collectDashboard(client, {
      selectedProjectIds: isSelectedMode ? selectedProjectIds : null,
      ignoredCheckNames,
      approvalThreshold,
      skipSections: sectionsToSkip(settings),
      threadCache: storedCache?.baseUrl === baseUrl ? storedCache : null,
    });
    if (await isStale()) {
      return;
    }
    const dashboard = withStableStatuses(previous, {
      ...collected,
      stats: { requests: client.requestCount, durationMs: Date.now() - startedAt },
      baseUrl,
      filters: {
        selectedProjectIds: isSelectedMode ? [...selectedProjectIds].sort((a, b) => a - b) : null,
        ignoredCheckNames: [...ignoredCheckNames].sort(),
        approvalThreshold,
      },
    });
    if (isSelectedMode && !dashboard.projectsSkipped && dashboard.watchedProjects === 0 && !dashboard.projectsUnavailable) {
      dashboard.warning = 'Ни один выбранный проект больше не доступен — обновите выбор в настройках';
    }
    await chrome.storage.local.set({
      [STORAGE_KEYS.dashboard]: dashboard,
      [STORAGE_KEYS.threadCache]: { ...threadCache, baseUrl },
    });
    const userState = await loadUserState();
    await scheduleSnoozeExpiry(userState);
    const { hiddenSections } = await loadSettings();
    await showCounts(deriveView(dashboard, userState, Date.now(), hiddenSections ?? []).counts);
    await notifyChanges(previous, dashboard, userState, settings).catch(console.error);
    await scheduleRefresh({ fast: hasActivePipelines(collected) }).catch(console.error);
  } catch (error) {
    if (await isStale()) {
      return;
    }
    await chrome.storage.local.set({
      [STORAGE_KEYS.dashboard]: { ...previous, error: describeError(error), failedAt: new Date().toISOString() },
    });
    await setBadge('!', BADGE_COLOR_ERROR);
  }
}

async function checkForUpdate() {
  const { checkUpdates } = await loadSettings();
  const permitted = checkUpdates && (await chrome.permissions.contains({ origins: [UPDATE_CHECK_ORIGIN] }));
  if (!permitted) {
    await chrome.storage.local.remove(STORAGE_KEYS.update);
    return;
  }
  try {
    const response = await fetch(`https://api.github.com/repos/${UPDATE_REPOSITORY}/releases/latest`, {
      headers: { Accept: 'application/vnd.github+json' },
      credentials: 'omit',
      cache: 'no-store',
      signal: AbortSignal.timeout(20_000),
    });
    if (!response.ok) {
      return;
    }
    const release = await response.json();
    const currentVersion = chrome.runtime.getManifest().version;
    const isNewer = compareVersions(release.tag_name, currentVersion) > 0;
    const stillEnabled =
      (await loadSettings()).checkUpdates && (await chrome.permissions.contains({ origins: [UPDATE_CHECK_ORIGIN] }));
    if (!stillEnabled) {
      return;
    }
    await chrome.storage.local.set({
      [STORAGE_KEYS.update]: isNewer
        ? { version: release.tag_name.replace(/^v/, ''), url: release.html_url, checkedAt: new Date().toISOString() }
        : null,
    });
  } catch (error) {
    console.warn('Update check failed', error);
  }
}

async function scheduleUpdateCheck() {
  const { checkUpdates } = await loadSettings();
  if (!checkUpdates) {
    await chrome.alarms.clear(UPDATE_ALARM_NAME);
    await chrome.storage.local.remove(STORAGE_KEYS.update);
    return;
  }
  await chrome.alarms.create(UPDATE_ALARM_NAME, { delayInMinutes: 0, periodInMinutes: UPDATE_CHECK_INTERVAL_MINUTES });
}

function refresh({ afterCurrent = false } = {}) {
  if (inFlightRefresh && afterCurrent) {
    queuedRefresh ??= inFlightRefresh.then(() => {
      queuedRefresh = null;
      return refresh();
    });
    return queuedRefresh;
  }
  inFlightRefresh ??= performRefresh().catch(console.error).finally(() => {
    inFlightRefresh = null;
  });
  return inFlightRefresh;
}

async function trackFastRefresh(fast) {
  if (!fast) {
    await chrome.storage.session.remove(FAST_REFRESH_SINCE_KEY);
    return null;
  }
  const { [FAST_REFRESH_SINCE_KEY]: since } = await chrome.storage.session.get(FAST_REFRESH_SINCE_KEY);
  if (since) {
    return since;
  }
  const now = Date.now();
  await chrome.storage.session.set({ [FAST_REFRESH_SINCE_KEY]: now });
  return now;
}

async function scheduleRefresh({ immediately = false, fast = null } = {}) {
  const { intervalMinutes, fastRefreshWhileCi } = await loadSettings();
  const fastSince = fast === null ? null : await trackFastRefresh(fast && fastRefreshWhileCi);
  await chrome.alarms.create(ALARM_NAME, {
    delayInMinutes: refreshDelayMinutes({ immediately, fastSince, intervalMinutes }),
    periodInMinutes: refreshPeriodMinutes(intervalMinutes),
  });
}

chrome.runtime.onInstalled.addListener(() => {
  scheduleRefresh({ immediately: true });
  scheduleUpdateCheck();
});
chrome.runtime.onStartup.addListener(() => {
  scheduleRefresh({ immediately: true });
  scheduleUpdateCheck();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) {
    refresh();
  } else if (alarm.name === UPDATE_ALARM_NAME) {
    checkForUpdate();
  } else if (alarm.name === SNOOZE_ALARM_NAME) {
    showCountsFromStorage().catch(console.error);
  }
});

registerNotificationListeners();
chrome.permissions.onAdded.addListener(registerNotificationListeners);

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes[STORAGE_KEYS.userState]) {
    showCountsFromStorage().catch(console.error);
  }
  if (area === 'local' && changes[STORAGE_KEYS.settings]) {
    scheduleRefresh({ immediately: false });
    showCountsFromStorage().catch(console.error);
    const { oldValue, newValue } = changes[STORAGE_KEYS.settings];
    if (Boolean(oldValue?.checkUpdates) !== Boolean(newValue?.checkUpdates)) {
      scheduleUpdateCheck();
    }
  }
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === 'refresh') {
    refresh({ afterCurrent: Boolean(message.settingsChanged) }).then(() => sendResponse({ done: true }));
    return true;
  }
  return false;
});
