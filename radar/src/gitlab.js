const PER_PAGE = 100;
const MAINTAINER_ACCESS_LEVEL = 40;
const REQUEST_TIMEOUT_MS = 20_000;
const MAX_RATE_LIMIT_RETRIES = 2;
const MAX_RATE_LIMIT_WAIT_MS = 25_000;

const AUTHORED_PIPELINES_QUERY = `
  query AuthoredPipelines($after: String) {
    currentUser {
      authoredMergeRequests(state: opened, first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          iid
          project { fullPath }
          headPipeline { id sha status path project { fullPath } }
        }
      }
    }
  }
`;

const mergeRequestStatesQuery = (withReviewState) => `
  query MergeRequestStates($fullPath: ID!, $iids: [String!]) {
    project(fullPath: $fullPath) {
      mergeRequests(iids: $iids, first: 100) {
        nodes {
          iid
          userNotesCount
          resolvableDiscussionsCount
          resolvedDiscussionsCount
          approvedBy { nodes { id name username } }
          ${withReviewState ? 'reviewers { nodes { id mergeRequestInteraction { reviewState } } }' : ''}
        }
      }
    }
  }
`;

const mergeRequestVerdictsQuery = `
  query MergeRequestVerdicts($fullPath: ID!, $iids: [String!]) {
    project(fullPath: $fullPath) {
      mergeRequests(iids: $iids, first: 100) {
        nodes {
          iid
          notes(filter: ONLY_ACTIVITY, last: 100) {
            nodes { system body createdAt author { username } }
          }
        }
      }
    }
  }
`;

export const MERGE_REQUEST_STATES_BATCH = 100;
export const MERGE_REQUEST_VERDICTS_BATCH = 25;
export const DEFAULT_BRANCH_PIPELINES_BATCH = 20;
const DEFAULT_BRANCH_PIPELINES_DEPTH = 5;
const CONCLUSIVE_PIPELINE_STATUSES = new Set(['success', 'failed']);

function defaultBranchPipelinesQuery(count) {
  const indexes = Array.from({ length: count }, (_, index) => index);
  const variables = indexes.map((index) => `$path${index}: ID!, $ref${index}: String!`).join(', ');
  const projects = indexes
    .map(
      (index) =>
        `p${index}: project(fullPath: $path${index}) { pipelines(ref: $ref${index}, scope: FINISHED, first: ${DEFAULT_BRANCH_PIPELINES_DEPTH}) { nodes { status path finishedAt } } }`,
    )
    .join('\n');
  return `query DefaultBranchPipelines(${variables}) {\n${projects}\n}`;
}

