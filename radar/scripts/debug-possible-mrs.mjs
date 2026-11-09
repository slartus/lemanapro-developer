#!/usr/bin/env node
// Диагностика секции «Возможные MR» против живого GitLab.
// Запуск: GITLAB_URL=https://gitlab.lmru.tech GITLAB_TOKEN=<token> node scripts/debug-possible-mrs.mjs
// [опционально] PROJECT_PATH=products/lmwork/lmru-mobile_lmwork BRANCH=task/multi-scanner-mode

import { GitLabClient, fetchMaintainedProjects } from '../src/gitlab.js';
import { fetchRaw } from '../src/dashboard.js';

const baseUrl = process.env.GITLAB_URL;
const token = process.env.GITLAB_TOKEN;
const projectPath = process.env.PROJECT_PATH ?? 'products/lmwork/lmru-mobile_lmwork';
const branchName = process.env.BRANCH ?? 'task/multi-scanner-mode';

if (!baseUrl || !token) {
  console.error('Нужны переменные окружения GITLAB_URL и GITLAB_TOKEN');
  process.exit(1);
}

const step = (title) => console.log(`\n=== ${title} ===`);
const show = (label, value) => console.log(`${label}: ${JSON.stringify(value)}`);

const client = new GitLabClient(baseUrl, token);

step('1. /user');
const me = await client.get('/user');
show('name', me.name);
show('username', me.username);
show('email', me.email);
show('public_email', me.public_email);
show('commit_email', me.commit_email);
console.log(`использовано запросов: ${client.requestCount}`);

step(`2. Список проектов (min_access_level=40), поиск «${projectPath}»`);
const maintained = await fetchMaintainedProjects(client);
console.log(`всего проектов: ${maintained.length}`);
const project = maintained.find((p) => p.path_with_namespace === projectPath);
if (!project) {
  const similar = maintained.filter((p) => p.path_with_namespace.includes('lmru-mobile'));
  console.log(`ПРОЕКТ НЕ НАЙДЕН В СПИСКЕ! Похожие пути: ${similar.map((p) => p.path_with_namespace).join(', ') || '—'}`);
  process.exit(2);
}
show('project.id', project.id);
show('default_branch', project.default_branch);

step(`3. Ветки проекта, поиск «${branchName}»`);
const branches = await client.getAll(`/projects/${project.id}/repository/branches`);
console.log(`всего веток: ${branches.length}`);
const branch = branches.find((b) => b.name === branchName);
if (!branch) {
  console.log('ВЕТКА НЕ НАЙДЕНА В СПИСКЕ ВЕТОК!');
  process.exit(3);
}
show('branch.commit', {
  id: branch.commit?.id,
  author_name: branch.commit?.author_name,
  author_email: branch.commit?.author_email,
  committer_name: branch.commit?.committer_name,
  committer_email: branch.commit?.committer_email,
  committed_date: branch.commit?.committed_date,
});
show('branch.merged', branch.merged);

step('4. compare (первый уникальный коммит)');
const compare = await client.get(`/projects/${project.id}/repository/compare`, {
  from: project.default_branch,
  to: branchName,
  straight: false,
});
console.log(`уникальных коммитов: ${compare.commits?.length ?? 'нет поля commits'}`);
const oldest = compare.commits?.[0];
show('oldest', oldest && { id: oldest.id, author_name: oldest.author_name, author_email: oldest.author_email, committer_name: oldest.committer_name });

step('5. Открытые MR проекта (коллизии source_branch)');
const openMrs = await client.getAll(`/projects/${project.id}/merge_requests`, { state: 'opened' });
console.log(`открытых MR: ${openMrs.length}`);
console.log(`коллизия по ветке: ${openMrs.some((mr) => mr.source_branch === branchName)}`);

step('6. fetchRaw целиком (только possibleMrs, выбранный проект)');
const raw = await fetchRaw(client, {
  selectedProjectIds: [project.id],
  skipSections: ['reviewing', 'assigned', 'mentioned', 'awaitingApproval', 'threads', 'authored', 'brokenBranches'],
});
show('watchedProjects', raw.watchedProjects);
show('possibleMrsUnavailable', raw.possibleMrsUnavailable);
show('possibleMrs', raw.possibleMrs.map((item) => ({ title: item.title, projectId: item.projectId })));
console.log(`\nвсего запросов к GitLab: ${client.requestCount}`);
