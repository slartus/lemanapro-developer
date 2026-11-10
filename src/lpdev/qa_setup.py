"""Interactive setup wizard for the QA timesheet build.

Asks for everything a QA tester needs, verifies both tokens against live
services and stores them in the macOS Keychain (service ``lpdev-qa``).
The generated config keeps only token *commands* — secrets never touch
config.yaml, logs or the database.
"""

from __future__ import annotations

import getpass
import re
import subprocess
import sys
from pathlib import Path

import httpx
import yaml

from lpdev.config import TRACKER_QUEUE_RE, normalize_https_url

QA_KEYCHAIN_SERVICE = "lpdev-qa"
QA_CONFIG_PATH = Path("~/.config/lpdev-qa/config.yaml")
QA_DATA_DIR = Path("~/lemanapro-timesheet")
QA_DEFAULT_PORT = 8124
QA_DEFAULT_GITLAB_URL = "https://gitlab.lmru.tech"
QA_DEFAULT_QUEUES = "SUPERAPP"

GITLAB_PAT_PREFIX = "glpat-"
TRACKER_TOKEN_PREFIX = "y0_"

_SECURITY_BIN = "/usr/bin/security"
_ORG_ID_RE = re.compile(r"[A-Za-z0-9_-]+")

VERIFY_TIMEOUT_SEC = 15


class WizardError(Exception):
    pass


def validate_gitlab_pat(value: str) -> str:
    token = value.strip()
    if not token.startswith(GITLAB_PAT_PREFIX):
        raise ValueError(f"GitLab PAT должен начинаться с {GITLAB_PAT_PREFIX}")
    if any(c.isspace() for c in token):
        raise ValueError("токен не должен содержать пробелов и переводов строк")
    return token


def validate_tracker_token(value: str) -> str:
    token = value.strip()
    if not token.startswith(TRACKER_TOKEN_PREFIX):
        raise ValueError(f"OAuth-токен Трекера должен начинаться с {TRACKER_TOKEN_PREFIX}")
    if any(c.isspace() for c in token):
        raise ValueError("токен не должен содержать пробелов и переводов строк")
    return token


def validate_org_id(value: str) -> str:
    org_id = value.strip()
    if not _ORG_ID_RE.fullmatch(org_id):
        raise ValueError("org_id — только латинские буквы, цифры, '_' и '-'")
    return org_id


def parse_queues(value: str) -> list[str]:
    queues = [part.strip().upper() for part in re.split(r"[,\s]+", value.strip()) if part.strip()]
    if not queues:
        raise ValueError("укажите хотя бы одну очередь")
    for queue in queues:
        if not TRACKER_QUEUE_RE.fullmatch(queue):
            raise ValueError(f"неверный ключ очереди: {queue!r} (латиница, цифры и '_', с заглавной)")
    return list(dict.fromkeys(queues))


def validate_port(value: str) -> int:
    try:
        port = int(value.strip())
    except ValueError:
        raise ValueError("порт — число") from None
    if not 1 <= port <= 65535:
        raise ValueError("порт должен быть в диапазоне 1–65535")
    return port


def keychain_add(account: str, secret: str) -> None:
    subprocess.run(
        [_SECURITY_BIN, "add-generic-password", "-U", "-s", QA_KEYCHAIN_SERVICE, "-a", account, "-j", "lpdev QA", "-w", secret],
        check=True,
        capture_output=True,
        timeout=VERIFY_TIMEOUT_SEC,
    )


def keychain_read_command(account: str) -> str:
    return f"{_SECURITY_BIN} find-generic-password -s {QA_KEYCHAIN_SERVICE} -a {account} -w"


def verify_gitlab(url: str, pat: str) -> str:
    try:
        response = httpx.get(
            f"{url}/api/v4/user",
            headers={"PRIVATE-TOKEN": pat},
            timeout=VERIFY_TIMEOUT_SEC,
        )
    except httpx.HTTPError as exc:
        raise WizardError(f"GitLab недоступен: {exc}") from exc
    if response.status_code != 200:
        raise WizardError(f"GitLab отклонил PAT: HTTP {response.status_code} (проверьте scope read_api и срок действия)")
    login = response.json().get("username", "?")
    return str(login)


def verify_tracker(org_id: str, token: str, org_header: str = "X-Org-Id") -> str:
    try:
        response = httpx.get(
            "https://api.tracker.yandex.net/v3/myself",
            headers={"Authorization": f"OAuth {token}", org_header: org_id},
            timeout=VERIFY_TIMEOUT_SEC,
        )
    except httpx.HTTPError as exc:
        raise WizardError(f"Трекер недоступен: {exc}") from exc
    if response.status_code != 200:
        raise WizardError(
            f"Трекер отклонил токен: HTTP {response.status_code} (проверьте org_id, токен и права tracker:read)"
        )
    return str(response.json().get("display", "?"))


