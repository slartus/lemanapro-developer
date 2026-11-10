import pytest
import yaml

from lpdev.config import load_config
from lpdev.qa_setup import (
    QA_CONFIG_PATH,
    QA_DATA_DIR,
    Wizard,
    WizardError,
    build_config,
    keychain_add,
    keychain_read_command,
    parse_queues,
    read_port,
    run_wizard_if_needed,
    validate_gitlab_pat,
    validate_org_id,
    validate_port,
    validate_tracker_token,
    write_port,
)


def test_validate_gitlab_pat_ok():
    assert validate_gitlab_pat("  glpat-abc123\n") == "glpat-abc123"


def test_validate_gitlab_pat_rejects_wrong_prefix_and_spaces():
    with pytest.raises(ValueError, match="glpat-"):
        validate_gitlab_pat("y0_token")
    with pytest.raises(ValueError, match="пробел"):
        validate_gitlab_pat("glpat-abc def")


def test_validate_tracker_token_ok():
    assert validate_tracker_token(" y0_AgAAA\n") == "y0_AgAAA"


def test_validate_tracker_token_rejects_wrong_prefix():
    with pytest.raises(ValueError, match="y0_"):
        validate_tracker_token("glpat-abc")


def test_validate_org_id():
    assert validate_org_id(" 123-abc_456 ") == "123-abc_456"
    with pytest.raises(ValueError):
        validate_org_id("орg")
    with pytest.raises(ValueError):
        validate_org_id("")


def test_parse_queues():
    assert parse_queues("superapp, TEST_Q  Q3") == ["SUPERAPP", "TEST_Q", "Q3"]
    with pytest.raises(ValueError):
        parse_queues("   ")
    with pytest.raises(ValueError, match="очереди"):
        parse_queues("1q")
    with pytest.raises(ValueError, match="очереди"):
        parse_queues("q-low")


def test_validate_port():
    assert validate_port(" 8124 ") == 8124
    with pytest.raises(ValueError):
        validate_port("abc")
    with pytest.raises(ValueError):
        validate_port("70000")


def test_build_config_passes_load_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(build_config("https://gitlab.lmru.tech", True, "123", ["SUPERAPP"]), encoding="utf-8")
    config_path.chmod(0o600)
    config = load_config(config_path, timesheet_only=True)
    assert config.gitlab.url == "https://gitlab.lmru.tech"
    assert config.gitlab.pat_command == "/usr/bin/security find-generic-password -s lpdev-qa -a gitlab -w"
    assert config.tracker.enabled is True
    assert config.tracker.submit_enabled is True
    assert config.tracker.role == "qa"
    assert config.tracker.in_progress_statuses == ["testing"]
    assert config.tracker.activity_statuses == {"testing": "t"}
    assert config.tracker.queues == ["SUPERAPP"]
    assert config.tracker.org_id == "123"
    assert config.tracker.org_header == "X-Org-Id"
    assert config.paths.db == str(QA_DATA_DIR / "lpdev.db")
    assert config.projects == []


def test_keychain_read_command():
    assert keychain_read_command("gitlab") == "/usr/bin/security find-generic-password -s lpdev-qa -a gitlab -w"


def test_keychain_add_runs_security(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)

        class Proc:
            returncode = 0

        return Proc()

    monkeypatch.setattr("lpdev.qa_setup.subprocess.run", fake_run)
    keychain_add("tracker", "y0_secret")
    assert calls == [
        [
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-s",
            "lpdev-qa",
            "-a",
            "tracker",
            "-j",
            "lpdev QA",
            "-w",
            "y0_secret",
        ]
    ]


