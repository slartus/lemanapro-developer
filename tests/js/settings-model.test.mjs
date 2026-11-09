import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  buildTree,
  groupRadarState,
  matchesQuery,
  mergeProjects,
  projectsUnder,
  settingsPayloadProjects,
} from '../../src/lpdev/web/static/settings-model.js';

const gitlab = [
  { id: 1, path: 'products/lmwork/app', name: 'app', maintainer: true },
  { id: 2, path: 'products/lmwork/bff', name: 'bff', maintainer: false },
  { id: 3, path: 'tools/ci', name: 'ci', maintainer: true },
];

describe('mergeProjects', () => {
  it('marks radar and review selections and keeps configured projects GitLab did not return', () => {
    const projects = mergeProjects(
      gitlab,
      [{ gitlab_project_id: 2, slug: 'products/lmwork/bff', agent: 'a', re_review_on_push: true }, { gitlab_project_id: 9, slug: 'old/x', agent: 'b' }],
      [1, 7],
    );
    const byId = Object.fromEntries(projects.map((project) => [project.id, project]));
    assert.equal(byId[1].radar, true);
    assert.deepEqual(byId[2].review, { agent: 'a', push: true });
    assert.equal(byId[9].known, false);
    assert.equal(byId[9].path, 'old/x');
    assert.equal(byId[7].path, '#7');
    assert.equal(byId[3].review, null);
  });
});

describe('buildTree', () => {
  it('groups projects by namespace and puts unknown ones apart', () => {
    const tree = buildTree(mergeProjects(gitlab, [{ gitlab_project_id: 9, slug: 'old/x', agent: 'b' }], []));
    assert.deepEqual([...tree.groups.keys()], ['Нет в списке GitLab', 'products', 'tools']);
    const lmwork = tree.groups.get('products').groups.get('lmwork');
    assert.equal(lmwork.path, 'products/lmwork');
    assert.deepEqual(lmwork.projects.map((project) => project.id), [1, 2]);
    assert.deepEqual(projectsUnder(tree.groups.get('products')).map((project) => project.id), [1, 2]);
  });
});

describe('groupRadarState', () => {
  it('counts only projects the radar can watch', () => {
    const tree = buildTree(mergeProjects(gitlab, [], [1]));
    const products = tree.groups.get('products');
    assert.equal(groupRadarState(products), 'all');
    assert.equal(groupRadarState(tree.groups.get('tools')), 'none');
    assert.equal(groupRadarState(buildTree(mergeProjects(gitlab, [], [])).groups.get('products')), 'none');
    const both = buildTree(mergeProjects([...gitlab, { id: 4, path: 'products/lmwork/web', name: 'web', maintainer: true }], [], [1]));
    assert.equal(groupRadarState(both.groups.get('products')), 'some');
  });
});

describe('matchesQuery', () => {
  it('searches path and name case-insensitively', () => {
    assert.equal(matchesQuery(gitlab[0], 'LMWORK/A'), true);
    assert.equal(matchesQuery(gitlab[0], ''), true);
    assert.equal(matchesQuery(gitlab[0], 'tools'), false);
  });
});

describe('settingsPayloadProjects', () => {
  it('builds review projects and sorted radar ids', () => {
    const projects = mergeProjects(gitlab, [{ gitlab_project_id: 3, slug: 'tools/ci', agent: '', re_review_on_push: false }], [3, 1]);
    assert.deepEqual(settingsPayloadProjects(projects, 'default-agent'), {
      review: [{ gitlab_project_id: 3, slug: 'tools/ci', agent: 'default-agent', re_review_on_push: false }],
      radarIds: [1, 3],
    });
  });
});

describe('adding a project by link or path', async () => {
  const { addProjectByPath, projectPathFromInput } = await import('../../src/lpdev/web/static/settings-model.js');

  it('extracts the project path from links and plain paths', () => {
    const base = 'https://gitlab.example.com';
    assert.equal(projectPathFromInput(' https://gitlab.example.com/group/sub/app/-/merge_requests/5 ', base), 'group/sub/app');
    assert.equal(projectPathFromInput('group/app.git', base), 'group/app');
    assert.equal(projectPathFromInput('/group/app/', base), 'group/app');
    assert.equal(projectPathFromInput('app', base), null);
    assert.equal(projectPathFromInput('https://evil.example.com/group/app', base), null);
    assert.equal(projectPathFromInput('group/app with space', base), null);
  });

  it('adds an unknown project for auto review and reuses a listed one', () => {
    const projects = mergeProjects(gitlab, [], []);
    const added = addProjectByPath(projects, 'other/team/app', 'agent-x');
    const fresh = added.find((project) => project.key === 'new:other/team/app');
    assert.deepEqual(fresh.review, { agent: 'agent-x', push: false });
    assert.equal(fresh.id, null);
    const reused = addProjectByPath(projects, 'TOOLS/ci', 'agent-x');
    assert.equal(reused.length, projects.length);
    assert.deepEqual(reused.find((project) => project.id === 3).review, { agent: 'agent-x', push: false });
    assert.deepEqual(settingsPayloadProjects(added, 'd').review, [
      { gitlab_project_id: null, slug: 'other/team/app', agent: 'agent-x', re_review_on_push: false },
    ]);
  });
});
