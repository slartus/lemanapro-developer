import { NOTIFICATION_TYPES } from './notifications.js';
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

const MERGEABLE_STATUSES = new Set(['mergeable', 'can_be_merged']);
const TRANSIENT_MERGE_STATUSES = new Set([
  'checking',
  'unchecked',
  'preparing',
  'approvals_syncing',
  'cannot_be_merged_recheck',
]);

const READY_PIPELINE_STATUSES = new Set(['success', 'skipped']);
const ACTIVE_PIPELINE_STATUSES = new Set([
  'created',
  'waiting_for_resource',
  'preparing',
  'pending',
  'running',
  'waiting_for_callback',
  'canceling',
]);

const isMergeable = (item) => MERGEABLE_STATUSES.has(item.status) && !item.draft;
const isReadyToMerge = (item) =>
  isMergeable(item) && (!item.pipeline || READY_PIPELINE_STATUSES.has(item.pipeline.status));
const isFailedPipeline = (item) => item.pipeline?.status === 'failed' && !item.pipeline.onlyIgnoredFailures;
const AUTHOR_TURN_REVIEW_STATES = new Set(['requested_changes', 'reviewed']);

export const awaitsAuthor = (item) => AUTHOR_TURN_REVIEW_STATES.has(item.myReviewState);
const awaitsMyReview = (item) => !item.approvedByMe && !item.draft && !awaitsAuthor(item);

function countActionable({ reviewing, assigned, mentioned, awaitingApproval, threads, authored }) {
  const waiting = new Set([
    ...mentioned.map((item) => item.key),
    ...reviewing.filter(awaitsMyReview).map((item) => item.key),
    ...assigned.filter(awaitsMyReview).map((item) => item.key),
    ...awaitingApproval.filter((item) => !awaitsAuthor(item)).map((item) => item.key),
    ...threads.map((item) => item.key),
  ]);
  return { waiting: waiting.size, failedCi: authored.filter(isFailedPipeline).length };
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

const NOTIFICATION_RULES = [
  {
    type: 'review',
    section: 'reviewing',
    applies: awaitsMyReview,
    isNew: (item, prev) => !prev || !awaitsMyReview(prev),
  },
  {
    type: 'assigned',
    section: 'assigned',
    sources: ['assignedUnavailable'],
    applies: awaitsMyReview,
    isNew: (item, prev) => !prev || !awaitsMyReview(prev),
  },
  {
    type: 'mention',
    section: 'mentioned',
    sources: ['mentionsUnavailable', 'assignedUnavailable'],
    isNew: (item, prev) => !prev || item.mentionCount > prev.mentionCount || item.mentionedAt > prev.mentionedAt,
  },
  {
    type: 'approval',
    section: 'awaitingApproval',
    sources: ['assignedUnavailable'],
    applies: awaitsMyReview,
    isNew: (item, prev) => !prev || !awaitsMyReview(prev),
  },
  {
    type: 'thread',
    section: 'threads',
    isNew: (item, prev) => !prev || item.threadsAwaitingReply > prev.threadsAwaitingReply,
  },
  {
    type: 'ciFailed',
    section: 'authored',
    sources: ['pipelinesUnavailable'],
    applies: isFailedPipeline,
    isNew: (item, prev) => !prev || !isFailedPipeline(prev) || prev.pipeline.id !== item.pipeline.id,
  },
  {
    type: 'mergeable',
    section: 'authored',
    sources: ['pipelinesUnavailable'],
    applies: isReadyToMerge,
    isNew: (item, prev) => Boolean(prev) && !isReadyToMerge(prev),
  },
  {
    type: 'branchBroken',
    section: 'brokenBranches',
    sources: ['defaultBranchesUnavailable'],
    isNew: (item, prev) => !prev,
  },
];

export const NOTIFICATION_SECTIONS = Object.fromEntries(
  NOTIFICATION_RULES.map(({ type, section }) => [type, section]),
);

export function sectionsToSkip(settings) {
  const hidden = settings.hiddenSections ?? [];
  if (!settings.notifications) {
    return hidden;
  }
  const muted = new Set(settings.mutedNotificationTypes ?? []);
  const notifying = new Set(
    NOTIFICATION_TYPES.filter(({ type }) => !muted.has(type))
      .map(({ type }) => NOTIFICATION_SECTIONS[type])
      .filter(Boolean),
  );
  return hidden.filter((id) => !notifying.has(id));
}

const isComplete = (dashboard) =>
  !dashboard.skippedMrs &&
  !dashboard.skippedProjects &&
  !dashboard.projectsUnavailable &&
  !dashboard.assignedUnavailable &&
  !dashboard.skippedSections?.length;

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

export function collectNotificationEvents(previous, next, userState = {}, now = Date.now()) {
  if (!previous?.sections || !sameSource(previous, next)) {
    return [];
  }
  const previousView = deriveView(previous, {}, now);
  const nextView = deriveView(next, userState, now);
  const previousComplete = isComplete(previous);
  const isApplicable = ({ section, sources = [] }) =>
    Boolean(previous.sections[section]) && sources.every((source) => !previous[source] && !next[source]);
  return NOTIFICATION_RULES.filter(isApplicable).flatMap(
    ({ type, section, applies = () => true, isNew }) => {
      const previousByKey = new Map(previousView.sections[section].map((item) => [item.key, item]));
      return nextView.sections[section]
        .filter((item) => {
          const prev = previousByKey.get(item.key);
          return applies(item) && (prev || previousComplete) && isNew(item, prev);
        })
        .map((item) => ({ type, item }));
    },
  );
}

export function sortPendingEvents(events, dashboard, userState = {}, now = Date.now()) {
  const relevant = [];
  const waiting = [];
  if (!dashboard?.sections) {
    return { relevant, waiting };
  }
  const view = deriveView(dashboard, userState, now);
  const rulesByType = new Map(NOTIFICATION_RULES.map((rule) => [rule.type, rule]));
  events.forEach((event) => {
    const rule = rulesByType.get(event.type);
    if (!rule) {
      return;
    }
    if ((rule.sources ?? []).some((source) => dashboard[source])) {
      waiting.push(event);
      return;
    }
    const current = view.sections[rule.section].find((candidate) => candidate.key === event.item.key);
    if (current && (rule.applies ?? (() => true))(current)) {
      relevant.push({ type: event.type, item: current });
    }
  });
  return { relevant, waiting };
}

export function tokenProblems(token, dismissed = {}, now = Date.now()) {
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
  const extraScopes = token.scopes.filter((scope) => scope !== REQUIRED_TOKEN_SCOPE).sort();
  const extraScopesKey = extraScopes.join(',');
  if (extraScopes.length && dismissed.extraScopes !== extraScopesKey) {
    problems.push({
      kind: 'extraScopes',
      text: `У токена лишние права: ${extraScopes.join(', ')} — достаточно ${REQUIRED_TOKEN_SCOPE}`,
      dismissKey: extraScopesKey,
    });
  }
  return problems;
}
