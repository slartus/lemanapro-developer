import { fileURLToPath } from 'node:url';

import { collectDashboard } from './dashboard.js';
import { GitLabClient } from './gitlab.js';
import { hasActivePipelines, sectionsToSkip, withStableStatuses } from './view.js';

export function describeError(error) {
  if (error.status === 401) {
    return 'Токен невалиден или истёк — обнови PAT в Keychain';
  }
  if (error.status === 403 && error.path === '/user') {
    return 'Недостаточно прав у токена — нужен scope read_api';
  }
  if (error.status === 429) {
    return 'GitLab ограничил частоту запросов — увеличь интервал обновления';
  }
  if (error.status >= 500) {
    return 'GitLab временно недоступен — показаны данные последнего успешного обновления';
  }
  if (error instanceof TypeError || error.name === 'TimeoutError') {
    return 'GitLab недоступен — проверь сеть или VPN';
  }
  return error.message;
}

const sortedNumbers = (values) => [...values].sort((a, b) => a - b);

export async function refreshDashboard(
  { baseUrl, apiBase, token, settings = {}, previous = null, threadCache = null },
  { createClient = (url, secret, options) => new GitLabClient(url, secret, options), now = () => Date.now() } = {},
) {
  const {
    projectFilterMode = 'all',
    selectedProjectIds = [],
    ignoredCheckNames = [],
    approvalThreshold = 0,
    hiddenSections = [],
  } = settings;
  const isSelectedMode = projectFilterMode === 'selected';
  const client = createClient(baseUrl, token, { apiBase });
  const startedAt = now();
  try {
    const { threadCache: nextThreadCache, ...collected } = await collectDashboard(client, {
      selectedProjectIds: isSelectedMode ? selectedProjectIds : null,
      ignoredCheckNames,
      approvalThreshold,
      skipSections: sectionsToSkip({ hiddenSections }),
      threadCache: threadCache?.baseUrl === baseUrl ? threadCache : null,
    });
    const dashboard = withStableStatuses(previous, {
      ...collected,
      stats: { requests: client.requestCount, durationMs: now() - startedAt },
      baseUrl,
      filters: {
        selectedProjectIds: isSelectedMode ? sortedNumbers(selectedProjectIds) : null,
        ignoredCheckNames: [...ignoredCheckNames].sort(),
        approvalThreshold,
      },
    });
    if (isSelectedMode && !dashboard.projectsSkipped && dashboard.watchedProjects === 0 && !dashboard.projectsUnavailable) {
      dashboard.warning = 'Ни один выбранный проект больше не доступен — обнови выбор в настройках';
    }
    return {
      ok: true,
      dashboard,
      threadCache: { ...nextThreadCache, baseUrl },
      activePipelines: hasActivePipelines(dashboard),
    };
  } catch (error) {
    return { ok: false, status: error.status ?? null, error: describeError(error) };
  }
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString('utf8');
}

async function main() {
  const input = JSON.parse(await readStdin());
  const result = await refreshDashboard(input);
  process.stdout.write(JSON.stringify(result));
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`${error?.stack ?? error}\n`);
    process.exitCode = 1;
  });
}
