export function mergeProjects(gitlabProjects, reviewProjects, radarIds) {
  const byKey = new Map(
    gitlabProjects.map((project) => [
      String(project.id),
      { id: project.id, key: String(project.id), path: project.path, name: project.name, maintainer: project.maintainer, known: true },
    ]),
  );
  const keyByPath = new Map([...byKey.values()].map((project) => [project.path.toLowerCase(), project.key]));
  const reviewByKey = new Map();
  reviewProjects.forEach((project) => {
    const id = project.gitlab_project_id;
    const slug = project.slug || (id === null || id === undefined ? '' : `#${id}`);
    const key = id === null || id === undefined ? keyByPath.get(slug.toLowerCase()) ?? `new:${slug}` : String(id);
    if (!byKey.has(key)) {
      byKey.set(key, { id: id ?? null, key, path: slug, name: slug, maintainer: id !== null && id !== undefined, known: false });
    }
    reviewByKey.set(key, project);
  });
  radarIds.forEach((id) => {
    const key = String(id);
    if (!byKey.has(key)) byKey.set(key, { id, key, path: `#${id}`, name: `#${id}`, maintainer: true, known: false });
  });
  const radar = new Set(radarIds.map(String));
  return [...byKey.values()]
    .map((project) => ({
      ...project,
      radar: radar.has(project.key),
      review: reviewByKey.has(project.key)
        ? { agent: reviewByKey.get(project.key).agent }
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

export function projectPathFromInput(input, gitlabUrl) {
  let value = input.trim();
  if (gitlabUrl && value.toLowerCase().startsWith(`${gitlabUrl.toLowerCase()}/`)) value = value.slice(gitlabUrl.length + 1);
  value = value.split(/[?#]/)[0].split('/-/')[0].replace(/\.git$/, '').replace(/^\/+|\/+$/g, '');
  return /^[A-Za-z0-9_.-]+(\/[A-Za-z0-9_.-]+)+$/.test(value) ? value : null;
}

export function addProjectByPath(projects, path, agent) {
  const existing = projects.find((project) => project.path.toLowerCase() === path.toLowerCase());
  if (existing) {
    return projects.map((project) => (project === existing && !project.review ? { ...project, review: { agent } } : project));
  }
  const added = { id: null, key: `new:${path}`, path, name: path, maintainer: false, known: false, radar: false, review: { agent } };
  return [...projects, added].sort((a, b) => a.path.localeCompare(b.path));
}

export function settingsPayloadProjects(projects, defaultAgent) {
  return {
    review: projects
      .filter((project) => project.review)
      .map((project) => ({
        gitlab_project_id: project.id,
        slug: project.path.startsWith('#') ? '' : project.path,
        agent: project.review.agent || defaultAgent,
      })),
    radarIds: projects.filter((project) => project.radar && project.id !== null).map((project) => project.id).sort((a, b) => a - b),
  };
}
