import { GitLabError } from '../src/gitlab.js';

export const ME = { id: 1, username: 'me' };
export const ALICE = { id: 2, username: 'alice' };
export const BOB = { id: 3, username: 'bob' };

export function mr({ projectId = 10, iid, author = ALICE, updatedAt = '2026-01-01T00:00:00Z', ...rest }) {
  return {
    project_id: projectId,
    iid,
    title: `MR ${iid}`,
    web_url: `https://gitlab.example.com/group/project/-/merge_requests/${iid}`,
    references: { full: `group/project!${iid}` },
    author,
    draft: false,
    detailed_merge_status: 'mergeable',
    has_conflicts: false,
    updated_at: updatedAt,
    user_notes_count: 0,
    ...rest,
  };
}

export function note(author, { system = false, resolvable = true, resolved = false } = {}) {
  return { author, system, resolvable, resolved };
}

export class FakeClient {
  constructor(routes, { graphql = null } = {}) {
    this.routes = routes;
    this.graphqlHandler = graphql;
    this.baseUrl = 'https://gitlab.example.com';
    this.calls = [];
  }

  resolve(path, params) {
    this.calls.push({ path, params });
    const handler = this.routes[path];
    if (handler === undefined) {
      throw new GitLabError(404, `404 Not Found: ${path}`, path);
    }
    if (handler instanceof GitLabError) {
      throw handler;
    }
    return typeof handler === 'function' ? handler(params) : handler;
  }

  async get(path, params) {
    return this.resolve(path, params);
  }

  async getAll(path, params) {
    return this.resolve(path, params);
  }

  async graphql(query, variables) {
    const { data, errors } = await this.graphqlPartial(query, variables);
    if (errors.length) {
      throw new GitLabError(200, `GraphQL: ${errors[0].message}`, '/graphql');
    }
    return data;
  }

  async graphqlPartial(query, variables) {
    if (!this.graphqlHandler) {
      throw new GitLabError(404, 'GraphQL disabled', '/graphql');
    }
    const result = await this.graphqlHandler(query, variables);
    return result?.[PARTIAL] ? { data: result.data, errors: result.errors } : { data: result, errors: [] };
  }
}

const PARTIAL = Symbol('partial');

export const partialGraphql = (data, errors) => ({ [PARTIAL]: true, data, errors });

export const serverError = (path) => new GitLabError(500, `500 Internal Server Error: ${path}`, path);

export function mergeRequestsRoute({ authored = [], reviewing = [], assigned = [] }) {
  return (params) => {
    if (params.scope === 'created_by_me') return authored;
    if (params.scope === 'assigned_to_me') return assigned;
    return reviewing;
  };
}

export function statesGraphql(statesByPath) {
  return (query, variables) => {
    if (!query.includes('MergeRequestStates')) {
      throw new GitLabError(404, 'Unexpected GraphQL query', '/graphql');
    }
    const states = statesByPath[variables.fullPath] ?? {};
    return {
      project: {
        mergeRequests: {
          nodes: variables.iids
            .filter((iid) => states[iid])
            .map((iid) => {
              const { approvers = [], notes = 0, resolvable = 0, resolved = 0, reviewStates = {} } = states[iid];
              return {
                iid,
                userNotesCount: notes,
                resolvableDiscussionsCount: resolvable,
                resolvedDiscussionsCount: resolved,
                approvedBy: { nodes: approvers.map((user) => ({ id: `gid://gitlab/User/${user.id}` })) },
                ...(query.includes('reviewState') && {
                  reviewers: {
                    nodes: Object.entries(reviewStates).map(([id, reviewState]) => ({
                      id: `gid://gitlab/User/${id}`,
                      mergeRequestInteraction: { reviewState },
                    })),
                  },
                }),
              };
            }),
        },
      },
    };
  };
}

export function verdictsGraphql(notesByPath) {
  return (query, variables) => {
    if (!query.includes('MergeRequestVerdicts')) {
      throw new GitLabError(404, 'Unexpected GraphQL query', '/graphql');
    }
    const notes = notesByPath[variables.fullPath] ?? {};
    return {
      project: {
        mergeRequests: {
          nodes: variables.iids
            .filter((iid) => notes[iid])
            .map((iid) => ({ iid, notes: { nodes: notes[iid] } })),
        },
      },
    };
  };
}

export function activityNote(author, body, createdAt) {
  return { author, system: true, body, createdAt };
}

export const withVerdicts = (statesHandler, verdictsHandler) => (query, variables) =>
  query.includes('MergeRequestVerdicts') ? verdictsHandler(query, variables) : statesHandler(query, variables);

export function emptyDashboardRoutes() {
  return {
    '/user': ME,
    '/projects': [],
    '/merge_requests': () => [],
    '/todos': [],
  };
}
