import {
  DEFAULT_BRANCH_PIPELINES_BATCH,
  GitLabError,
  MERGE_REQUEST_STATES_BATCH,
  MERGE_REQUEST_VERDICTS_BATCH,
  fetchApprovals,
  fetchAuthoredPipelines,
  fetchDefaultBranchPipelines,
  fetchDiscussions,
  fetchFirstUniqueCommit,
  fetchMaintainedProjects,
  fetchMergeRequestStates,
  fetchMergeRequestVerdicts,
  fetchPipelineFailures,
  fetchTokenInfo,
  reviewVerdictOf,
} from './gitlab.js';
import { TRANSIENT_MERGE_STATUSES } from './view.js';

const CONCURRENCY = 6;
const SKIPPABLE_STATUSES = new Set([403, 404]);
const FATAL_STATUSES = new Set([401, 429]);
const MENTION_ACTIONS = new Set(['mentioned', 'directly_addressed']);
const MENTION_TARGET_TYPES = ['MergeRequest', 'Issue'];
const REVIEW_STATE_SCHEMA_ERROR = /mergeRequestInteraction|reviewState|MergeRequestReviewer/;
const REVIEW_VERDICT_SCHEMA_ERROR = /NotesFilterType|ONLY_ACTIVITY|argument 'filter'/;

export const mrKey = (mr) => `${mr.project_id}!${mr.iid}`;
const issueKey = (issue) => `${issue.project_id}#${issue.iid}`;
const todoKey = (todo) => (todo.target_type === 'Issue' ? issueKey(todo.target) : mrKey(todo.target));
const mrReference = (mr) => mr.references?.full ?? `!${mr.iid}`;
const isDraft = (mr) => Boolean(mr.draft ?? mr.work_in_progress);
const projectPathOf = (mr) => {
  const reference = mr.references?.full;
  return reference?.includes('!') ? reference.slice(0, reference.lastIndexOf('!')) : null;
};
const headPipelineOf = (mr) => {
  const pipeline = mr.head_pipeline;
  const projectPath = projectPathOf(mr);
  if (!pipeline?.id || !projectPath) {
    return null;
  }
  return {
    id: pipeline.id,
    projectPath,
    sha: pipeline.sha,
    status: String(pipeline.status ?? '').toLowerCase(),
    webUrl: pipeline.web_url ?? null,
  };
};
const headPipelinesOf = (mrs) => {
  const pipelines = new Map();
  (mrs ?? []).forEach((mr) => {
    const pipeline = headPipelineOf(mr);
    if (pipeline) {
      pipelines.set(mrKey(mr), pipeline);
    }
  });
  return pipelines;
};

const buildHeadPipelines = async (client, mrs, previousStatuses) => {
  const candidates = uniqueBy(mrs ?? [], mrKey);
  const pipelines = headPipelinesOf(candidates);
  const unresolved = candidates.filter((mr) => needsHeadPipeline(mr, previousStatuses));
  const resolved = await mapLimit(unresolved, CONCURRENCY, (mr) =>
    optional(
      client.get(`/projects/${mr.project_id}/merge_requests/${mr.iid}`).then((single) => {
        const pipeline = single && headPipelineOf(single);
        return pipeline ? [mrKey(single), pipeline] : null;
      }),
    ),
  );
  resolved.forEach((entry) => {
    if (entry) {
      pipelines.set(entry[0], entry[1]);
    }
  });
  return pipelines;
};
const uniqueBy = (items, key) => [...new Map(items.map((item) => [key(item), item])).values()];
const byUpdatedDesc = (a, b) => b.updatedAt.localeCompare(a.updatedAt);
const byMentionedDesc = (a, b) => b.mentionedAt.localeCompare(a.mentionedAt);

export const normalizeCheckName = (name) => name.trim().toLowerCase();