export class GitLabError extends Error {
  constructor(status, message, path) {
    super(message);
    this.status = status;
    this.path = path;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export class GitLabClient {
  constructor(baseUrl, token, { apiBase = baseUrl } = {}) {
    this.baseUrl = baseUrl;
    this.apiUrl = `${apiBase}/api/v4`;
    this.graphqlUrl = `${apiBase}/api/graphql`;
    this.token = token;
    this.pausedUntil = 0;
    this.requestCount = 0;
  }

  async request(path, params = {}) {
    const url = new URL(`${this.apiUrl}${path}`);
    Object.entries(params).forEach(([key, value]) => url.searchParams.set(key, String(value)));
    const response = await this.send(url, {}, path);
    return {
      data: await response.json(),
      nextPage: response.headers.get('x-next-page'),
    };
  }

  async graphql(query, variables = {}) {
    const { data, errors } = await this.graphqlPartial(query, variables);
    if (errors.length) {
      throw new GitLabError(200, `GraphQL: ${errors[0].message}`, '/graphql');
    }
    return data;
  }

  async graphqlPartial(query, variables = {}) {
    const response = await this.send(
      this.graphqlUrl,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${this.token}` },
        body: JSON.stringify({ query, variables }),
      },
      '/graphql',
    );
    const { data = null, errors = [] } = await response.json();
    if (!data && errors.length) {
      throw new GitLabError(response.status, `GraphQL: ${errors[0].message}`, '/graphql');
    }
    return { data, errors };
  }

  async send(url, init, path) {
    for (let attempt = 0; ; attempt++) {
      await this.waitForRateLimit(path);
      this.requestCount++;
      const response = await this.fetchWithToken(url, init);
      if (response.status === 429 && attempt < MAX_RATE_LIMIT_RETRIES) {
        const retryAfterMs = (Number(response.headers.get('retry-after')) || 5) * 1000;
        this.pausedUntil = Math.max(this.pausedUntil, Date.now() + retryAfterMs);
        continue;
      }
      if (!response.ok) {
        throw new GitLabError(response.status, `${response.status} ${response.statusText}: ${path}`, path);
      }
      return response;
    }
  }

  async waitForRateLimit(path) {
    const waitMs = this.pausedUntil - Date.now();
    if (waitMs > MAX_RATE_LIMIT_WAIT_MS) {
      throw new GitLabError(429, 'Rate limited', path);
    }
    if (waitMs > 0) {
      await sleep(waitMs);
    }
  }

  fetchWithToken(url, { headers = {}, ...init } = {}) {
    return fetch(url, {
      ...init,
      headers: { 'PRIVATE-TOKEN': this.token, ...headers },
      credentials: 'omit',
      cache: 'no-store',
      redirect: 'error',
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  }

  async get(path, params) {
    return (await this.request(path, params)).data;
  }

  async getAll(path, params = {}) {
    const items = [];
    let page = '1';
    while (page) {
      const { data, nextPage } = await this.request(path, { ...params, per_page: PER_PAGE, page });
      items.push(...data);
      page = nextPage;
    }
    return items;
  }
}

const hasMergeRequests = (project) =>
  project.merge_requests_enabled !== false && project.merge_requests_access_level !== 'disabled';

export async function fetchAuthoredPipelines(client) {
  const pipelines = new Map();
  let after = null;
  do {
    const data = await client.graphql(AUTHORED_PIPELINES_QUERY, { after });
    const connection = data.currentUser.authoredMergeRequests;
    connection.nodes
      .filter((node) => node.headPipeline)
      .forEach(({ iid, project, headPipeline }) => {
        pipelines.set(`${project.fullPath}!${iid}`, {
          id: Number(headPipeline.id.split('/').pop()),
          projectPath: headPipeline.project?.fullPath ?? project.fullPath,
          sha: headPipeline.sha,
          status: headPipeline.status.toLowerCase(),
          webUrl: new URL(headPipeline.path, client.baseUrl).href,
        });
      });
    after = connection.pageInfo.hasNextPage ? connection.pageInfo.endCursor : null;
  } while (after);
  return pipelines;
}

export async function fetchPipelineFailures(client, { projectPath, id, sha }) {
  const projectApiPath = `/projects/${encodeURIComponent(projectPath)}`;
  const pipelinePath = `${projectApiPath}/pipelines/${id}`;
  const [jobs, bridges, statuses] = await Promise.all([
    client.getAll(`${pipelinePath}/jobs`, { 'scope[]': 'failed' }),
    client.getAll(`${pipelinePath}/bridges`, { 'scope[]': 'failed' }),
    sha
      ? client
          .getAll(`${projectApiPath}/repository/commits/${encodeURIComponent(sha)}/statuses`, { pipeline_id: id })
          .catch((error) => (error instanceof GitLabError ? [] : Promise.reject(error)))
      : [],
  ]);
  const isBlocking = (job) => !job.allow_failure;
  const toFailure = (job, overrides) => ({
    id: job.id,
    name: job.name,
    stage: job.stage,
    reason: job.failure_reason,
    webUrl: job.web_url,
    kind: 'job',
    ...overrides,
  });
  const gitlabJobIds = new Set([...jobs, ...bridges].map((job) => job.id));
  return [
    ...jobs.filter(isBlocking).map((job) => toFailure(job)),
    ...bridges
      .filter(isBlocking)
      .map((bridge) =>
        toFailure(bridge, { webUrl: bridge.downstream_pipeline?.web_url ?? bridge.web_url, kind: 'downstream' }),
      ),
    ...statuses
      .filter((status) => status.status === 'failed' && isBlocking(status) && !gitlabJobIds.has(status.id))
      .map((status) =>
        toFailure(status, { reason: status.description, webUrl: status.target_url, kind: 'external' }),
      ),
  ];
}

export async function fetchMaintainedProjects(client) {
  const projects = await client.getAll('/projects', {
    membership: true,
    min_access_level: MAINTAINER_ACCESS_LEVEL,
    archived: false,
    order_by: 'path',
    sort: 'asc',
  });
  return projects.filter(hasMergeRequests);
}

export async function fetchFirstUniqueCommit(client, projectId, fromRef, toRef) {
  const compare = await client.get(`/projects/${projectId}/repository/compare`, {
    from: fromRef,
    to: toRef,
    straight: false,
  });
  const commits = compare?.commits ?? [];
  return commits.length ? commits[0] : null;
}

const userIdOf = (gid) => Number(gid.split('/').pop());

function reviewStatesOf(node) {
  return Object.fromEntries(
    (node.reviewers?.nodes ?? [])
      .filter((reviewer) => reviewer.mergeRequestInteraction?.reviewState)
      .map((reviewer) => [userIdOf(reviewer.id), reviewer.mergeRequestInteraction.reviewState.toLowerCase()]),
  );
}

export async function fetchMergeRequestStates(client, fullPath, iids, { withReviewState = false } = {}) {
  const data = await client.graphql(mergeRequestStatesQuery(withReviewState), { fullPath, iids: iids.map(String) });
  const isComplete = (node) =>
    Array.isArray(node.approvedBy?.nodes) &&
    [node.userNotesCount, node.resolvableDiscussionsCount, node.resolvedDiscussionsCount].every(Number.isInteger);
  return new Map(
    (data.project?.mergeRequests?.nodes ?? []).filter(isComplete).map((node) => [
      Number(node.iid),
      {
        approverIds: node.approvedBy.nodes.map((user) => userIdOf(user.id)),
        approverNames: node.approvedBy.nodes.map((user) => user.name || user.username).filter(Boolean),
        ...(withReviewState && { reviewStates: reviewStatesOf(node) }),
        userNotesCount: node.userNotesCount,
        unresolvedThreads: node.resolvableDiscussionsCount - node.resolvedDiscussionsCount,
        resolvedThreads: node.resolvedDiscussionsCount,
      },
    ]),
  );
}

export async function fetchMergeRequestVerdicts(client, fullPath, iids) {
  const data = await client.graphql(mergeRequestVerdictsQuery, { fullPath, iids: iids.map(String) });
  return new Map(
    (data.project?.mergeRequests?.nodes ?? [])
      .filter((node) => Array.isArray(node.notes?.nodes))
      .map((node) => [Number(node.iid), node.notes.nodes]),
  );
}

const REQUESTED_CHANGES_NOTE = 'requested changes';
const REVIEW_REQUEST_PREFIX = 'requested review from';

export function reviewVerdictOf(notes, me) {
  const myUsername = String(me.username ?? '').toLowerCase();
  const events = [...notes]
    .filter((note) => note.system && note.author?.username)
    .sort((a, b) => (a.createdAt ?? '').localeCompare(b.createdAt ?? ''));
  let verdict = null;
  for (const note of events) {
    const body = (note.body ?? '').trim().toLowerCase();
    if (body === REQUESTED_CHANGES_NOTE && note.author.username.toLowerCase() === myUsername) {
      verdict = 'requested_changes';
    } else if (body.startsWith(REVIEW_REQUEST_PREFIX)) {
      const mentioned = [...body.matchAll(/@([\w.-]*[\w-])/g)].map((match) => match[1].toLowerCase());
      if (mentioned.includes(myUsername)) verdict = null;
    }
  }
  return verdict;
}

export async function fetchDefaultBranchPipelines(client, projects) {
  const variables = Object.fromEntries(
    projects.flatMap((project, index) => [
      [`path${index}`, project.path_with_namespace],
      [`ref${index}`, project.default_branch],
    ]),
  );
  const { data, errors } = await client.graphqlPartial(defaultBranchPipelinesQuery(projects.length), variables);
  const failedAliases = new Set(errors.map((error) => error.path?.[0]));
  return projects.map((_, index) => {
    const alias = `p${index}`;
    if (failedAliases.has(alias) || failedAliases.has(undefined)) {
      return undefined;
    }
    const pipeline = data?.[alias]?.pipelines?.nodes?.find((node) =>
      CONCLUSIVE_PIPELINE_STATUSES.has(node.status.toLowerCase()),
    );
    return pipeline
      ? {
          status: pipeline.status.toLowerCase(),
          webUrl: new URL(pipeline.path, client.baseUrl).href,
          finishedAt: pipeline.finishedAt,
        }
      : null;
  });
}

export async function fetchTokenInfo(client) {
  const token = await client.get('/personal_access_tokens/self');
  return { expiresAt: token.expires_at ?? null, scopes: token.scopes ?? [] };
}

export const fetchApprovals = (client, mr) =>
  client.get(`/projects/${mr.project_id}/merge_requests/${mr.iid}/approvals`);

export const fetchDiscussions = (client, mr) =>
  client.getAll(`/projects/${mr.project_id}/merge_requests/${mr.iid}/discussions`);
