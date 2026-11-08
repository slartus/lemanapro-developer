from __future__ import annotations

from pathlib import Path

import yaml

from mrwatch.config import AGENT_NAME_RE

GLOBAL_AGENT_DIRS = (
    Path("~/.config/opencode/agent"),
    Path("~/.config/opencode/agents"),
)
REQUIRED_DENIED_TOOLS = ("edit", "bash", "webfetch")


class AgentError(Exception):
    pass


def _agent_dirs(agents_dirs: tuple[Path, ...] | None) -> list[Path]:
    return [d.expanduser() for d in (agents_dirs or GLOBAL_AGENT_DIRS)]


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise AgentError(f"agent {path.stem}: frontmatter not found")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise AgentError(f"agent {path.stem}: frontmatter not closed")
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        raise AgentError(f"agent {path.stem}: invalid frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentError(f"agent {path.stem}: frontmatter must be a mapping")
    return data


def _denied(frontmatter: dict, tool: str) -> bool:
    tools = frontmatter.get("tools")
    if isinstance(tools, dict) and tools.get(tool) is False:
        return True
    permission = frontmatter.get("permission")
    if isinstance(permission, dict):
        value = permission.get(tool)
        if value == "deny":
            return True
        if isinstance(value, dict) and value and all(v == "deny" for v in value.values()):
            return True
    return False


def resolve_agent_file(name: str, agents_dirs: tuple[Path, ...] | None = None) -> Path:
    if not AGENT_NAME_RE.fullmatch(name):
        raise AgentError(f"invalid agent name: {name!r}")
    for directory in _agent_dirs(agents_dirs):
        candidate = directory / f"{name}.md"
        if candidate.is_file():
            return candidate
    raise AgentError(f"agent '{name}' not found in global opencode agents directory")


def validate_agent(name: str, agents_dirs: tuple[Path, ...] | None = None) -> None:
    path = resolve_agent_file(name, agents_dirs)
    try:
        frontmatter = _frontmatter(path)
    except OSError as exc:
        raise AgentError(f"agent '{name}' unreadable: {exc}") from exc
    missing = [tool for tool in REQUIRED_DENIED_TOOLS if not _denied(frontmatter, tool)]
    if missing:
        raise AgentError(
            f"agent '{name}' is not read-only: {', '.join(missing)} must be disabled "
            "(tools: false or permission: deny)"
        )


def list_readonly_agents(agents_dirs: tuple[Path, ...] | None = None) -> list[str]:
    names: set[str] = set()
    for directory in _agent_dirs(agents_dirs):
        for path in directory.glob("*.md"):
            try:
                validate_agent(path.stem, agents_dirs)
            except (AgentError, OSError):
                continue
            names.add(path.stem)
    return sorted(names)