def build_config(gitlab_url: str, submit_enabled: bool, org_id: str, queues: list[str]) -> str:
    config = {
        "gitlab": {"url": gitlab_url, "pat_command": keychain_read_command("gitlab")},
        "tracker": {
            "enabled": True,
            "submit_enabled": submit_enabled,
            "org_id": org_id,
            "org_header": "X-Org-Id",
            "token_command": keychain_read_command("tracker"),
            "queues": queues,
            "role": "qa",
        },
        "paths": {
            "db": str(QA_DATA_DIR / "lpdev.db"),
            "workspaces": str(QA_DATA_DIR / "workspaces"),
            "logs": str(QA_DATA_DIR / "logs"),
        },
    }
    return yaml.safe_dump(config, sort_keys=False, allow_unicode=True)


def write_port(port: int, config_path: Path = QA_CONFIG_PATH) -> Path:
    port_path = config_path.parent / "port"
    port_path.write_text(f"{port}\n", encoding="utf-8")
    port_path.chmod(0o600)
    return port_path


def read_port(config_path: Path = QA_CONFIG_PATH) -> int:
    port_path = config_path.parent / "port"
    try:
        return validate_port(port_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return QA_DEFAULT_PORT


class Wizard:
    def __init__(
        self,
        input_fn=input,
        prompt_fn=getpass.getpass,
        verify_gitlab_fn=verify_gitlab,
        verify_tracker_fn=verify_tracker,
        keychain_add_fn=keychain_add,
        config_path: Path = QA_CONFIG_PATH,
        print_fn=print,
    ) -> None:
        self.input_fn = input_fn
        self.prompt_fn = prompt_fn
        self.verify_gitlab_fn = verify_gitlab_fn
        self.verify_tracker_fn = verify_tracker_fn
        self.keychain_add_fn = keychain_add_fn
        self.config_path = config_path
        self.print_fn = print_fn

    def ask(self, label: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        value = self.input_fn(f"{label}{suffix}: ").strip()
        return value or default

    def ask_secret(self, label: str, validator) -> str:
        while True:
            try:
                return validator(self.prompt_fn(f"{label}: "))
            except ValueError as exc:
                self.print_fn(f"  ! {exc}")
                self.print_fn("  попробуйте ещё раз (Ctrl+C — отмена)")

    def ask_valid(self, label: str, default: str, parser) -> object:
        while True:
            try:
                return parser(self.ask(label, default))
            except ValueError as exc:
                self.print_fn(f"  ! {exc}")

    def run(self) -> Path:
        self.print_fn("=== Настройка таймшита (role: qa) ===")
        self.print_fn("Токены сохраняются в Keychain (service lpdev-qa), в конфиг они не пишутся.")
        self.print_fn("Ctrl+C — отмена.\n")

        gitlab_url = self.ask_valid("GitLab URL", QA_DEFAULT_GITLAB_URL, normalize_https_url)
        pat = self.ask_secret("GitLab PAT (scope read_api)", validate_gitlab_pat)
        self.print_fn("  проверяю PAT...")
        login = self.verify_gitlab_fn(gitlab_url, pat)
        self.print_fn(f"  GitLab: {login}")

        self.keychain_add_fn("gitlab", pat)

        org_id = self.ask_valid("Tracker org_id (ID организации)", "", validate_org_id)
        queues = self.ask_valid("Очереди через запятую", QA_DEFAULT_QUEUES, parse_queues)
        submit = self.ask("Списывать время из интерфейса? (y/n)", "y").lower() in ("y", "yes", "д", "да")

        tracker_token = self.ask_secret(
            "OAuth-токен Трекера (tracker:read" + (", tracker:write" if submit else "") + ")",
            validate_tracker_token,
        )
        self.print_fn("  проверяю токен...")
        display = self.verify_tracker_fn(org_id, tracker_token)
        self.print_fn(f"  Трекер: {display}")

        self.keychain_add_fn("tracker", tracker_token)

        port = self.ask_valid("Порт", str(QA_DEFAULT_PORT), validate_port)

        self.config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config_path.write_text(build_config(gitlab_url, submit, org_id, queues), encoding="utf-8")
        self.config_path.chmod(0o600)
        write_port(port, self.config_path)
        self.print_fn(f"\nКонфиг записан: {self.config_path}")
        self.print_fn("Данные будут лежать в ~/lemanapro-timesheet/")
        return self.config_path


def run_wizard_if_needed(force: bool = False) -> Path:
    if not force and QA_CONFIG_PATH.is_file():
        return QA_CONFIG_PATH
    if not sys.stdin.isatty():
        raise WizardError(
            f"конфиг не найден: {QA_CONFIG_PATH}\n"
            "Запустите setup в Terminal для первичной настройки (или заново с --setup)."
        )
    return Wizard().run()
