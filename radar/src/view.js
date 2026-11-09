import { isSnoozeActive } from './snooze.js';

const DAY_MS = 24 * 60 * 60 * 1000;
const TOKEN_EXPIRY_WARNING_DAYS = 7;
export const REQUIRED_TOKEN_SCOPE = 'read_api';

export const SECTION_META = [
  { id: 'reviewing', title: 'Меня назначили ревьюером' },
  { id: 'assigned', title: 'Назначены на меня' },
  { id: 'mentioned', title: 'Меня упомянули' },
  { id: 'awaitingApproval', title: 'Ждут моего апрува' },
  { id: 'threads', title: 'Ждут моего ответа в тредах' },
  { id: 'authored', title: 'Мои MR' },
  { id: 'possibleMrs', title: 'Возможные MR' },
  { id: 'brokenBranches', title: 'Сломан default branch' },
];

export const SECTION_IDS = SECTION_META.map(({ id }) => id);

const TRANSIENT_MERGE_STATUSES = new Set([
  'checking',
  'unchecked',
  'preparing',
  'approvals_syncing',
  'cannot_be_merged_recheck',
]);

const ACTIVE_PIPELINE_STATUSES = new Set([
  'created',
  'waiting_for_resource',
  'preparing',
  'pending',
  'running',
  'waiting_for_callback',
  'canceling',
]);

const isFailedPipeline = (item) => item.pipeline?.status === 'failed' && !item.pipeline.onlyIgnoredFailures;
const AUTHOR_TURN_REVIEW_STATES = new Set(['requested_changes', 'reviewed']);

export const awaitsAuthor = (item) => AUTHOR_TURN_REVIEW_STATES.has(item.myReviewState);
const awaitsMyReview = (item) => !item.approvedByMe && !item.draft && !awaitsAuthor(item);

export function actionableKeys({ reviewing, assigned, mentioned, awaitingApproval, threads, authored }) {
  return {
    waiting: new Set([
      ...mentioned.map((item) => item.key),
      ...reviewing.filter(awaitsMyReview).map((item) => item.key),
      ...assigned.filter(awaitsMyReview).map((item) => item.key),
      ...awaitingApproval.filter((item) => !awaitsAuthor(item)).map((item) => item.key),
      ...threads.map((item) => item.key),
    ]),
    failedCi: new Set(authored.filter(isFailedPipeline).map((item) => item.key)),
  };
}

function countActionable(sections) {
  const { waiting, failedCi } = actionableKeys(sections);
  return { waiting: waiting.size, failedCi: failedCi.size };
}

export function itemsByKey(dashboard) {
  return new Map(SECTION_IDS.flatMap((id) => dashboard.sections?.[id] ?? []).map((item) => [item.key, item]));
}

export function deriveView(dashboard, userState = {}, now = Date.now(), hiddenSections = []) {
  const snoozed = userState.snoozed ?? {};
  const isSnoozed = (item) => isSnoozeActive(snoozed[item.key], item, now);
  const sections = Object.fromEntries(
    SECTION_IDS.map((id) => [id, (dashboard.sections?.[id] ?? []).filter((item) => !isSnoozed(item))]),
  );
  const snoozedItems = [...itemsByKey(dashboard).values()]
    .filter(isSnoozed)
    .map((item) => ({ ...item, snooze: snoozed[item.key] }));
  const visibleSections = hiddenSections.length
    ? Object.fromEntries(SECTION_IDS.map((id) => [id, hiddenSections.includes(id) ? [] : sections[id]]))
    : sections;
  return { sections, snoozed: snoozedItems, counts: countActionable(visibleSections) };
}

export function sectionsToSkip(settings) {
  return settings.hiddenSections ?? [];
}

const sameSource = (previous, next) =>
  previous.baseUrl === next.baseUrl &&
  previous.username === next.username &&
  JSON.stringify(previous.filters ?? null) === JSON.stringify(next.filters ?? null);

export const hasActivePipelines = (dashboard) =>
  (dashboard.sections?.authored ?? []).some(
    (item) =>
      ACTIVE_PIPELINE_STATUSES.has(item.pipeline?.status) ||
      item.status === 'ci_still_running' ||
      TRANSIENT_MERGE_STATUSES.has(item.status),
  );

export function withStableStatuses(previous, next) {
  if (!previous?.sections || !next.sections || !sameSource(previous, next)) {
    return next;
  }
  const previousItems = itemsByKey(previous);
  const stabilize = (item) => {
    const prev = previousItems.get(item.key);
    if (!prev) {
      return item;
    }
    const keepStatus = TRANSIENT_MERGE_STATUSES.has(item.status) && prev.status && !TRANSIENT_MERGE_STATUSES.has(prev.status);
    const keepReviewState = item.kind === 'mr' && item.myReviewState === undefined && prev.myReviewState !== undefined;
    return keepStatus || keepReviewState
      ? {
          ...item,
          ...(keepStatus && { status: prev.status }),
          ...(keepReviewState && { myReviewState: prev.myReviewState }),
        }
      : item;
  };
  return {
    ...next,
    sections: Object.fromEntries(Object.entries(next.sections).map(([id, items]) => [id, items.map(stabilize)])),
  };
}

export function tokenProblems(token, dismissed = {}, now = Date.now(), allowedScopes = [REQUIRED_TOKEN_SCOPE]) {
  if (!token) {
    return [];
  }
  const problems = [];
  if (!token.expiresAt) {
    if (!dismissed.noExpiry) {
      problems.push({
        kind: 'noExpiry',
        text: 'Токен бессрочный — лучше пересоздать его со сроком действия',
        dismissKey: 'noExpiry',
      });
    }
  } else {
    const msLeft = Date.parse(token.expiresAt) - now;
    if (msLeft <= 0) {
      problems.push({ kind: 'expiry', text: 'Срок действия токена истёк' });
    } else if (msLeft < DAY_MS) {
      problems.push({ kind: 'expiry', text: 'Токен истекает сегодня' });
    } else if (msLeft <= TOKEN_EXPIRY_WARNING_DAYS * DAY_MS) {
      problems.push({ kind: 'expiry', text: `Токен истекает через ${Math.floor(msLeft / DAY_MS)} дн.` });
    }
  }
  const extraScopes = token.scopes.filter((scope) => !allowedScopes.includes(scope)).sort();
  const extraScopesKey = extraScopes.join(',');
  if (extraScopes.length && dismissed.extraScopes !== extraScopesKey) {
    problems.push({
      kind: 'extraScopes',
      text: `У токена лишние права: ${extraScopes.join(', ')} — достаточно ${allowedScopes.join(', ')}`,
      dismissKey: extraScopesKey,
    });
  }
  return problems;
}
