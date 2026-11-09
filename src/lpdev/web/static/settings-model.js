export function mergeProjects(gitlabProjects, reviewProjects, radarIds) {
  const byId = new Map(
    gitlabProjects.map((project) => [project.id, { id: project.id, path: project.path, name: project.name, maintainer: project.maintainer, known: true }]),
  );
  reviewProjects.forEach((project) => {
    if (!byId.has(project.gitlab_project_id)) {
      byId.set(project.gitlab_project_id, {
        id: project.gitlab_project_id,
        path: project.slug || `#${project.gitlab_project_id}`,
        name: project.slug || `#${project.gitlab_project_id}`,
        maintainer: true,
        known: false,
      });
    }
  });
  radarIds.forEach((id) => {
    if (!byId.has(id)) byId.set(id, { id, path: `#${id}`, name: `#${id}`, maintainer: true, known: false });
  });
  const review = new Map(reviewProjects.map((project) => [project.gitlab_project_id, project]));
  const radar = new Set(radarIds);
  return [...byId.values()]
    .map((project) => ({
      ...project,
      radar: radar.has(project.id),
      review: review.has(project.id)
        ? { agent: review.get(project.id).agent, push: Boolean(review.get(project.id).re_review_on_push) }
        : null,
    }))
    .sort((a, b) => a.path.localeCompare(b.path));
}

export function buildTree(projects) {
  const root = { path: '', name: '', groups: new Map(), projects: [] };
  projects.forEach((project) => {
    const parts = project.known ? project.path.split('/').slice(0, -1) : ['Нет в списке GitLab'];
    let node = root;
    parts.forEach((part) => {
      if (!node.groups.has(part)) {
        node.groups.set(part, { path: node.path ? `${node.path}/${part}` : part, name: part, groups: new Map(), projects: [] });
      }
      node = node.groups.get(part);
    });
    node.projects.push(project);
  });
  return root;
}

export function projectsUnder(node) {
  return [...node.projects, ...[...node.groups.values()].flatMap(projectsUnder)];
}

export function groupRadarState(node) {
  const eligible = projectsUnder(node).filter((project) => project.maintainer);
  const checked = eligible.filter((project) => project.radar).length;
  if (!eligible.length || checked === 0) return 'none';
  return checked === eligible.length ? 'all' : 'some';
}

export function matchesQuery(project, query) {
  const needle = query.trim().toLowerCase();
  return !needle || project.path.toLowerCase().includes(needle) || project.name.toLowerCase().includes(needle);
}

export function settingsPayloadProjects(projects, defaultAgent) {
  return {
    review: projects
      .filter((project) => project.review)
      .map((project) => ({
        gitlab_project_id: project.id,
        slug: project.known || !project.path.startsWith('#') ? project.path : '',
        agent: project.review.agent || defaultAgent,
        re_review_on_push: project.review.push,
      })),
    radarIds: projects.filter((project) => project.radar).map((project) => project.id).sort((a, b) => a - b),
  };
}
