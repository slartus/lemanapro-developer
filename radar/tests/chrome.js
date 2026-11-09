const listeners = () => {
  const callbacks = [];
  return {
    callbacks,
    addListener: (callback) => callbacks.push(callback),
    emit: (...args) => callbacks.map((callback) => callback(...args)),
  };
};

function storageArea(name, onChanged) {
  const data = new Map();
  const keysOf = (keys) => (keys === undefined || keys === null ? [...data.keys()] : [keys].flat());
  const clone = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
  return {
    data,
    async get(keys) {
      return Object.fromEntries(keysOf(keys).filter((key) => data.has(key)).map((key) => [key, clone(data.get(key))]));
    },
    async set(items) {
      const changed = Object.entries(items).filter(
        ([key, value]) => JSON.stringify(data.get(key)) !== JSON.stringify(value),
      );
      const changes = Object.fromEntries(
        changed.map(([key, value]) => [key, { oldValue: clone(data.get(key)), newValue: clone(value) }]),
      );
      changed.forEach(([key, value]) => data.set(key, clone(value)));
      if (changed.length) {
        onChanged.emit(changes, name);
      }
    },
    async remove(keys) {
      const present = keysOf(keys).filter((key) => data.has(key));
      const changes = Object.fromEntries(present.map((key) => [key, { oldValue: clone(data.get(key)) }]));
      present.forEach((key) => data.delete(key));
      if (present.length) {
        onChanged.emit(changes, name);
      }
    },
  };
}

export function createChrome({ origins = [], permissions = [] } = {}) {
  const onChanged = listeners();
  const granted = { origins: new Set(origins), permissions: new Set(permissions) };
  const alarms = new Map();
  const notifications = [];
  const badge = {};
  return {
    alarms: {
      all: alarms,
      create: async (name, info) => alarms.set(name, info),
      clear: async (name) => alarms.delete(name),
      onAlarm: listeners(),
    },
    action: {
      badge,
      setBadgeBackgroundColor: async ({ color }) => Object.assign(badge, { color }),
      setBadgeText: async ({ text }) => Object.assign(badge, { text }),
      setTitle: async ({ title }) => Object.assign(badge, { title }),
      openPopup: async () => {},
    },
    notifications: {
      created: notifications,
      create: async (id, options) => notifications.push({ id, options }),
      clear: async () => true,
      onClicked: listeners(),
      onClosed: listeners(),
    },
    permissions: {
      contains: async ({ origins: wanted = [], permissions: wantedPermissions = [] }) =>
        wanted.every((origin) => granted.origins.has(origin)) &&
        wantedPermissions.every((permission) => granted.permissions.has(permission)),
      onAdded: listeners(),
    },
    runtime: {
      getURL: (path) => `chrome-extension://radar/${path}`,
      getManifest: () => ({ version: '1.0.0' }),
      onInstalled: listeners(),
      onStartup: listeners(),
      onMessage: listeners(),
    },
    storage: {
      onChanged,
      local: storageArea('local', onChanged),
      session: storageArea('session', onChanged),
    },
    tabs: { create: async () => {} },
  };
}

let instance = 0;

export async function loadBackground(chrome) {
  globalThis.chrome = chrome;
  await import(`../src/background.js?instance=${instance++}`);
}

export function sendMessage(chrome, message) {
  return new Promise((resolve) => {
    chrome.runtime.onMessage.emit(message, {}, resolve);
  });
}