def test_keychain_add_raises_on_error(monkeypatch):
    def fake_run(args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr("lpdev.qa_setup.subprocess.run", fake_run)
    with pytest.raises(OSError):
        keychain_add("gitlab", "glpat-x")


def test_port_sidecar_roundtrip(tmp_path):
    config_path = tmp_path / "config.yaml"
    assert read_port(config_path) == 8124
    write_port(9999, config_path)
    assert read_port(config_path) == 9999
    config_path.parent.joinpath("port").write_text("not-a-port\n", encoding="utf-8")
    assert read_port(config_path) == 8124


def _wizard_with(answers, prompts, gitlab_verify, tracker_verify, tmp_path, keychain_calls):
    return Wizard(
        input_fn=lambda label="": answers.pop(0),
        prompt_fn=lambda label="": prompts.pop(0),
        verify_gitlab_fn=lambda url, pat: gitlab_verify.pop(0),
        verify_tracker_fn=lambda org, token: tracker_verify.pop(0),
        keychain_add_fn=lambda account, secret: keychain_calls.append((account, secret)),
        config_path=tmp_path / "config.yaml",
        print_fn=lambda *a, **k: None,
    )


def test_wizard_writes_config_and_keychain(tmp_path):
    answers = ["", "", "", "y", "8124"]
    prompts = ["glpat-abc", "y0_tok"]
    keychain_calls = []
    wizard = _wizard_with(answers, prompts, ["qa-user"], ["QA User"], tmp_path, keychain_calls)
    path = wizard.run()
    assert path == tmp_path / "config.yaml"
    assert path.stat().st_mode & 0o777 == 0o600
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["gitlab"]["url"] == "https://gitlab.lmru.tech"
    assert raw["tracker"]["org_id"] == "7867633"
    assert raw["tracker"]["submit_enabled"] is True
    assert raw["tracker"]["role"] == "qa"
    assert "secret" not in path.read_text(encoding="utf-8")
    assert raw["gitlab"]["pat_command"].endswith("-a gitlab -w")
    assert keychain_calls == [("gitlab", "glpat-abc"), ("tracker", "y0_tok")]
    assert (tmp_path / "port").read_text().strip() == "8124"


def test_wizard_reasks_on_bad_input_and_no_submit(tmp_path):
    answers = ["", "123", "", "n", "8125"]
    prompts = ["bad-token", "glpat-abc", "bad2", "y0_tok"]
    keychain_calls = []
    wizard = _wizard_with(answers, prompts, ["qa-user"], ["QA User"], tmp_path, keychain_calls)
    wizard.run()
    raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert raw["tracker"]["submit_enabled"] is False
    assert keychain_calls == [("gitlab", "glpat-abc"), ("tracker", "y0_tok")]


def test_wizard_reasks_on_invalid_queue(tmp_path):
    answers = ["", "123", "1q", "SUPERAPP", "n", "8124"]
    prompts = ["glpat-abc", "y0_tok"]
    keychain_calls = []
    wizard = _wizard_with(answers, prompts, ["qa-user"], ["QA User"], tmp_path, keychain_calls)
    wizard.run()
    raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert raw["tracker"]["queues"] == ["SUPERAPP"]


def test_wizard_reasks_on_verify_failure(tmp_path):
    answers = ["", "123", "", "y", "8124"]
    prompts = ["glpat-abc", "glpat-abc", "y0_tok"]
    gitlab_verify = [WizardError("PAT отклонён"), "qa-user"]
    keychain_calls = []
    wizard = _wizard_with(answers, prompts, gitlab_verify, ["QA User"], tmp_path, keychain_calls)
    wizard.run()
    assert keychain_calls == [("gitlab", "glpat-abc"), ("tracker", "y0_tok")]


def test_run_wizard_if_needed_skips_existing_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("gitlab:\n  url: https://gitlab.lmru.tech\n", encoding="utf-8")
    called = []

    def fail_run(self):
        called.append(1)
        raise AssertionError("wizard must not run")

    monkeypatch.setattr(Wizard, "run", fail_run)
    assert run_wizard_if_needed(config_path=config_path) == config_path
    assert called == []


def test_run_wizard_if_needed_requires_tty(tmp_path, monkeypatch):
    config_path = tmp_path / "missing.yaml"
    monkeypatch.setattr("sys.stdin", type("Stdin", (), {"isatty": lambda self: False})())
    with pytest.raises(Exception, match="Terminal"):
        run_wizard_if_needed(config_path=config_path)


def test_wizard_config_is_loadable_and_qa_default_paths_used(tmp_path):
    answers = ["", "123", "", "y", "8124"]
    prompts = ["glpat-abc", "y0_tok"]
    wizard = _wizard_with(answers, prompts, ["qa-user"], ["QA User"], tmp_path, [])
    path = wizard.run()
    path.chmod(0o600)
    config = load_config(path, timesheet_only=True)
    assert config.tracker.role == "qa"
    assert config.tracker.token_command == keychain_read_command("tracker")


def test_qa_config_path_default():
    assert "~" in str(QA_CONFIG_PATH)
    assert QA_CONFIG_PATH.expanduser().is_absolute()