async function mapLimit(items, limit, mapper) {
  const results = new Array(items.length);
  let cursor = 0;
  const worker = async () => {
    while (cursor < items.length) {
      const index = cursor++;
      results[index] = await mapper(items[index]);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return results;
}

const isSkippable = (error) =>
  error instanceof GitLabError && (SKIPPABLE_STATUSES.has(error.status) || error.status >= 500);

async function orSkip(action, skippedErrors) {
  try {
    return await action();
  } catch (error) {
    if (isSkippable(error)) {
      skippedErrors.push(error);
      return null;
    }
    throw error;
  }
}

function assertNotAllFailed(results, skippedErrors) {
  const serverError = skippedErrors.find((error) => error.status >= 500);
  if (serverError && results.length > 0 && skippedErrors.length === results.length) {
    throw serverError;
  }
}

const optional = (promise) =>
  promise.catch((error) =>
    error instanceof GitLabError && !FATAL_STATUSES.has(error.status) ? null : Promise.reject(error),
  );

export function countThreadsAwaitingReply(discussions, mr, me) {
  const isMyMr = mr.author.id === me.id;
  return discussions.filter((discussion) => {
    const notes = discussion.notes.filter((note) => !note.system);
    const isUnresolved = notes.some((note) => note.resolvable && !note.resolved);
    if (!isUnresolved) {
      return false;
    }
    const iParticipate = isMyMr || notes.some((note) => note.author.id === me.id);
    const lastNote = notes[notes.length - 1];
    return iParticipate && lastNote.author.id !== me.id;
  }).length;
}

async function fetchThreadsAwaitingReply(client, mr, me) {
  if (!mr.user_notes_count) {
    return 0;
  }
  return countThreadsAwaitingReply(await fetchDiscussions(client, mr), mr, me);
}

async function fetchDetails(client, mr, me, shouldCheckThreads) {
  const approvals = await fetchApprovals(client, mr);
  return {
    approvedByMe: Boolean(approvals.user_has_approved),
    approvalsCount: approvals.approved_by?.length ?? 0,
    approverNames: (approvals.approved_by ?? [])
      .map((entry) => entry.user ?? entry)
      .map((user) => user.name || user.username)
      .filter(Boolean),
    threadsAwaitingReply: shouldCheckThreads ? await fetchThreadsAwaitingReply(client, mr, me) : 0,
  };
}

const threadStateKey = (mr, state) =>
  `${mr.updated_at}:${state.userNotesCount}:${state.unresolvedThreads}:${state.resolvedThreads}`;

async function detailsFromState(client, mr, me, state, shouldCheckThreads, threadCache, verdict) {
  const interactionState = state.reviewStates ? (state.reviewStates[me.id] ?? null) : null;
  const details = {
    approvedByMe: state.approverIds.includes(me.id),
    approvalsCount: state.approverIds.length,
    approverNames: state.approverNames ?? [],
    myReviewState: state.reviewStates
      ? interactionState && interactionState !== 'unreviewed'
        ? interactionState
        : (verdict ?? interactionState ?? null)
      : undefined,
    threadsAwaitingReply: 0,
  };
  if (!shouldCheckThreads || state.userNotesCount === 0 || state.unresolvedThreads <= 0) {
    return { details };
  }
  const stateKey = threadStateKey(mr, state);
  const cached = threadCache[mrKey(mr)];
  const threadsAwaitingReply =
    cached?.stateKey === stateKey
      ? cached.threadsAwaitingReply
      : countThreadsAwaitingReply(await fetchDiscussions(client, mr), mr, me);
  return { details: { ...details, threadsAwaitingReply }, cacheEntry: { stateKey, threadsAwaitingReply } };
}

function chunk(items, size) {
  return Array.from({ length: Math.ceil(items.length / size) }, (_, index) =>
    items.slice(index * size, (index + 1) * size),
  );
}

const isReviewStateSchemaError = (error) =>
  error instanceof GitLabError && error.path === '/graphql' && REVIEW_STATE_SCHEMA_ERROR.test(error.message);

const isReviewVerdictSchemaError = (error) =>
  error instanceof GitLabError && error.path === '/graphql' && REVIEW_VERDICT_SCHEMA_ERROR.test(error.message);

async function fetchProjectStates(client, path, iids, reviewStateSupport) {
  if (!reviewStateSupport.enabled) {
    return fetchMergeRequestStates(client, path, iids);
  }
  try {
    return await fetchMergeRequestStates(client, path, iids, { withReviewState: true });
  } catch (error) {
    if (!isReviewStateSchemaError(error)) {
      throw error;
    }
    const states = await fetchMergeRequestStates(client, path, iids);
    reviewStateSupport.enabled = false;
    return states;
  }
}

async function fetchProjectVerdicts(client, path, iids, verdictSupport) {
  if (!verdictSupport.enabled) {
    return null;
  }
  try {
    return await fetchMergeRequestVerdicts(client, path, iids);
  } catch (error) {
    if (!isReviewVerdictSchemaError(error)) {
      throw error;
    }
    verdictSupport.enabled = false;
    return null;
  }
}

async function fetchVerdictNotes(client, mrs, verdictSupport) {
  const iidsByPath = new Map();
  mrs.forEach((mr) => {
    const path = projectPathOf(mr);
    if (path) {
      iidsByPath.set(path, [...(iidsByPath.get(path) ?? []), mr]);
    }
  });
  const batches = [...iidsByPath].flatMap(([path, pathMrs]) =>
    chunk(pathMrs, MERGE_REQUEST_VERDICTS_BATCH).map((batch) => ({ path, batch })),
  );
  const results = await mapLimit(batches, CONCURRENCY, ({ path, batch }) =>
    optional(fetchProjectVerdicts(client, path, batch.map((mr) => mr.iid), verdictSupport)),
  );
  const notes = new Map();
  batches.forEach(({ batch }, index) =>
    batch.forEach((mr) => {
      const nodes = results[index]?.get(mr.iid);
      if (nodes) {
        notes.set(mrKey(mr), nodes);
      }
    }),
  );
  return notes;
}

async function fetchStates(client, mrs) {
  const iidsByPath = new Map();
  mrs.forEach((mr) => {
    const path = projectPathOf(mr);
    if (path) {
      iidsByPath.set(path, [...(iidsByPath.get(path) ?? []), mr]);
    }
  });
  const batches = [...iidsByPath].flatMap(([path, pathMrs]) =>
    chunk(pathMrs, MERGE_REQUEST_STATES_BATCH).map((batch) => ({ path, batch })),
  );
  const reviewStateSupport = { enabled: true };
  const results = await mapLimit(batches, CONCURRENCY, ({ path, batch }) =>
    optional(fetchProjectStates(client, path, batch.map((mr) => mr.iid), reviewStateSupport)),
  );
  const states = new Map();
  batches.forEach(({ batch }, index) =>
    batch.forEach((mr) => {
      const state = results[index]?.get(mr.iid);
      if (state) {
        states.set(mrKey(mr), state);
      }
    }),
  );
  return states;
}

function toItem(mr, extra) {
  return {
    key: mrKey(mr),
    kind: 'mr',
    projectId: mr.project_id,
    title: mr.title,
    webUrl: mr.web_url,
    reference: mrReference(mr),
    author: mr.author.username,
    draft: isDraft(mr),
    status: mr.detailed_merge_status ?? mr.merge_status,
    hasConflicts: Boolean(mr.has_conflicts),
    updatedAt: mr.updated_at,
    ...extra,
  };
}

export function collectMentions(todos, excludedKeys) {
  const todosByMr = new Map();
  todos
    .filter((todo) => MENTION_ACTIONS.has(todo.action_name) && todo.target?.state === 'opened')
    .filter((todo) => !excludedKeys.has(todoKey(todo)))
    .forEach((todo) => {
      const key = todoKey(todo);
      todosByMr.set(key, [...(todosByMr.get(key) ?? []), todo]);
    });
  return [...todosByMr.values()].map((mrTodos) => {
    const latest = mrTodos.reduce((a, b) => (a.created_at > b.created_at ? a : b));
    const isIssue = latest.target_type === 'Issue';
    return toItem(latest.target, {
      ...(isIssue && { key: issueKey(latest.target), kind: 'issue', status: null }),
      mentionedBy: [...new Set(mrTodos.map((todo) => todo.author.username))],
      mentionCount: mrTodos.length,
      mentionUrl: latest.target_url,
      mentionedAt: latest.created_at,
    });
  });
}

function withFailures(pipeline, failures, ignoredChecks) {
  if (pipeline?.status !== 'failed') {
    return pipeline;
  }
  const blockingFailures = failures?.filter((failure) => !ignoredChecks.has(normalizeCheckName(failure.name)));
  return {
    ...pipeline,
    failures,
    onlyIgnoredFailures: Boolean(failures?.length) && blockingFailures.length === 0,
  };
}

const pipelineOf = (raw, mr) =>
  raw.authoredPipelines?.get(mrReference(mr)) ?? raw.headPipelines?.get(mrKey(mr)) ?? null;

const todoMrsOf = (todos) =>
  (todos ?? []).filter((todo) => todo.target_type === 'MergeRequest').map((todo) => todo.target);

const CI_BLOCKED_MERGE_STATUS = 'ci_must_pass';

const needsHeadPipeline = (mr, previousStatuses) => {
  if (mr.head_pipeline?.id) {
    return false;
  }
  const current = mr.detailed_merge_status ?? mr.merge_status;
  const previous = previousStatuses?.get(mrKey(mr));
  const effective = previous && TRANSIENT_MERGE_STATUSES.has(current) ? previous : current;
  return effective === CI_BLOCKED_MERGE_STATUS;
};

async function fetchMentionTodos(client) {
  const lists = await Promise.all(
    MENTION_TARGET_TYPES.map((type) => client.getAll('/todos', { state: 'pending', type })),
  );
  return lists.flat();
}

async function fetchBrokenDefaultBranches(client, projects) {
  const candidates = projects.filter((project) => project.default_branch && project.path_with_namespace);
  const batches = chunk(candidates, DEFAULT_BRANCH_PIPELINES_BATCH);
  const results = await mapLimit(batches, CONCURRENCY, (batch) =>
    optional(fetchDefaultBranchPipelines(client, batch)),
  );
  const broken = batches.flatMap((batch, index) =>
    batch
      .map((project, position) => ({ project, pipeline: results[index]?.[position] }))
      .filter(({ pipeline }) => pipeline?.status === 'failed')
      .map(({ project, pipeline }) => ({
        key: `branch:${project.id}`,
        kind: 'branch',
        projectId: project.id,
        title: project.path_with_namespace,
        reference: project.default_branch,
        webUrl: pipeline.webUrl,
        updatedAt: pipeline.finishedAt ?? new Date(0).toISOString(),
      })),
  );
  const unavailable = results.some((result) => result === null || result.includes(undefined));
  return { broken, unavailable };
}

function createMrUrl(baseUrl, projectPath, branchName) {
  const url = new URL(baseUrl);
  url.pathname = `${url.pathname.replace(/\/+$/, '')}/${projectPath}/-/merge_requests/new`;
  url.searchParams.set('merge_request[source_branch]', branchName);
  return url.href;
}

const myIdentitiesOf = (me) =>
  [me.name, me.username, me.email, me.public_email, me.commit_email]
    .filter(Boolean)
    .map((identity) => identity.toLowerCase());

const isMineCommit = (commit, identities) => {
  const c = commit ?? {};
  return [c.author_name, c.committer_name, c.author_email, c.committer_email].some((value) =>
    identities.includes((value ?? '').toLowerCase()),
  );
};

const branchOriginCache = new Map();
const BRANCH_ORIGIN_CACHE_LIMIT = 1000;

async function firstUniqueCommitOf(project, branch, client) {
  const cacheKey = `${project.id}:${branch.name}:${branch.commit?.id ?? ''}`;
  let commit = branchOriginCache.get(cacheKey);
  if (commit !== undefined) {
    return commit;
  }
  try {
    commit = await fetchFirstUniqueCommit(client, project.id, project.default_branch, branch.name);
  } catch (error) {
    if (error instanceof GitLabError && FATAL_STATUSES.has(error.status)) {
      throw error;
    }
    return null;
  }
  if (branchOriginCache.size >= BRANCH_ORIGIN_CACHE_LIMIT) {
    branchOriginCache.clear();
  }
  branchOriginCache.set(cacheKey, commit);
  return commit;
}

const EXISTING_MR_STATES = new Set(['closed', 'merged']);

async function existingMrOf(client, project, branch) {
  const mrs = await client.getAll(`/projects/${project.id}/merge_requests`, {
    source_branch: branch.name,
    state: 'all',
  });
  const candidates = mrs.filter((mr) => EXISTING_MR_STATES.has(mr.state));
  return candidates.length ? candidates.reduce((latest, mr) => (mr.iid > latest.iid ? mr : latest)) : null;
}

export async function collectPossibleMrs(project, branches, openSourceBranches, me, baseUrl, client) {
  const identities = myIdentitiesOf(me);
  const candidates = branches.filter(
    (branch) => !branch.default && !openSourceBranches.has(branch.name),
  );
  const owned = await mapLimit(candidates, CONCURRENCY, async (branch) => {
    if (isMineCommit(branch.commit, identities)) {
      return branch;
    }
    if (!project.default_branch || !client) {
      return null;
    }
    return isMineCommit(await firstUniqueCommitOf(project, branch, client), identities) ? branch : null;
  });
  return mapLimit(owned.filter(Boolean), CONCURRENCY, async (branch) => {
    let existingMr = null;
    if (client) {
      try {
        existingMr = await existingMrOf(client, project, branch);
      } catch (error) {
        if (error instanceof GitLabError && FATAL_STATUSES.has(error.status)) {
          throw error;
        }
      }
    }
    return {
      key: `branch:${project.id}:${branch.name}`,
      kind: 'possibleMr',
      projectId: project.id,
      projectPath: project.path_with_namespace,
      title: branch.name,
      reference: project.path_with_namespace,
      webUrl: createMrUrl(baseUrl, project.path_with_namespace, branch.name),
      author: null,
      merged: Boolean(branch.merged),
      updatedAt: branch.commit?.committed_date ?? branch.commit?.authored_date ?? new Date(0).toISOString(),
      ...(existingMr
        ? {
            mrState: existingMr.state,
            mrIid: existingMr.iid,
            mrTitle: existingMr.title,
            mrUrl: existingMr.web_url,
          }
        : {}),
    };
  });
}

async function combinePossibleMrs(projects, branchLists, mrListsByProject, me, baseUrl, client) {
  const items = [];
  for (const [index, project] of projects.entries()) {
    const branches = branchLists[index];
    const openMrs = mrListsByProject[index];
    if (!branches || openMrs === null) {
      continue;
    }
    const openSourceBranches = new Set(openMrs.map((mr) => mr.source_branch));
    items.push(...(await collectPossibleMrs(project, branches, openSourceBranches, me, baseUrl, client)));
  }
  return { items, unavailable: branchLists.some((list) => list === null) };
}

const needs = (skipSections) => {
  const skipped = new Set(skipSections);
  const need = (id) => !skipped.has(id);
  return {
    reviewing: need('reviewing'),
    assigned: need('assigned'),
    mentioned: need('mentioned'),
    awaitingApproval: need('awaitingApproval'),
    threads: need('threads'),
    authored: need('authored'),
    possibleMrs: need('possibleMrs'),
    brokenBranches: need('brokenBranches'),
  };
};

export async function fetchRaw(client, { selectedProjectIds = null, skipSections = [], previousStatuses = null } = {}) {
  const me = await client.get('/user');
  const n = needs(skipSections);
  const needAuthoredMrs = n.authored || n.threads;
  const needReviewingMrs = n.reviewing || n.assigned || n.threads || n.mentioned || n.awaitingApproval;
  const needAssignedMrs = n.assigned || n.threads || n.mentioned || n.awaitingApproval;
  const needMaintainedProjects = n.awaitingApproval || n.brokenBranches || n.possibleMrs;

  const [maintainedProjects, authoredMrs, reviewingMrs, assignedMrs, authoredPipelines, todos, token] =
    await Promise.all([
      needMaintainedProjects ? optional(fetchMaintainedProjects(client)) : [],
      needAuthoredMrs
        ? client.getAll('/merge_requests', { scope: 'created_by_me', state: 'opened', with_merge_status_recheck: true })
        : [],
      needReviewingMrs
        ? client.getAll('/merge_requests', { scope: 'all', state: 'opened', reviewer_id: me.id })
        : [],
      needAssignedMrs
        ? optional(
            client.getAll('/merge_requests', { scope: 'assigned_to_me', state: 'opened', with_merge_status_recheck: true }),
          )
        : [],
      n.authored ? optional(fetchAuthoredPipelines(client)) : new Map(),
      n.mentioned ? optional(fetchMentionTodos(client)) : [],
      optional(fetchTokenInfo(client)),
    ]);
  const watchedIds = selectedProjectIds && new Set(selectedProjectIds);
  const watchedProjects = needMaintainedProjects
    ? (maintainedProjects ?? []).filter((project) => !watchedIds || watchedIds.has(project.id))
    : [];

  const needMaintainedMrLists = n.awaitingApproval || n.possibleMrs;
  const skippedErrors = [];
  const [maintainedMrLists, defaultBranches, branchLists] = await Promise.all([
    needMaintainedMrLists
      ? mapLimit(watchedProjects, CONCURRENCY, (project) =>
          orSkip(
            () => client.getAll(`/projects/${project.id}/merge_requests`, { state: 'opened' }),
            skippedErrors,
          ),
        )
      : [],
    n.brokenBranches ? fetchBrokenDefaultBranches(client, watchedProjects) : { broken: [], unavailable: false },
    n.possibleMrs
      ? mapLimit(watchedProjects, CONCURRENCY, (project) =>
          optional(client.getAll(`/projects/${project.id}/repository/branches`)),
        )
      : null,
  ]);
  assertNotAllFailed(maintainedMrLists, skippedErrors);
  const possibleMrs =
    branchLists === null
      ? { items: [], unavailable: false }
      : await combinePossibleMrs(watchedProjects, branchLists, maintainedMrLists, me, client.baseUrl, client);

  return {
    me,
    authoredMrs,
    reviewingMrs,
    assignedMrs: (assignedMrs ?? []).filter((mr) => mr.author.id !== me.id),
    assignedUnavailable: assignedMrs === null,
    maintainedMrs: maintainedMrLists
      .flatMap((list) => list ?? [])
      .filter((mr) => !isDraft(mr) && mr.author.id !== me.id),
    headPipelines: await buildHeadPipelines(
      client,
      [...reviewingMrs, ...(assignedMrs ?? []), ...maintainedMrLists.flatMap((list) => list ?? []), ...todoMrsOf(todos)],
      previousStatuses,
    ),
    watchedProjects: watchedProjects.length,
    skippedProjects: maintainedMrLists.filter((list) => list === null).length,
    projectsUnavailable: maintainedProjects === null,
    projectsSkipped: !needMaintainedProjects,
    authoredPipelines,
    todos,
    token,
    brokenBranches: defaultBranches.broken,
    defaultBranchesUnavailable: defaultBranches.unavailable,
    possibleMrs: possibleMrs.items,
    possibleMrsUnavailable: possibleMrs.unavailable,
  };
}

export async function enrich(client, raw, threadCache = null, skipSections = []) {
  const { me, authoredMrs, reviewingMrs, assignedMrs, maintainedMrs } = raw;
  const n = needs(skipSections);
  if (!(n.reviewing || n.assigned || n.authored || n.awaitingApproval || n.threads || n.mentioned)) {
    return {
      details: new Map(),
      pipelineFailures: new Map(),
      skippedMrs: 0,
      threadCache: threadCache?.userId === me.id ? threadCache : { userId: me.id, entries: {} },
    };
  }
  const threadWatchKeys = new Set([...authoredMrs, ...reviewingMrs, ...assignedMrs].map(mrKey));
  const allMrs = uniqueBy([...authoredMrs, ...reviewingMrs, ...assignedMrs, ...maintainedMrs], mrKey);
  const cachedThreads = threadCache?.userId === me.id ? threadCache.entries : {};
  const cachedVerdicts = threadCache?.userId === me.id ? (threadCache.verdictEntries ?? {}) : {};

  const states = await fetchStates(client, allMrs);

  const verdictEntries = { ...cachedVerdicts };
  const verdicts = new Map();
  if (n.reviewing || n.awaitingApproval) {
    const verdictSupport = { enabled: true };
    const candidates = allMrs.filter((mr) => {
      if (mr.author.id === me.id) {
        return false;
      }
      const state = states.get(mrKey(mr));
      if (!state?.reviewStates) {
        return false;
      }
      const interaction = state.reviewStates[me.id];
      if (interaction && interaction !== 'unreviewed') {
        return false;
      }
      const entry = verdictEntries[mrKey(mr)];
      return !entry || entry.updatedAt !== mr.updated_at;
    });
    const notes = candidates.length ? await fetchVerdictNotes(client, candidates, verdictSupport) : new Map();
    candidates.forEach((mr) => {
      const nodes = notes.get(mrKey(mr));
      if (nodes) {
        const verdict = reviewVerdictOf(nodes, me);
        verdicts.set(mrKey(mr), verdict);
        verdictEntries[mrKey(mr)] = { updatedAt: mr.updated_at, verdict };
      }
    });
    const liveKeys = new Set(allMrs.map(mrKey));
    Object.keys(verdictEntries).forEach((key) => {
      if (!liveKeys.has(key)) {
        delete verdictEntries[key];
      }
    });
  }
  const verdictOf = (mr) => {
    if (verdicts.has(mrKey(mr))) {
      return verdicts.get(mrKey(mr));
    }
    const entry = verdictEntries[mrKey(mr)];
    return entry?.updatedAt === mr.updated_at ? entry.verdict : undefined;
  };

  const skippedErrors = [];
  const results = await mapLimit(allMrs, CONCURRENCY, (mr) =>
    orSkip(async () => {
      const shouldCheckThreads = threadWatchKeys.has(mrKey(mr));
      const state = states.get(mrKey(mr));
      return state
        ? detailsFromState(client, mr, me, state, shouldCheckThreads, cachedThreads, verdictOf(mr))
        : { details: await fetchDetails(client, mr, me, shouldCheckThreads) };
    }, skippedErrors),
  );
  assertNotAllFailed(results, skippedErrors);
  const detailsByKey = new Map(
    allMrs.map((mr, index) => [mrKey(mr), results[index]?.details]).filter(([, detail]) => detail),
  );
  const threadEntries = Object.fromEntries(
    allMrs.map((mr, index) => [mrKey(mr), results[index]?.cacheEntry]).filter(([, entry]) => entry),
  );

  const failedWatched = allMrs.filter(
    (mr) => detailsByKey.has(mrKey(mr)) && pipelineOf(raw, mr)?.status === 'failed',
  );
  const failedMentioned = todoMrsOf(raw.todos).filter(
    (mr) => !detailsByKey.has(mrKey(mr)) && pipelineOf(raw, mr)?.status === 'failed',
  );
  const failedMrs = uniqueBy([...failedWatched, ...failedMentioned], mrKey);
  const failures = await mapLimit(failedMrs, CONCURRENCY, (mr) =>
    optional(fetchPipelineFailures(client, pipelineOf(raw, mr))),
  );

  return {
    details: detailsByKey,
    pipelineFailures: new Map(failedMrs.map((mr, index) => [mrKey(mr), failures[index]])),
    skippedMrs: results.filter((result) => result === null).length,
    threadCache: { userId: me.id, entries: threadEntries, verdictEntries },
  };
}

export function classify(
  raw,
  { details, pipelineFailures },
  { ignoredCheckNames = [], approvalThreshold = 0 } = {},
) {
  const { me, authoredMrs, reviewingMrs, assignedMrs, maintainedMrs, todos } = raw;
  const reviewingKeys = new Set(reviewingMrs.map(mrKey));
  const ignoredChecks = new Set(ignoredCheckNames.map(normalizeCheckName));
  const withPipeline = (item) =>
    item.kind === 'mr' && item.pipeline === undefined
      ? {
          ...item,
          pipeline: withFailures(
            raw.headPipelines?.get(item.key) ?? null,
            pipelineFailures.get(item.key) ?? null,
            ignoredChecks,
          ),
        }
      : item;
  const toItems = (mrs) =>
    mrs
      .filter((mr) => details.has(mrKey(mr)))
      .map((mr) => {
        const { approvedByMe, approvalsCount, approverNames, myReviewState, threadsAwaitingReply } = details.get(mrKey(mr));
      const item = toItem(mr, { approvedByMe, approvalsCount, approverNames, myReviewState, threadsAwaitingReply });
      return {
        ...item,
        pipeline: withFailures(pipelineOf(raw, mr), pipelineFailures.get(item.key) ?? null, ignoredChecks),
      };
    });

  const reviewing = toItems(reviewingMrs.filter((mr) => mr.author.id !== me.id));
  const assigned = toItems(assignedMrs.filter((mr) => !reviewingKeys.has(mrKey(mr))));
  const personalKeys = new Set([...reviewing, ...assigned].map((item) => item.key));
  const mentioned = collectMentions(todos ?? [], personalKeys).map(withPipeline);
  const awaitingApproval = toItems(maintainedMrs.filter((mr) => !personalKeys.has(mrKey(mr)))).filter(
    (item) => !item.approvedByMe && !(approvalThreshold > 0 && item.approvalsCount >= approvalThreshold),
  );
  const authored = toItems(authoredMrs);
  const threads = uniqueBy([...authored, ...reviewing, ...assigned], (item) => item.key).filter(
    (item) => item.threadsAwaitingReply > 0,
  );

  return {
    reviewing: reviewing.sort(byUpdatedDesc),
    assigned: assigned.sort(byUpdatedDesc),
    mentioned: mentioned.sort(byMentionedDesc),
    awaitingApproval: awaitingApproval.sort(byUpdatedDesc),
    threads: threads.sort(byUpdatedDesc),
    authored: authored.sort(byUpdatedDesc),
    possibleMrs: [...raw.possibleMrs].sort(byUpdatedDesc),
    brokenBranches: [...raw.brokenBranches].sort(byUpdatedDesc),
  };
}

export async function collectDashboard(
  client,
  {
    selectedProjectIds = null,
    ignoredCheckNames = [],
    approvalThreshold = 0,
    threadCache = null,
    skipSections = [],
    previousStatuses = null,
  } = {},
) {
  const raw = await fetchRaw(client, { selectedProjectIds, skipSections, previousStatuses });
  const enriched = await enrich(client, raw, threadCache, skipSections);
  return {
    fetchedAt: new Date().toISOString(),
    username: raw.me.username,
    watchedProjects: raw.watchedProjects,
    skippedProjects: raw.skippedProjects,
    projectsSkipped: raw.projectsSkipped,
    skippedSections: skipSections,
    skippedMrs: enriched.skippedMrs,
    projectsUnavailable: raw.projectsUnavailable,
    pipelinesUnavailable: raw.authoredPipelines === null,
    mentionsUnavailable: raw.todos === null,
    assignedUnavailable: raw.assignedUnavailable,
    defaultBranchesUnavailable: raw.defaultBranchesUnavailable,
    possibleMrsUnavailable: raw.possibleMrsUnavailable,
    token: raw.token,
    sections: classify(raw, enriched, { ignoredCheckNames, approvalThreshold }),
    threadCache: enriched.threadCache,
  };
}
