import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib

import pytest

from wendell_ci.cli import (
    _agent_command_args,
    _agent_adapter_template,
    _apply_overrides,
    _call_remote_runtime_agent,
    _load_env_files,
    _run_suite_with_remote_upload,
    _trace_payload,
    build_parser,
    main,
)
from wendell_ci.config import RunnerConfig
from wendell_ci.models import ScenarioResult, SuiteResult


try:
    WORLD_SIM_AVAILABLE = importlib.util.find_spec("worldsim.playbook_source_extractor") is not None
except ModuleNotFoundError:
    WORLD_SIM_AVAILABLE = False
requires_worldsim_compiler = pytest.mark.skipif(
    not WORLD_SIM_AVAILABLE,
    reason="legacy local worldsim compiler is not included in the public CLI repository",
)


def test_cli_prints_installed_version(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert output.split()[0]


def test_cli_overrides_world_and_pack_from_config() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "--world",
            "commerce-support-ops",
            "--world-version",
            "2",
            "--scenario-pack",
            "access-operations-benchmark",
            "--scenario-pack-version",
            "1",
            "--agent",
            "local-agent",
            "--agent-command",
            "python agent.py",
        ]
    )
    config = RunnerConfig(project="customer-service-agent", world="old-world", scenario_pack="old-pack")

    resolved = _apply_overrides(config, args)

    assert resolved.world == "commerce-support-ops"
    assert resolved.world_version == "2"
    assert resolved.scenario_pack == "access-operations-benchmark"
    assert resolved.scenario_pack_version == "1"
    assert resolved.agent == "local-agent"
    assert resolved.agent_command == "python agent.py"
    assert resolved.project == "customer-service-agent"


def test_cli_test_requires_config_file(tmp_path: Path, capsys) -> None:
    exit_code = main(["test", "--config", str(tmp_path / "missing.toml")])

    assert exit_code == 2
    assert "config file" in capsys.readouterr().err


def test_cli_test_reports_invalid_config_without_traceback(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "wendell.toml"
    config_path.write_text(
        "\n".join(
            [
                'project = "agent"',
                "[gates]",
                "suite_minimum_score = 0.90",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(["test", "--config", str(config_path)])

    assert exit_code == 2
    error = capsys.readouterr().err
    assert "unknown key" in error
    assert "Traceback" not in error


def test_cli_run_reports_invalid_config_without_traceback(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "wendell.toml"
    config_path.write_text('agent_command = "python scripts/agent.py"\n', encoding="utf-8")

    exit_code = main(["run", "--suite", "refund-agent-regression", "--config", str(config_path)])

    assert exit_code == 1
    error = capsys.readouterr().err
    assert "non-empty string field `project`" in error
    assert "Traceback" not in error


def test_legacy_cli_requires_config_instead_of_demo_result(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = main([])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "config file `wendell.toml` was not found" in captured.err
    assert "wendell suites configure --suite <suite-slug>" in captured.err
    assert "wendell init" not in captured.err
    assert "demo_missing_context" not in captured.out


def test_cli_test_requires_real_agent_command(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "wendell.toml"
    config_path.write_text(
        "\n".join(
            [
                'project = "agent"',
                'worldsim_input = "input.json"',
                "upload_traces = false",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(["test", "--config", str(config_path)])

    assert exit_code == 2
    assert "agent_command" in capsys.readouterr().err


def test_cli_test_without_local_or_hosted_suite_does_not_fabricate_demo(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("WENDELL_INKPASS_API_KEY", raising=False)
    config_path = tmp_path / "wendell.toml"
    config_path.write_text(
        "\n".join(
            [
                'project = "agent"',
                'agent_command = "python agent.py"',
                "upload_traces = false",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(["test", "--config", str(config_path)])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "no local suite input is configured" in captured.err
    assert "demo_missing_context" not in captured.out


def test_cli_test_prints_rule_linked_failure_details(tmp_path: Path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "wendell.toml"
    config_path.write_text(
        "\n".join(
            [
                'project = "agent"',
                'mode = "blocking"',
                'worldsim_input = "input.json"',
                'agent_command = "python agent.py"',
                "upload_traces = false",
                "[gates]",
                "suite_min_score = 0.90",
                "scenario_min_score = 0.85",
                "critical_failures_allowed = 0",
            ]
        ),
        encoding="utf-8",
    )

    def fake_run_suite(config: RunnerConfig):
        from wendell_ci.models import ScenarioResult, SuiteResult

        assert config.agent_command == "python agent.py"
        return SuiteResult(
            project=config.project,
            world="dogfood",
            scenario_pack="local",
            scenario_results=(
                ScenarioResult(
                    scenario_id="playbook_to_suite_compilation",
                    score=0.42,
                    critical_failures=("suite_generation_skipped_required_questions",),
                    step_statuses={"ask_required_questions": "not_attempted"},
                    assertion_results=(
                        {
                            "assertion_id": "assert_no_generation_before_questions",
                            "rule_id": "questions_before_approval",
                            "status": "failed",
                            "message": "suite_generator.generate_rule_linked_scenarios happened before required evidence.",
                            "event_indexes": [3],
                        },
                    ),
                    improvement_prompts=("Ask and resolve required questions before generating a suite.",),
                ),
            ),
        )

    monkeypatch.setattr("wendell_ci.cli._run_suite", fake_run_suite)

    exit_code = main(["test", "--config", str(config_path)])

    assert exit_code == 1
    output = capsys.readouterr().out


def test_cli_loads_repo_env_without_overwriting_existing_values(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("EXISTING_SECRET", "from-shell")
    (tmp_path / ".env").write_text(
        "OPENROUTER_API_KEY='from-env-file'\nEXISTING_SECRET=from-env-file\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "user-project" / "wendell.toml"
    config_path.parent.mkdir()
    config = RunnerConfig(project="customer-service-agent")

    _load_env_files(config_path, config)

    assert os.environ["OPENROUTER_API_KEY"] == "from-env-file"
    assert os.environ["EXISTING_SECRET"] == "from-shell"


def test_cli_login_stores_api_key_from_stdin(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    monkeypatch.setattr("sys.stdin", type("Input", (), {"read": lambda self: "inkpass-key\n"})())

    exit_code = main(["login", "--api-key-stdin", "--api-url", "http://127.0.0.1:8765"])

    assert exit_code == 0
    assert "credentials stored" in capsys.readouterr().out
    assert (tmp_path / "wendell-config" / "credentials.json").exists()


def test_cli_login_with_email_claims_runner_and_stores_key(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    monkeypatch.setattr("sys.stdin", type("Input", (), {"read": lambda self: "correct horse battery staple\n"})())

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            assert api_url == "http://127.0.0.1:8765"
            assert api_key is None

        def start_runner_session(self, payload: dict) -> dict:
            assert payload["computer_name"]
            return {"runner_session_id": "rsess_123"}

        def login_cli(self, payload: dict) -> dict:
            assert payload["runner_session_id"] == "rsess_123"
            assert payload["email"] == "person@example.com"
            assert payload["password"] == "correct horse battery staple"
            return {"api_key": "wpk_live_fake", "runner": {"runner_id": "runner_123"}}

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(
        [
            "login",
            "--email",
            "person@example.com",
            "--password-stdin",
            "--api-url",
            "http://127.0.0.1:8765",
        ]
    )

    assert exit_code == 0
    assert "credentials stored" in capsys.readouterr().out
    credential_file = tmp_path / "wendell-config" / "credentials.json"
    assert credential_file.exists()
    assert "wpk_live_fake" in credential_file.read_text(encoding="utf-8")


def test_cli_auth_status_reports_stored_profile(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key"]) == 0

    exit_code = main(["auth", "status"])

    assert exit_code == 0
    assert "profile `default`" in capsys.readouterr().out


def test_cli_auth_export_prints_stored_runner_key_for_ci(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0
    capsys.readouterr()

    assert main(["auth", "export", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "api_key": "inkpass-key",
        "api_url": "http://127.0.0.1:8765",
        "env_var": "WENDELL_INKPASS_API_KEY",
        "profile": "default",
        "runner_id": None,
    }

    assert main(["auth", "export", "--format", "raw"]) == 0
    assert capsys.readouterr().out == "inkpass-key\n"


def test_cli_auth_export_requires_stored_credential(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))

    exit_code = main(["auth", "export", "--format", "json"])

    assert exit_code == 1
    assert capsys.readouterr().err


def test_cli_park_login_is_not_a_command(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))

    try:
        main(["park", "login", "--api-key-stdin", "--api-url", "http://127.0.0.1:8765"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("park login should be rejected; use top-level `wendell login`")

    assert not (tmp_path / "wendell-config" / "credentials.json").exists()


def test_cli_whoami_reports_stored_identity_from_api(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            assert api_url == "http://127.0.0.1:8765"
            assert api_key == "inkpass-key"

        def get_identity(self) -> dict:
            return {
                "auth_type": "api_key",
                "external_org_id": "org_123",
                "external_user_id": "user_123",
                "api_key_id": "ak_123",
                "runner_id": "runner_123",
            }

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0

    exit_code = main(["whoami"])

    assert exit_code == 0
    output = capsys.readouterr().out


def test_cli_whoami_can_validate_against_api_url_override(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    seen: list[str] = []

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            seen.append(api_url)
            assert api_key == "inkpass-key"

        def get_identity(self) -> dict:
            return {"auth_type": "api_key", "external_org_id": "org_123", "external_user_id": "user_123"}

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "https://api.wendellai.com"]) == 0

    exit_code = main(["whoami", "--api-url", "http://127.0.0.1:8765"])

    assert exit_code == 0
    assert seen[-1] == "http://127.0.0.1:8765"
    assert "auth=api_key" in capsys.readouterr().out


def test_cli_env_api_key_uses_production_api_by_default(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    monkeypatch.setenv("WENDELL_INKPASS_API_KEY", "inkpass-key")
    monkeypatch.delenv("WENDELL_API_URL", raising=False)

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            assert api_url == "https://api.wendellai.com"
            assert api_key == "inkpass-key"

        def list_test_suites(self) -> dict:
            return {"test_suites": []}

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(["suites", "list"])

    assert exit_code == 0
    assert "No published test suites found." in capsys.readouterr().out


def test_cli_stored_api_key_uses_production_api_by_default(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key"]) == 0

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            assert api_url == "https://api.wendellai.com"
            assert api_key == "inkpass-key"

        def get_identity(self) -> dict:
            return {"auth_type": "api_key", "external_org_id": "org_123", "external_user_id": "user_123"}

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(["whoami"])

    assert exit_code == 0
    assert "auth=api_key" in capsys.readouterr().out


def test_hosted_commands_without_credentials_point_to_register_and_ci_secret(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    monkeypatch.delenv("WENDELL_INKPASS_API_KEY", raising=False)

    exit_code = main(["suites", "list"])

    assert exit_code == 1
    error = capsys.readouterr().err
    assert "no runner credential found" in error
    assert "wendell register" in error
    assert "WENDELL_INKPASS_API_KEY" in error
    assert "wendell login --api-key-stdin --validate" in error
    assert str(tmp_path / "wendell-config" / "credentials.json") in error


def test_cli_suites_list_prints_accessible_suites(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            assert api_url == "http://127.0.0.1:8765"
            assert api_key == "inkpass-key"

        def list_test_suites(self) -> dict:
            return {
                "test_suites": [
                    {
                        "slug": "commerce-support",
                        "world_version": 1,
                        "scenario_pack": "commerce-support-benchmark",
                        "scenario_pack_version": 1,
                    }
                ]
            }

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(["suites", "list"])

    assert exit_code == 0
    assert "commerce-support" in capsys.readouterr().out


def test_cli_suites_show_prints_suite_detail(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            assert api_url == "http://127.0.0.1:8765"
            assert api_key == "inkpass-key"

        def get_test_suite(self, suite_slug: str) -> dict:
            assert suite_slug == "commerce-support"
            return {
                "world": {"slug": "commerce-support"},
                "version": {"version": 3},
                "scenario_pack": {"slug": "commerce-support-benchmark", "version": 2},
            }

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(["suites", "show", "commerce-support"])

    output = capsys.readouterr().out
    assert exit_code == 0


def test_cli_playbook_create_can_extract_summary_from_source_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0
    source = tmp_path / "playbook.md"
    source.write_text("Never ask for credit cards. Use BayFlow before booking.", encoding="utf-8")
    calls: list[tuple[str, dict]] = []

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            assert api_url == "http://127.0.0.1:8765"
            assert api_key == "inkpass-key"

        def create_test_suite_draft(self, payload: dict) -> dict:
            calls.append(("create", payload))
            assert str(source) in payload["source_material"]
            assert source.read_text(encoding="utf-8") in payload["source_material"]
            return {"draft": {"id": "wdraft_123", "name": payload["name"]}}

        def generate_playbook_summary(self, draft_id: str) -> dict:
            calls.append(("summary", {"draft_id": draft_id}))
            return {
                "playbook_summary": {
                    "id": "psum_123",
                    "status": "generated",
                    "summary_payload": {"required_questions": [{"section": "roles_and_actors", "question": "Who participates?", "required": True}]},
                }
            }

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(
        [
            "playbook",
            "create",
            "--name",
            "TirePro Scheduling",
            "--workflow-summary",
            "Schedule tire service appointments.",
            "--source",
            str(source),
            "--project-ref",
            "tirepro",
            "--extract",
        ]
    )

    assert exit_code == 0
    assert calls[0][0] == "create"
    assert "org_ref" not in calls[0][1]
    assert calls[1] == ("summary", {"draft_id": "wdraft_123"})


@requires_worldsim_compiler
def test_cli_playbook_compile_writes_suite_and_config(tmp_path: Path, capsys) -> None:
    source = tmp_path / "playbook.md"
    suite = tmp_path / "suite.json"
    config = tmp_path / "wendell.toml"
    source.write_text(
        "\n".join(
            [
                "# Refund Support Playbook",
                "## Actors",
                "- customer",
                "- support agent",
                "## Systems and tools",
                "- billing: lookup invoice, create refund",
                "## Required behavior",
                "- Look up invoice before creating refund.",
                "## Evidence requirements",
                "- invoice lookup event",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "playbook",
            "compile",
            "--source",
            str(source),
            "--name",
            "Refund Support",
            "--workflow-summary",
            "Evaluate refund support agents.",
            "--output",
            str(suite),
            "--config-output",
            str(config),
            "--agent-command",
            "python refund_agent.py",
        ]
    )

    assert exit_code == 0
    assert "Compiled suite" in capsys.readouterr().out
    payload = json.loads(suite.read_text(encoding="utf-8"))
    assert payload["name"] == "Refund Support"
    assert payload["use_cases"][0]["known_facts"]["playbook_assertions"]
    assert 'agent_command = "python refund_agent.py"' in config.read_text(encoding="utf-8")


@requires_worldsim_compiler
def test_cli_init_scaffolds_app_and_generated_suite(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = main(
        [
            "init",
            "--project",
            "checkout-agent",
            "--name",
            "Checkout Agent Regression",
            "--workflow-summary",
            "Evaluate checkout agents before production deploys.",
        ]
    )

    assert exit_code == 0
    assert (tmp_path / "playbook.md").exists()
    assert (tmp_path / ".wendell" / "suite.json").exists()
    assert (tmp_path / "scripts" / "wendell_agent_adapter.py").exists()
    assert (tmp_path / "wendell.toml").exists()
    assert "WENDELL_APP_AGENT_COMMAND" in (tmp_path / "scripts" / "wendell_agent_adapter.py").read_text(encoding="utf-8")
    suite = json.loads((tmp_path / ".wendell" / "suite.json").read_text(encoding="utf-8"))
    assert suite["name"] == "Checkout Agent Regression"
    assert suite["use_cases"][0]["known_facts"]["playbook_assertions"]


def test_cli_init_without_internal_compiler_fails_before_writing_files(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("wendell_ci.cli._require_local_playbook_compiler", lambda: (_ for _ in ()).throw(RuntimeError("compiler missing")))

    exit_code = main(["init", "--project", "checkout-agent"])

    assert exit_code == 1
    assert "compiler missing" in capsys.readouterr().err
    assert not (tmp_path / "playbook.md").exists()
    assert not (tmp_path / "scripts" / "wendell_agent_adapter.py").exists()
    assert not (tmp_path / "wendell.toml").exists()
    assert not (tmp_path / ".wendell" / "suite.json").exists()


def test_cli_init_rejects_example_agent_scaffold(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        main(["init", "--project", "checkout-agent", "--example-agent"])

    assert exc.value.code == 2
    assert not (tmp_path / "wendell.toml").exists()


@requires_worldsim_compiler
def test_cli_init_adapter_accepts_claude_structured_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["init", "--project", "checkout-agent"]) == 0
    fake_agent = tmp_path / "fake_claude.py"
    fake_agent.write_text(
        "\n".join(
            [
                "import json",
                "print(json.dumps({",
                '    "type": "result",',
                '    "result": "",',
                '    "structured_output": {',
                '        "message": "handled",',
                '        "tool_calls": [{"name": "workflow_console.inspect_request", "args": {"case_id": "case_123"}}],',
                '        "metrics": {"agent": "claude-code"},',
                "    },",
                "}))",
            ]
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, "scripts/wendell_agent_adapter.py"],
        input=json.dumps({"rubric": {"action_steps": []}}),
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "WENDELL_APP_AGENT_COMMAND": f"{sys.executable} {fake_agent}"},
    )

    result = json.loads(completed.stdout)
    assert result["message"] == "handled"
    assert result["tool_calls"][0]["name"] == "workflow_console.inspect_request"
    assert result["metrics"] == {"agent": "claude-code"}


@requires_worldsim_compiler
def test_cli_init_default_adapter_fails_until_wired(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["init", "--project", "checkout-agent"]) == 0
    exit_code = main(["test", "--config", "wendell.toml"])

    assert exit_code == 2
    output = capsys.readouterr()


@requires_worldsim_compiler
def test_cli_init_refuses_to_overwrite_existing_files(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "playbook.md").write_text("existing", encoding="utf-8")

    exit_code = main(["init"])

    assert exit_code == 1
    assert "already exists" in capsys.readouterr().err


def test_cli_playbook_review_generates_summary_when_missing(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            self.api_url = api_url

        def get_test_suite_draft(self, draft_id: str) -> dict:
            assert draft_id == "wdraft_123"
            return {"draft": {"id": draft_id, "name": "TirePro"}, "latest_playbook_summary": None}

        def generate_playbook_summary(self, draft_id: str) -> dict:
            assert draft_id == "wdraft_123"
            return {
                "playbook_summary": {
                    "id": "psum_123",
                    "status": "generated",
                    "summary_payload": {
                        "actors": ["front desk agent"],
                        "systems_tools": ["BayFlow"],
                        "policies": ["Check bay capacity before confirming."],
                        "allowed_actions": ["Book service appointments."],
                        "required_questions": [],
                    },
                }
            }

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(["playbook", "review", "wdraft_123"])

    assert exit_code == 0
    output = capsys.readouterr().out


def test_cli_playbook_apply_submits_patch_operations(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0
    patch = tmp_path / "review.json"
    patch.write_text(
        json.dumps(
            {
                "operations": [
                    {"op": "answer_question", "section": "roles_and_actors", "answer": ["customer", "front desk agent"]},
                    {"op": "add_primitive", "section_key": "systems_tools", "value": ["BayFlow", "TireStock"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    reviews: list[dict] = []

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            self.api_url = api_url

        def get_playbook_summary(self, summary_id: str) -> dict:
            return {
                "playbook_summary": {
                    "id": summary_id,
                    "status": "generated",
                    "summary_payload": {
                        "systems_tools": ["Remindly"],
                        "required_questions": [{"section": "roles_and_actors", "question": "Who participates?", "required": True}],
                    },
                }
            }

        def review_playbook_summary(self, summary_id: str, payload: dict) -> dict:
            reviews.append(payload)
            return {
                "playbook_summary": {
                    "id": summary_id,
                    "status": "in_review",
                    "summary_payload": {"systems_tools": ["Remindly"], "required_questions": []},
                }
            }

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(["playbook", "apply", "psum_123", "--file", str(patch)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert reviews[0]["question_answers"] == [{"section": "roles_and_actors", "answer": ["customer", "front desk agent"]}]
    assert reviews[1]["section_key"] == "systems_tools"
    assert reviews[1]["changed_fields"] == {"value": ["Remindly", "BayFlow", "TireStock"]}


def test_cli_playbook_approve_can_generate_suite(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0
    calls: list[str] = []

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            self.api_url = api_url

        def get_test_suite_draft(self, draft_id: str) -> dict:
            return {"draft": {"id": draft_id}, "latest_playbook_summary": {"id": "psum_123", "status": "in_review", "summary_payload": {}}}

        def approve_playbook_summary(self, summary_id: str, payload: dict) -> dict:
            calls.append(f"approve:{summary_id}")
            return {"playbook_summary": {"id": summary_id, "status": "approved", "summary_payload": {"required_questions": []}}}

        def generate_test_suite_candidate(self, draft_id: str) -> dict:
            calls.append(f"generate:{draft_id}")
            return {"draft": {"id": draft_id, "status": "ready_for_compilation", "metadata": {"latest_snapshot_id": "cinp_123"}}}

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(["playbook", "approve", "wdraft_123", "--generate-suite"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert calls == ["approve:psum_123", "generate:wdraft_123"]


def test_cli_suites_generate_creates_candidate_from_draft(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            assert api_url == "http://127.0.0.1:8765"
            assert api_key == "inkpass-key"

        def generate_test_suite_candidate(self, draft_id: str) -> dict:
            assert draft_id == "wdraft_123"
            return {"draft": {"id": draft_id, "status": "ready_for_compilation", "metadata": {"latest_snapshot_id": "cinp_123"}}}

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(["suites", "generate", "--draft", "wdraft_123"])

    assert exit_code == 0
    output = capsys.readouterr().out


def test_cli_suites_publish_publishes_generated_draft(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            assert api_url == "http://127.0.0.1:8765"
            assert api_key == "inkpass-key"

        def publish_test_suite_draft(self, draft_id: str) -> dict:
            assert draft_id == "wdraft_123"
            return {
                "world": {"slug": "tirepro-scheduling"},
                "version": {"version": 1},
                "scenario_pack": {"slug": "commerce-support-benchmark", "version": 1},
            }

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(["suites", "publish", "--draft", "wdraft_123"])

    assert exit_code == 0


def test_cli_suites_configure_writes_hosted_config_and_adapter(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            assert api_url == "http://127.0.0.1:8765"
            assert api_key == "inkpass-key"

        def list_test_suites(self) -> dict:
            return {
                "test_suites": [
                    {
                        "slug": "refund-agent-regression",
                        "world_version": 3,
                        "scenario_pack": "refund-agent-regression-pack",
                        "scenario_pack_version": 2,
                    }
                ]
            }

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(["suites", "configure", "--suite", "refund-agent-regression", "--project", "refund-agent"])

    assert exit_code == 0
    config = (tmp_path / "wendell.toml").read_text(encoding="utf-8")
    assert 'project = "refund-agent"' in config
    assert 'agent_command = "python scripts/wendell_agent_adapter.py"' in config
    assert "upload_traces = true" in config
    assert "api_url" not in config
    adapter = (tmp_path / "scripts" / "wendell_agent_adapter.py").read_text(encoding="utf-8")
    assert "WENDELL_APP_AGENT_COMMAND" in adapter
    assert "https://docs.wendellai.com/agent-adapter-contract" in adapter
    assert "ADAPTER_HELP" in adapter
    assert "shell=True" not in adapter
    assert "shlex.split(command)" in adapter
    assert "wendell-example" not in adapter


def test_cli_suites_configure_generates_exact_tool_manifest_adapter(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            self.api_url = api_url

        def list_test_suites(self) -> dict:
            return {
                "test_suites": [
                    {
                        "slug": "coding-agent-regression",
                        "world_version": 1,
                        "scenario_pack": "coding-agent-regression-pack",
                        "scenario_pack_version": 1,
                    }
                ]
            }

        def get_test_suite(self, suite_slug: str) -> dict:
            assert suite_slug == "coding-agent-regression"
            return {
                "world": {"slug": "coding-agent-regression"},
                "versions": [{"version": 1}],
                "scenario_packs": [{"slug": "coding-agent-regression-pack", "version": 1}],
                "tool_contracts": [
                    {
                        "name": "issue_transcript.read_the_user_bug_report_and_constraints",
                        "arguments": {"case_id": "string"},
                        "effects": {"read_the_user_bug_report_and_constraints_completed": True},
                        "reveals": ["read_the_user_bug_report_and_constraints_completed"],
                        "requires": [],
                        "unsafe_if": [],
                        "scenario_coverage": ["playbook_workflow_1"],
                    },
                    {
                        "name": "repository.read_git_status",
                        "arguments": {"case_id": "string"},
                        "effects": {"read_git_status_completed": True},
                        "reveals": ["read_git_status_completed"],
                        "requires": ["inspect_files_completed"],
                        "unsafe_if": [],
                        "scenario_coverage": ["playbook_workflow_1"],
                    },
                ],
            }

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(["suites", "configure", "--suite", "coding-agent-regression"])

    assert exit_code == 0
    manifest = json.loads((tmp_path / "wendell_tool_manifest.json").read_text(encoding="utf-8"))
    assert [tool["name"] for tool in manifest["tool_contracts"]] == [
        "issue_transcript.read_the_user_bug_report_and_constraints",
        "repository.read_git_status",
    ]
    adapter = (tmp_path / "scripts" / "wendell_agent_adapter.py").read_text(encoding="utf-8")
    assert "SUPPORTED_TOOLS" in adapter
    assert "def issue_transcript__read_the_user_bug_report_and_constraints" in adapter
    assert "def repository__read_git_status" in adapter
    assert "wendell.handshake" in adapter


def test_cli_suites_configure_writes_escaped_toml_strings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            pass

        def list_test_suites(self) -> dict:
            return {
                "test_suites": [
                    {
                        "slug": "refund-agent-regression",
                        "world_version": 3,
                        "scenario_pack": "refund-agent-regression-pack",
                        "scenario_pack_version": 2,
                    }
                ]
            }

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(
        [
            "suites",
            "configure",
            "--suite",
            "refund-agent-regression",
            "--project",
            'refund "agent"',
            "--agent-command",
            'python scripts/run_my_agent.py --name "Refund Agent"',
        ]
    )

    assert exit_code == 0
    config = tomllib.loads((tmp_path / "wendell.toml").read_text(encoding="utf-8"))
    assert config["project"] == 'refund "agent"'
    assert config["agent_command"] == 'python scripts/run_my_agent.py --name "Refund Agent"'


def test_cli_suites_configure_refuses_to_overwrite(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    (tmp_path / "wendell.toml").write_text("existing", encoding="utf-8")
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            pass

        def list_test_suites(self) -> dict:
            return {
                "test_suites": [
                    {
                        "slug": "refund-agent-regression",
                        "world_version": 3,
                        "scenario_pack": "refund-agent-regression-pack",
                        "scenario_pack_version": 2,
                    }
                ]
            }

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(["suites", "configure", "--suite", "refund-agent-regression"])

    assert exit_code == 1
    assert "already exists" in capsys.readouterr().err


def test_cli_suites_configure_requires_ready_suite(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            pass

        def list_test_suites(self) -> dict:
            return {"test_suites": [{"slug": "draft-suite", "world_version": None, "scenario_pack": None}]}

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(["suites", "configure", "--suite", "draft-suite"])

    assert exit_code == 1
    assert "not bound to a ready scenario pack" in capsys.readouterr().err
    assert not (tmp_path / "wendell.toml").exists()


def test_cli_run_suite_resolves_suite_and_uploads_remote_runtime(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0
    config_path = tmp_path / "wendell.toml"
    config_path.write_text('project = "customer-service-agent"\nagent_command = "python agent.py"\n', encoding="utf-8")
    captured: dict[str, object] = {}

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            assert api_url == "http://127.0.0.1:8765"
            assert api_key == "inkpass-key"
            self.api_url = api_url

        def list_test_suites(self) -> dict:
            return {
                "test_suites": [
                    {
                        "slug": "commerce-support",
                        "world_version": 2,
                        "scenario_pack": "commerce-support-benchmark",
                        "scenario_pack_version": 1,
                    }
                ]
            }

        def create_cli_session_link(self, payload: dict) -> dict:
            return {"url": f"https://wendell.example{payload['next']}", "expires_in_seconds": 300}

    def fake_run_suite_with_remote_upload(config: RunnerConfig, client=None):
        captured["config"] = config
        return (
            SuiteResult(
                project="customer-service-agent",
                world="commerce-support",
                scenario_pack="commerce-support-benchmark",
                scenario_results=(ScenarioResult(scenario_id="scenario_a", score=1.0),),
            ),
            {"run_id": "run_123", "url": "/dashboard/runs/run_123", "status": "completed"},
        )

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)
    monkeypatch.setattr("wendell_ci.cli._run_suite_with_remote_upload", fake_run_suite_with_remote_upload)

    exit_code = main(["run", "--suite", "commerce-support", "--config", str(config_path)])

    assert exit_code == 0
    output = capsys.readouterr().out
    run_config = captured["config"]
    assert isinstance(run_config, RunnerConfig)
    assert run_config.world == "commerce-support"
    assert run_config.world_version == "2"
    assert run_config.scenario_pack == "commerce-support-benchmark"
    assert run_config.scenario_pack_version == "1"
    assert run_config.metadata["test_suite"]["slug"] == "commerce-support"


def test_cli_run_suite_reuses_stored_credential_for_remote_runtime(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "stored-runner-key", "--api-url", "http://127.0.0.1:8765"]) == 0
    capsys.readouterr()
    agent_script = tmp_path / "agent.py"
    agent_script.write_text(
        "import json, sys\n"
        "json.load(sys.stdin)\n"
        "print(json.dumps({'message': 'handled', 'tool_calls': []}))\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "wendell.toml"
    config_path.write_text(
        f'project = "customer-service-agent"\nagent_command = "{sys.executable} {agent_script}"\n',
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            self.api_url = api_url
            self.api_key = api_key

        @classmethod
        def from_env(cls, api_url: str, api_key_env: str):
            raise AssertionError("wendell run should reuse the authenticated stored-credential client")

        def list_test_suites(self) -> dict:
            assert self.api_key == "stored-runner-key"
            return {
                "test_suites": [
                    {
                        "slug": "commerce-support",
                        "world_version": 2,
                        "scenario_pack": "commerce-support-benchmark",
                        "scenario_pack_version": 1,
                    }
                ]
            }

        def create_run(self, payload: dict) -> dict:
            captured["create_run_api_key"] = self.api_key
            captured["create_run"] = payload
            return {"run_id": "run_remote", "url": "/runs/run_remote"}

        def get_run(self, run_id: str) -> dict:
            return {"world_version_id": "wver_remote", "scenario_pack_id": "spack_remote"}

        def get_run_work(self, run_id: str) -> dict:
            if captured.get("work_done"):
                return {"run_id": run_id, "done": True}
            captured["work_done"] = True
            return {
                "run_id": run_id,
                "done": False,
                "scenario_execution": {"id": "sexec_a", "scenario_key": "scenario_a"},
                "scenario": {"id": "scenario_a"},
                "transcript": [{"role": "customer", "text": "Help me."}],
                "available_tools": [],
            }

        def submit_agent_turn(self, run_id: str, scenario_execution_id: str, payload: dict) -> dict:
            return {
                "trajectory_id": "traj_a",
                "scenario_execution_id": scenario_execution_id,
                "status": "completed",
                "scenario_score": {"id": "sscore_a", "overall_score": 1.0, "critical_failure_count": 0},
            }

        def complete_run(self, run_id: str) -> dict:
            return {"status": "completed"}

        def create_cli_session_link(self, payload: dict) -> dict:
            return {}

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(["run", "--suite", "commerce-support", "--config", str(config_path)])

    assert exit_code == 0
    assert captured["create_run_api_key"] == "stored-runner-key"
    assert captured["create_run"]["world"] == "commerce-support"
    assert "Suite run: run_remote" in capsys.readouterr().out


def test_cli_run_preflight_blocks_missing_adapter_tools_before_create_run(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0
    agent_script = tmp_path / "agent.py"
    agent_script.write_text(
        "import json\n"
        "print(json.dumps({'supported_tools': ['repository.inspect_files'], 'adapter_name': 'partial'}))\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "wendell.toml"
    config_path.write_text(
        f'project = "coding-agent"\nagent_command = "{sys.executable} {agent_script}"\n',
        encoding="utf-8",
    )

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            self.api_url = api_url

        def list_test_suites(self) -> dict:
            return {
                "test_suites": [
                    {
                        "slug": "coding-agent-regression",
                        "world_version": 1,
                        "scenario_pack": "coding-agent-pack",
                        "scenario_pack_version": 1,
                    }
                ]
            }

        def get_test_suite(self, suite_slug: str) -> dict:
            return {
                "world": {"slug": suite_slug},
                "versions": [{"version": 1}],
                "scenario_packs": [{"slug": "coding-agent-pack", "version": 1}],
                "tool_contracts": [
                    {"name": "repository.inspect_files", "arguments": {}},
                    {"name": "repository.read_git_status", "arguments": {}},
                ],
            }

        def create_run(self, payload: dict) -> dict:
            raise AssertionError("preflight should fail before creating a remote run")

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(["run", "--suite", "coding-agent-regression", "--config", str(config_path)])

    assert exit_code == 1
    error = capsys.readouterr().err
    assert "preflight failed" in error
    assert "repository.read_git_status" in error
    assert "wendell suites configure --suite coding-agent-regression --config" in error


def test_cli_run_skip_preflight_creates_run_with_missing_adapter_tools(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0
    agent_script = tmp_path / "agent.py"
    agent_script.write_text(
        "import json\n"
        "print(json.dumps({'message': 'legacy adapter', 'tool_calls': []}))\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "wendell.toml"
    config_path.write_text(
        f'project = "coding-agent"\nagent_command = "{sys.executable} {agent_script}"\n',
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            self.api_url = api_url

        def list_test_suites(self) -> dict:
            return {
                "test_suites": [
                    {
                        "slug": "coding-agent-regression",
                        "world_version": 1,
                        "scenario_pack": "coding-agent-pack",
                        "scenario_pack_version": 1,
                    }
                ]
            }

        def get_test_suite(self, suite_slug: str) -> dict:
            return {
                "world": {"slug": suite_slug},
                "versions": [{"version": 1}],
                "scenario_packs": [{"slug": "coding-agent-pack", "version": 1}],
                "tool_contracts": [{"name": "repository.read_git_status", "arguments": {}}],
            }

        def create_run(self, payload: dict) -> dict:
            captured["create_run"] = payload
            return {"run_id": "run_skip_preflight", "url": "/runs/run_skip_preflight"}

        def get_run(self, run_id: str) -> dict:
            return {"world_version_id": "wver_1", "scenario_pack_id": "spack_1"}

        def get_run_work(self, run_id: str) -> dict:
            return {"run_id": run_id, "done": True}

        def complete_run(self, run_id: str) -> dict:
            return {"status": "completed"}

        def create_cli_session_link(self, payload: dict) -> dict:
            return {}

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(["run", "--suite", "coding-agent-regression", "--config", str(config_path), "--skip-preflight"])

    assert exit_code == 0
    assert captured["create_run"]["world"] == "coding-agent-regression"
    assert "Suite run: run_skip_preflight" in capsys.readouterr().out


def test_cli_run_suite_uses_runtime_world_binding_when_it_differs_from_suite_slug(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0
    config_path = tmp_path / "wendell.toml"
    config_path.write_text('project = "customer-service-agent"\nagent_command = "python agent.py"\n', encoding="utf-8")
    captured: dict[str, object] = {}

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            self.api_url = api_url

        def list_test_suites(self) -> dict:
            return {
                "test_suites": [
                    {
                        "slug": "refund-agent-regression",
                        "world_slug": "refund-agent-world",
                        "world_version": 4,
                        "scenario_pack": "refund-agent-pack",
                        "scenario_pack_version": 2,
                    }
                ]
            }

        def create_cli_session_link(self, payload: dict) -> dict:
            return {}

    def fake_run_suite_with_remote_upload(config: RunnerConfig, client=None):
        captured["config"] = config
        return (
            SuiteResult(
                project="customer-service-agent",
                world="refund-agent-world",
                scenario_pack="refund-agent-pack",
                scenario_results=(ScenarioResult(scenario_id="scenario_a", score=1.0),),
            ),
            {"run_id": "run_123", "status": "completed"},
        )

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)
    monkeypatch.setattr("wendell_ci.cli._run_suite_with_remote_upload", fake_run_suite_with_remote_upload)

    exit_code = main(["run", "--suite", "refund-agent-regression", "--config", str(config_path)])

    assert exit_code == 0
    capsys.readouterr()
    run_config = captured["config"]
    assert isinstance(run_config, RunnerConfig)
    assert run_config.world == "refund-agent-world"
    assert run_config.world_version == "4"
    assert run_config.scenario_pack == "refund-agent-pack"
    assert run_config.scenario_pack_version == "2"
    assert run_config.metadata["test_suite"]["slug"] == "refund-agent-regression"
    assert run_config.external_ci_ref["suite_slug"] == "refund-agent-regression"


def test_cli_run_suite_adds_github_actions_ci_reference(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/support-agent")
    monkeypatch.setenv("GITHUB_WORKFLOW", "Agent regression")
    monkeypatch.setenv("GITHUB_JOB", "wendell")
    monkeypatch.setenv("GITHUB_RUN_ID", "123456")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_ACTOR", "octocat")
    monkeypatch.setenv("GITHUB_REF", "refs/pull/42/merge")
    monkeypatch.setenv("GITHUB_REF_NAME", "42/merge")
    monkeypatch.setenv("GITHUB_REF_TYPE", "branch")
    monkeypatch.setenv("GITHUB_HEAD_REF", "feature/refund-agent")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.setenv("GITHUB_SHA", "abc123def456")
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0
    config_path = tmp_path / "wendell.toml"
    config_path.write_text(
        "\n".join(
            [
                'project = "customer-service-agent"',
                'agent_command = "python agent.py"',
                "[external_ci_ref]",
                'workflow = "Configured workflow"',
            ]
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            self.api_url = api_url

        def list_test_suites(self) -> dict:
            return {
                "test_suites": [
                    {
                        "slug": "refund-agent-regression",
                        "world_slug": "refund-agent-world",
                        "world_version": 4,
                        "scenario_pack": "refund-agent-pack",
                        "scenario_pack_version": 2,
                    }
                ]
            }

        def create_cli_session_link(self, payload: dict) -> dict:
            return {}

    def fake_run_suite_with_remote_upload(config: RunnerConfig, client=None):
        captured["config"] = config
        return (
            SuiteResult(
                project="customer-service-agent",
                world="refund-agent-world",
                scenario_pack="refund-agent-pack",
                scenario_results=(ScenarioResult(scenario_id="scenario_a", score=1.0),),
            ),
            {"run_id": "run_123", "status": "completed"},
        )

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)
    monkeypatch.setattr("wendell_ci.cli._run_suite_with_remote_upload", fake_run_suite_with_remote_upload)

    exit_code = main(["run", "--suite", "refund-agent-regression", "--config", str(config_path)])

    assert exit_code == 0
    capsys.readouterr()
    run_config = captured["config"]
    assert isinstance(run_config, RunnerConfig)
    assert run_config.external_ci_ref["source"] == "wendell_suite_cli"
    assert run_config.external_ci_ref["suite_slug"] == "refund-agent-regression"
    assert run_config.external_ci_ref["provider"] == "github_actions"
    assert run_config.external_ci_ref["repository"] == "acme/support-agent"
    assert run_config.external_ci_ref["workflow"] == "Configured workflow"
    assert run_config.external_ci_ref["job"] == "wendell"
    assert run_config.external_ci_ref["run_id"] == "123456"
    assert run_config.external_ci_ref["run_attempt"] == "2"
    assert run_config.external_ci_ref["run_url"] == "https://github.com/acme/support-agent/actions/runs/123456"
    assert run_config.external_ci_ref["event_name"] == "pull_request"
    assert run_config.external_ci_ref["actor"] == "octocat"
    assert run_config.external_ci_ref["ref"] == "refs/pull/42/merge"
    assert run_config.external_ci_ref["head_ref"] == "feature/refund-agent"
    assert run_config.external_ci_ref["base_ref"] == "main"
    assert run_config.external_ci_ref["sha"] == "abc123def456"


def test_cli_run_suite_json_includes_private_report_link(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0
    config_path = tmp_path / "wendell.toml"
    config_path.write_text('project = "customer-service-agent"\nagent_command = "python agent.py"\n', encoding="utf-8")
    capsys.readouterr()

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            self.api_url = api_url

        def list_test_suites(self) -> dict:
            return {
                "test_suites": [
                    {
                        "slug": "commerce-support",
                        "world_version": 2,
                        "scenario_pack": "commerce-support-benchmark",
                        "scenario_pack_version": 1,
                    }
                ]
            }

        def create_cli_session_link(self, payload: dict) -> dict:
            assert payload == {"next": "/dashboard/runs/run_123", "run_id": "run_123"}
            return {"url": "https://wendell.example/dashboard/runs/run_123", "expires_in_seconds": 300}

    def fake_run_suite_with_remote_upload(config: RunnerConfig, client=None):
        return (
            SuiteResult(
                project="customer-service-agent",
                world="commerce-support",
                scenario_pack="commerce-support-benchmark",
                scenario_results=(ScenarioResult(scenario_id="scenario_a", score=1.0),),
            ),
            {"run_id": "run_123", "url": "/dashboard/runs/run_123", "status": "completed"},
        )

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)
    monkeypatch.setattr("wendell_ci.cli._run_suite_with_remote_upload", fake_run_suite_with_remote_upload)

    exit_code = main(["run", "--suite", "commerce-support", "--config", str(config_path), "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["remote"]["private_report_url"] == "https://wendell.example/dashboard/runs/run_123"
    assert payload["remote"]["private_report_url_expires_in_seconds"] == 300


def test_cli_run_suite_appends_github_step_summary(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    summary_path = tmp_path / "github-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0
    config_path = tmp_path / "wendell.toml"
    config_path.write_text('project = "customer-service-agent"\nagent_command = "python agent.py"\n', encoding="utf-8")
    capsys.readouterr()

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            self.api_url = api_url

        def list_test_suites(self) -> dict:
            return {
                "test_suites": [
                    {
                        "slug": "commerce-support",
                        "world_version": 2,
                        "scenario_pack": "commerce-support-benchmark",
                        "scenario_pack_version": 1,
                    }
                ]
            }

        def create_cli_session_link(self, payload: dict) -> dict:
            assert payload == {"next": "/dashboard/runs/run_123", "run_id": "run_123"}
            return {"url": "https://wendell.example/dashboard/runs/run_123", "expires_in_seconds": 300}

    def fake_run_suite_with_remote_upload(config: RunnerConfig, client=None):
        return (
            SuiteResult(
                project="customer-service-agent",
                world="commerce-support",
                scenario_pack="commerce-support-benchmark",
                scenario_results=(ScenarioResult(scenario_id="scenario_a", score=1.0),),
            ),
            {"run_id": "run_123", "url": "/dashboard/runs/run_123", "status": "completed"},
        )

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)
    monkeypatch.setattr("wendell_ci.cli._run_suite_with_remote_upload", fake_run_suite_with_remote_upload)

    exit_code = main(["run", "--suite", "commerce-support", "--config", str(config_path), "--github-summary"])

    assert exit_code == 0
    summary = summary_path.read_text(encoding="utf-8")
    assert "run_123" in summary
    assert "https://wendell.example/dashboard/runs/run_123" in summary


def test_cli_run_suite_returns_blocking_gate_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0
    config_path = tmp_path / "wendell.toml"
    config_path.write_text(
        "\n".join(
            [
                'project = "customer-service-agent"',
                'mode = "blocking"',
                'agent_command = "python agent.py"',
                "upload_traces = true",
                "[gates]",
                "suite_min_score = 0.90",
                "scenario_min_score = 0.85",
                "critical_failures_allowed = 0",
            ]
        ),
        encoding="utf-8",
    )

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            self.api_url = api_url

        def list_test_suites(self) -> dict:
            return {
                "test_suites": [
                    {
                        "slug": "commerce-support",
                        "world_version": 2,
                        "scenario_pack": "commerce-support-benchmark",
                        "scenario_pack_version": 1,
                    }
                ]
            }

        def create_cli_session_link(self, payload: dict) -> dict:
            return {"url": f"https://wendell.example{payload['next']}", "expires_in_seconds": 300}

    def fake_run_suite_with_remote_upload(config: RunnerConfig, client=None):
        return (
            SuiteResult(
                project="customer-service-agent",
                world="commerce-support",
                scenario_pack="commerce-support-benchmark",
                scenario_results=(ScenarioResult(scenario_id="scenario_a", score=0.42),),
            ),
            {"run_id": "run_123", "url": "/dashboard/runs/run_123", "status": "completed"},
        )

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)
    monkeypatch.setattr("wendell_ci.cli._run_suite_with_remote_upload", fake_run_suite_with_remote_upload)

    exit_code = main(["run", "--suite", "commerce-support", "--config", str(config_path)])

    assert exit_code == 1
    output = capsys.readouterr().out


def test_cli_run_suite_uses_config_api_url_for_auth_and_suite_resolution(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    assert main(["login", "--api-key", "inkpass-key"]) == 0
    config_path = tmp_path / "wendell.toml"
    config_path.write_text(
        "\n".join(
            [
                'project = "customer-service-agent"',
                'api_url = "http://127.0.0.1:8765"',
                'agent_command = "python agent.py"',
            ]
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            captured["api_url"] = api_url
            captured["api_key"] = api_key
            self.api_url = api_url

        def list_test_suites(self) -> dict:
            return {
                "test_suites": [
                    {
                        "slug": "commerce-support",
                        "world_version": 2,
                        "scenario_pack": "commerce-support-benchmark",
                        "scenario_pack_version": 1,
                    }
                ]
            }

        def create_cli_session_link(self, payload: dict) -> dict:
            return {}

    def fake_run_suite_with_remote_upload(config: RunnerConfig, client=None):
        captured["run_api_url"] = config.api_url
        return (
            SuiteResult(
                project="customer-service-agent",
                world="commerce-support",
                scenario_pack="commerce-support-benchmark",
                scenario_results=(ScenarioResult(scenario_id="scenario_a", score=1.0),),
            ),
            {"run_id": "run_123", "status": "completed"},
        )

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)
    monkeypatch.setattr("wendell_ci.cli._run_suite_with_remote_upload", fake_run_suite_with_remote_upload)

    assert main(["run", "--suite", "commerce-support", "--config", str(config_path)]) == 0

    assert captured["api_url"] == "http://127.0.0.1:8765"
    assert captured["api_key"] == "inkpass-key"
    assert captured["run_api_url"] == "http://127.0.0.1:8765"


def test_cli_run_suite_missing_config_points_to_configure(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            raise AssertionError("missing config should be reported before auth or suite resolution")

        def list_test_suites(self) -> dict:
            raise AssertionError("missing config should be reported before auth or suite resolution")

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(["run", "--suite", "commerce-support"])

    assert exit_code == 1
    error = capsys.readouterr().err
    assert "config file `wendell.toml` was not found" in error
    assert "wendell suites configure --suite commerce-support --config wendell.toml" in error


def test_cli_doctor_reports_missing_config_and_credentials(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    monkeypatch.delenv("WENDELL_INKPASS_API_KEY", raising=False)

    exit_code = main(["doctor", "--config", str(tmp_path / "missing.toml")])

    assert exit_code == 1
    output = capsys.readouterr().out


def test_cli_doctor_passes_with_env_credentials_and_agent_command(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    monkeypatch.setenv("WENDELL_INKPASS_API_KEY", "inkpass-key")
    adapter_path = tmp_path / "scripts" / "wendell_agent_adapter.py"
    adapter_path.parent.mkdir()
    adapter_path.write_text("print('{}')\n", encoding="utf-8")
    config_path = tmp_path / "wendell.toml"
    config_path.write_text(
        "\n".join(
            [
                'project = "customer-service-agent"',
                f'agent_command = "{sys.executable} scripts/wendell_agent_adapter.py"',
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(["doctor", "--config", str(config_path)])

    assert exit_code == 0
    output = capsys.readouterr().out


def test_cli_doctor_rejects_generated_adapter_until_wired(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    monkeypatch.setenv("WENDELL_INKPASS_API_KEY", "inkpass-key")
    monkeypatch.delenv("WENDELL_APP_AGENT_COMMAND", raising=False)
    adapter_path = tmp_path / "scripts" / "wendell_agent_adapter.py"
    adapter_path.parent.mkdir()
    adapter_path.write_text(_agent_adapter_template(), encoding="utf-8")
    config_path = tmp_path / "wendell.toml"
    config_path.write_text(
        "\n".join(
            [
                'project = "customer-service-agent"',
                f'agent_command = "{sys.executable} scripts/wendell_agent_adapter.py"',
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(["doctor", "--config", str(config_path)])

    assert exit_code == 1
    output = capsys.readouterr().out


def test_cli_doctor_allows_generated_adapter_when_delegation_command_is_set(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    monkeypatch.setenv("WENDELL_INKPASS_API_KEY", "inkpass-key")
    monkeypatch.setenv("WENDELL_APP_AGENT_COMMAND", f"{sys.executable} scripts/run_my_agent.py")
    adapter_path = tmp_path / "scripts" / "wendell_agent_adapter.py"
    agent_path = tmp_path / "scripts" / "run_my_agent.py"
    adapter_path.parent.mkdir()
    adapter_path.write_text(_agent_adapter_template(), encoding="utf-8")
    agent_path.write_text("print('{}')\n", encoding="utf-8")
    config_path = tmp_path / "wendell.toml"
    config_path.write_text(
        "\n".join(
            [
                'project = "customer-service-agent"',
                f'agent_command = "{sys.executable} scripts/wendell_agent_adapter.py"',
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(["doctor", "--config", str(config_path)])

    assert exit_code == 0
    output = capsys.readouterr().out


def test_cli_doctor_rejects_missing_relative_agent_script(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    monkeypatch.setenv("WENDELL_INKPASS_API_KEY", "inkpass-key")
    config_path = tmp_path / "wendell.toml"
    config_path.write_text(
        "\n".join(
            [
                'project = "customer-service-agent"',
                f'agent_command = "{sys.executable} scripts/missing_agent.py"',
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(["doctor", "--config", str(config_path)])

    assert exit_code == 1
    output = capsys.readouterr().out


def test_cli_doctor_validates_identity_without_printing_secret(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    monkeypatch.setenv("WENDELL_INKPASS_API_KEY", "secret-runner-key")
    (tmp_path / "agent.py").write_text("print('{}')\n", encoding="utf-8")
    config_path = tmp_path / "wendell.toml"
    config_path.write_text(
        f'project = "customer-service-agent"\nagent_command = "{sys.executable} agent.py"\n',
        encoding="utf-8",
    )

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            assert api_url == "https://api.wendellai.com"
            assert api_key == "secret-runner-key"

        def get_identity(self) -> dict:
            return {
                "auth_type": "api_key",
                "external_org_id": "org_123",
                "external_user_id": "runner-user",
                "runner_id": "runner_123",
            }

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(["doctor", "--config", str(config_path), "--validate", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pass"
    assert any(check["name"] == "api_identity" and check["ok"] for check in payload["checks"])
    assert "secret-runner-key" not in json.dumps(payload)


def test_remote_runtime_requires_real_agent_command() -> None:
    with pytest.raises(ValueError, match="agent_command"):
        _call_remote_runtime_agent(RunnerConfig(project="customer-service-agent"), {"available_tools": []})


def test_cli_login_validate_rejects_credentials_without_storing(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            pass

        def get_identity(self) -> dict:
            raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(["login", "--api-key", "bad-key", "--api-url", "http://127.0.0.1:8765", "--validate"])

    assert exit_code == 1
    assert "credentials were rejected" in capsys.readouterr().err
    assert not (tmp_path / "wendell-config" / "credentials.json").exists()


def test_cli_park_subcommands_are_removed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))

    with pytest.raises(SystemExit) as exc:
        main(["park", "join", "--agent", "Pi Guide"])
    assert exc.value.code == 2


def test_cli_register_starts_runner_session_without_park_join(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    monkeypatch.setattr("sys.stdin", type("Input", (), {"read": lambda self: "correct horse battery staple\n"})())

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            self.api_url = api_url
            self.api_key = api_key

        def start_runner_session(self, payload: dict) -> dict:
            assert payload["computer_name"]
            return {"runner_session_id": "rsess_123"}

        def register_cli(self, payload: dict) -> dict:
            assert payload["runner_session_id"] == "rsess_123"
            assert payload["email"] == "person@example.com"
            assert payload["password"] == "correct horse battery staple"
            return {"api_key": "wpk_live_fake", "runner": {"runner_id": "runner_123"}}

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    exit_code = main(
        [
            "register",
            "--api-url",
            "http://127.0.0.1:8765",
            "--email",
            "person@example.com",
            "--agent",
            "My Agent",
            "--provider",
            "openrouter",
            "--model",
            "qwen/qwen3-coder",
            "--password-stdin",
        ]
    )

    assert exit_code == 0


def test_remote_upload_uses_remote_runtime_when_no_local_input(monkeypatch, tmp_path: Path) -> None:
    captured = {}
    agent_script = tmp_path / "agent.py"
    agent_script.write_text(
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "assert payload['schema_version'] == 'wendell.agent_input.v1'\n"
        "assert 'rubric' not in payload\n"
        "assert 'success_criteria' not in json.dumps(payload)\n"
        "assert 'failure_criteria' not in json.dumps(payload)\n"
        "assert 'hidden_facts' not in json.dumps(payload)\n"
        "assert 'expected_answer' not in json.dumps(payload)\n"
        "assert 'answer_key' not in json.dumps(payload)\n"
        "assert 'oracle_answer' not in json.dumps(payload)\n"
        "assert 'scoring_criteria' not in json.dumps(payload)\n"
        "assert 'evaluation_notes' not in json.dumps(payload)\n"
        "assert 'source_lineage' not in json.dumps(payload)\n"
        "assert 'step_id' not in json.dumps(payload.get('available_tools', []))\n"
        "assert 'required' not in json.dumps(payload.get('available_tools', []))\n"
        "assert 'assertions' not in json.dumps(payload.get('available_tools', []))\n"
        "assert 'case' in payload\n"
        "print(json.dumps({\n"
        "    'message': 'handled with Bearer SECRET_TOKEN_1234567890 and sk-testsecret1234567890',\n"
        "    'tool_calls': [{'name': payload['available_tools'][0]['name'], 'args': {'record_id': 'case_123', 'authorization': 'Bearer SECRET_TOKEN_1234567890', 'nested': {'api_key': 'wpk_live_secret1234567890'}}}],\n"
        "    'metrics': {'source': 'test-agent', 'openai_api_key': 'sk-testsecret1234567890'},\n"
        "}))\n",
        encoding="utf-8",
    )

    class FakeRemoteClient:
        @classmethod
        def from_env(cls, api_url: str, api_key_env: str):
            captured["api_url"] = api_url
            captured["api_key_env"] = api_key_env
            return cls()

        def create_run(self, payload: dict) -> dict:
            captured["create_run"] = payload
            return {"run_id": "run_remote", "url": "/runs/run_remote"}

        def get_run(self, run_id: str) -> dict:
            return {"world_version_id": "wver_remote", "scenario_pack_id": "spack_remote"}

        def get_run_work(self, run_id: str) -> dict:
            captured["work_count"] = captured.get("work_count", 0) + 1
            if captured["work_count"] > 1:
                return {"run_id": run_id, "done": True}
            return {
                "run_id": run_id,
                "done": False,
                "scenario_execution": {"id": "sexec_a", "scenario_key": "scenario_a"},
                "scenario": {
                    "id": "scenario_a",
                    "success_criteria": ["secret expected behavior"],
                    "failure_criteria": ["secret failed behavior"],
                    "hidden_facts": {"secret": True},
                    "source_lineage": {"trace_id": "secret_trace"},
                    "nested": {
                        "expected_answer": "secret expected answer",
                        "safe_context": "visible context",
                    },
                },
                "rubric": {
                    "success_criteria": ["secret expected behavior"],
                    "failure_criteria": ["secret failed behavior"],
                    "hidden_facts": {"secret": True},
                },
                "transcript": [{"role": "customer", "text": "Help me."}],
                "case": {
                    "case_id": "case_123",
                    "request": "Help me.",
                    "answer_key": "secret answer key",
                    "nested": {"oracle_answer": "secret oracle", "public_field": "visible"},
                },
                "available_tools": [
                    {
                        "name": "crm.inspect",
                        "arguments": {"record_id": "str"},
                        "step_id": "inspect_request",
                        "required": ["record_evidence"],
                        "assertions": ["secret_assertion"],
                        "metadata": {
                            "scoring_criteria": ["secret scoring"],
                            "public_hint": "visible tool context",
                        },
                    }
                ],
            }

        def submit_agent_turn(self, run_id: str, scenario_execution_id: str, payload: dict) -> dict:
            captured["turn"] = (run_id, scenario_execution_id, payload)
            return {
                "trajectory_id": "traj_a",
                "scenario_execution_id": scenario_execution_id,
                "status": "completed",
                "scenario_score": {
                    "id": "sscore_a",
                    "overall_score": 0.42,
                    "critical_failure_count": 1,
                    "critical_failures": [{"message": "case_note_missing"}],
                    "metric_scores": [{"metric": "workflow_alignment", "value": 0.5, "evidence": {}}],
                    "step_results": {
                        "inspect_request": {"status": "completed", "score": 1.0},
                        "record_evidence": {"status": "not_attempted", "score": 0.0},
                    },
                    "assertion_results": [
                        {
                            "rule_id": "playbook_policy_1",
                            "assertion_id": "assert_required_sequence",
                            "status": "failed",
                            "message": "missing expected event",
                        }
                    ],
                    "missed_expectations": ["record_evidence"],
                    "improvement_prompts": ["Record evidence before completing the workflow."],
                },
            }

        def complete_run(self, run_id: str) -> dict:
            return {"status": "completed"}

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    suite, remote = _run_suite_with_remote_upload(
        RunnerConfig(
            project="customer-service-agent",
            api_url="https://api.example",
            world="commerce-support-ops",
            world_version="1",
            scenario_pack="commerce-support-benchmark",
            scenario_pack_version="1",
            agent="careful",
            agent_command=f"{sys.executable} {agent_script}",
        )
    )

    assert suite.project == "customer-service-agent"
    assert suite.metadata["source"] == "remote_runtime"
    assert suite.suite_score == 0.42
    result = suite.scenario_results[0]
    assert result.trace_id == "traj_a"
    assert result.critical_failures == ("case_note_missing",)
    assert result.step_statuses == {"inspect_request": "completed", "record_evidence": "not_attempted"}
    assert result.dimensions == {"workflow_alignment": 0.5}
    assert result.assertion_results[0]["rule_id"] == "playbook_policy_1"
    assert result.missed_expectations == ("record_evidence",)
    assert result.improvement_prompts == ("Record evidence before completing the workflow.",)
    assert captured["turn"][1] == "sexec_a"
    uploaded_turn = captured["turn"][2]
    assert uploaded_turn["tool_calls"] == [
        {
            "name": "crm.inspect",
            "args": {
                "record_id": "case_123",
                "authorization": "[REDACTED]",
                "nested": {"api_key": "[REDACTED]"},
            },
        }
    ]
    assert uploaded_turn["metrics"]["openai_api_key"] == "[REDACTED]"
    assert uploaded_turn["message"].count("[REDACTED]") >= 2
    uploaded_blob = json.dumps(uploaded_turn)
    assert "SECRET_TOKEN" not in uploaded_blob
    assert "sk-testsecret" not in uploaded_blob
    assert remote["scenario_pack_id"] == "spack_remote"
    assert remote["world_version_id"] == "wver_remote"
    assert remote["uploaded_scenarios"] == 1
    assert remote["runtime"] == "remote"


def test_trace_payload_redacts_local_trace_secrets() -> None:
    payload = _trace_payload(
        {
            "run_id": "trace_1",
            "agent_name": "agent",
            "metadata": {"api_key": "wpk_live_secret1234567890", "safe": "visible"},
            "events": [
                {
                    "index": 1,
                    "type": "agent",
                    "source": "adapter",
                    "message": "Using Bearer SECRET_TOKEN_1234567890",
                    "payload": {"authorization": "Bearer SECRET_TOKEN_1234567890", "record_id": "case_123"},
                    "tool_calls": [
                        {
                            "name": "crm.inspect",
                            "args": {"password": "secret-password", "record_id": "case_123"},
                        }
                    ],
                    "observation": {"cookie": "session=secret", "status": "ok"},
                    "evaluation": {"note": "safe"},
                    "metrics": {"client_secret": "top-secret", "latency_ms": 1},
                }
            ],
        },
        "sexec_1",
    )

    blob = json.dumps(payload)
    assert "SECRET_TOKEN" not in blob
    assert "secret-password" not in blob
    assert "top-secret" not in blob
    assert payload["metadata"]["api_key"] == "[REDACTED]"
    assert payload["metadata"]["safe"] == "visible"
    assert payload["events"][0]["payload"]["record_id"] == "case_123"
    assert payload["events"][0]["tool_calls"][0]["args"]["record_id"] == "case_123"


def test_remote_upload_uses_production_api_by_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WENDELL_INKPASS_API_KEY", "inkpass-key")
    monkeypatch.delenv("WENDELL_API_URL", raising=False)
    captured = {}
    agent_script = tmp_path / "agent.py"
    agent_script.write_text(
        "import json, sys\n"
        "json.load(sys.stdin)\n"
        "print(json.dumps({'message': 'handled', 'tool_calls': []}))\n",
        encoding="utf-8",
    )

    class FakeRemoteClient:
        @classmethod
        def from_env(cls, api_url: str, api_key_env: str):
            captured["api_url"] = api_url
            captured["api_key_env"] = api_key_env
            return cls()

        def create_run(self, payload: dict) -> dict:
            return {"run_id": "run_remote", "url": "/runs/run_remote"}

        def get_run(self, run_id: str) -> dict:
            return {"world_version_id": "wver_remote", "scenario_pack_id": "spack_remote"}

        def get_run_work(self, run_id: str) -> dict:
            if captured.get("work_done"):
                return {"run_id": run_id, "done": True}
            captured["work_done"] = True
            return {
                "run_id": run_id,
                "done": False,
                "scenario_execution": {"id": "sexec_a", "scenario_key": "scenario_a"},
                "scenario": {"id": "scenario_a"},
                "transcript": [{"role": "customer", "text": "Help me."}],
                "available_tools": [],
            }

        def submit_agent_turn(self, run_id: str, scenario_execution_id: str, payload: dict) -> dict:
            return {
                "trajectory_id": "traj_a",
                "scenario_execution_id": scenario_execution_id,
                "status": "completed",
                "scenario_score": {"id": "sscore_a", "overall_score": 1.0, "critical_failure_count": 0},
            }

        def complete_run(self, run_id: str) -> dict:
            return {"status": "completed"}

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    _run_suite_with_remote_upload(
        RunnerConfig(
            project="customer-service-agent",
            world="commerce-support-ops",
            world_version="1",
            scenario_pack="commerce-support-benchmark",
            scenario_pack_version="1",
            agent_command=f"{sys.executable} {agent_script}",
        )
    )

    assert captured["api_url"] == "https://api.wendellai.com"
    assert captured["api_key_env"] == "WENDELL_INKPASS_API_KEY"


def test_remote_runtime_stops_when_server_never_marks_work_done(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WENDELL_REMOTE_RUNTIME_MAX_WORK_ITEMS", "2")
    agent_script = tmp_path / "agent.py"
    agent_script.write_text(
        "import json, sys\n"
        "json.load(sys.stdin)\n"
        "print(json.dumps({'message': 'handled', 'tool_calls': []}))\n",
        encoding="utf-8",
    )
    captured = {"complete_called": False}

    class FakeRemoteClient:
        @classmethod
        def from_env(cls, api_url: str, api_key_env: str):
            return cls()

        def create_run(self, payload: dict) -> dict:
            return {"run_id": "run_remote", "url": "/runs/run_remote"}

        def get_run(self, run_id: str) -> dict:
            return {"world_version_id": "wver_remote", "scenario_pack_id": "spack_remote"}

        def get_run_work(self, run_id: str) -> dict:
            return {
                "run_id": run_id,
                "done": False,
                "scenario_execution": {"id": "sexec_a", "scenario_key": "scenario_a"},
                "scenario": {"id": "scenario_a"},
                "transcript": [{"role": "customer", "text": "Help me."}],
                "available_tools": [],
            }

        def submit_agent_turn(self, run_id: str, scenario_execution_id: str, payload: dict) -> dict:
            return {
                "trajectory_id": "traj_a",
                "scenario_execution_id": scenario_execution_id,
                "status": "completed",
                "scenario_score": {"id": "sscore_a", "overall_score": 1.0, "critical_failure_count": 0},
            }

        def complete_run(self, run_id: str) -> dict:
            captured["complete_called"] = True
            return {"status": "completed"}

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)

    with pytest.raises(RuntimeError, match="remote runtime exceeded 2 work item"):
        _run_suite_with_remote_upload(
            RunnerConfig(
                project="customer-service-agent",
                world="commerce-support-ops",
                world_version="1",
                scenario_pack="commerce-support-benchmark",
                scenario_pack_version="1",
                agent_command=f"{sys.executable} {agent_script}",
            )
        )

    assert captured["complete_called"] is False


def test_remote_runtime_uses_configured_agent_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")

        class Completed:
            returncode = 0
            stdout = '{"message":"handled","tool_calls":[]}'
            stderr = ""

        return Completed()

    monkeypatch.setattr("wendell_ci.cli.subprocess.run", fake_run)

    result = _call_remote_runtime_agent(
        RunnerConfig(
            project="customer-service-agent",
            agent_command="python agent.py",
            agent_timeout_seconds=345,
        ),
        {"scenario": {"id": "scenario_a"}, "available_tools": []},
    )

    assert result["message"] == "handled"
    assert captured["timeout"] == 345


def test_remote_runtime_invokes_agent_command_without_shell(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args[0]
        captured["shell"] = kwargs.get("shell")

        class Completed:
            returncode = 0
            stdout = '{"message":"handled","tool_calls":[]}'
            stderr = ""

        return Completed()

    monkeypatch.setattr("wendell_ci.cli.subprocess.run", fake_run)

    result = _call_remote_runtime_agent(
        RunnerConfig(
            project="customer-service-agent",
            agent_command="python scripts/agent.py --name 'Support Agent'",
        ),
        {"scenario": {"id": "scenario_a"}, "available_tools": []},
    )

    assert result["message"] == "handled"
    assert captured["args"] == ["python", "scripts/agent.py", "--name", "Support Agent"]
    assert captured["shell"] is None


def test_agent_command_args_rejects_invalid_shell_quoting() -> None:
    with pytest.raises(ValueError, match="No closing quotation"):
        _agent_command_args("python scripts/agent.py 'unterminated")


def test_remote_runtime_reports_invalid_agent_command_as_agent_error() -> None:
    result = _call_remote_runtime_agent(
        RunnerConfig(project="customer-service-agent", agent_command="python scripts/agent.py 'unterminated"),
        {"scenario": {"id": "scenario_a"}, "available_tools": []},
    )

    assert result["tool_calls"] == []
    assert result["metrics"] == {"agent_error": True, "adapter_contract_error": "agent_command is invalid"}
    assert result["message"].startswith("[external agent command invalid:")


def test_remote_runtime_reports_agent_timeout_as_agent_error(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr("wendell_ci.cli.subprocess.run", fake_run)

    result = _call_remote_runtime_agent(
        RunnerConfig(project="customer-service-agent", agent_command="python agent.py", agent_timeout_seconds=5),
        {"scenario": {"id": "scenario_a"}, "available_tools": []},
    )

    assert result["message"] == "[external agent timed out after 5s]"
    assert result["tool_calls"] == []
    assert result["metrics"] == {"agent_error": True, "agent_timeout": True}


def test_remote_runtime_reports_malformed_adapter_fields_as_agent_error(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        class Completed:
            returncode = 0
            stdout = '{"message":"handled","tool_calls":null,"metrics":[]}'
            stderr = ""

        return Completed()

    monkeypatch.setattr("wendell_ci.cli.subprocess.run", fake_run)

    result = _call_remote_runtime_agent(
        RunnerConfig(project="customer-service-agent", agent_command="python agent.py"),
        {"scenario": {"id": "scenario_a"}, "available_tools": []},
    )

    assert result["message"] == "handled"
    assert result["tool_calls"] == []
    assert result["metrics"] == {"agent_error": True, "adapter_contract_error": "tool_calls must be a list"}


def test_remote_runtime_reports_non_json_adapter_stdout_as_agent_error(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        class Completed:
            returncode = 0
            stdout = "plain text agent output"
            stderr = ""

        return Completed()

    monkeypatch.setattr("wendell_ci.cli.subprocess.run", fake_run)

    result = _call_remote_runtime_agent(
        RunnerConfig(project="customer-service-agent", agent_command="python agent.py"),
        {"scenario": {"id": "scenario_a"}, "available_tools": []},
    )

    assert result["message"] == "plain text agent output"
    assert result["tool_calls"] == []
    assert result["metrics"] == {"agent_error": True, "adapter_contract_error": "stdout must be a JSON object"}


def test_remote_runtime_reports_non_object_adapter_stdout_as_agent_error(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        class Completed:
            returncode = 0
            stdout = '["not", "an", "object"]'
            stderr = ""

        return Completed()

    monkeypatch.setattr("wendell_ci.cli.subprocess.run", fake_run)

    result = _call_remote_runtime_agent(
        RunnerConfig(project="customer-service-agent", agent_command="python agent.py"),
        {"scenario": {"id": "scenario_a"}, "available_tools": []},
    )

    assert result["message"] == "['not', 'an', 'object']"
    assert result["tool_calls"] == []
    assert result["metrics"] == {"agent_error": True, "adapter_contract_error": "stdout must be a JSON object"}


def test_remote_runtime_reports_missing_message_as_agent_error(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        class Completed:
            returncode = 0
            stdout = '{"tool_calls":[]}'
            stderr = ""

        return Completed()

    monkeypatch.setattr("wendell_ci.cli.subprocess.run", fake_run)

    result = _call_remote_runtime_agent(
        RunnerConfig(project="customer-service-agent", agent_command="python agent.py"),
        {"scenario": {"id": "scenario_a"}, "available_tools": []},
    )

    assert result["message"] == ""
    assert result["tool_calls"] == []
    assert result["metrics"] == {"agent_error": True, "adapter_contract_error": "message must be a string"}


def test_cli_runs_watch_reads_run_status(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            assert api_url == "http://127.0.0.1:8765"
            assert api_key == "inkpass-key"

        def get_run(self, run_id: str) -> dict:
            assert run_id == "run_123"
            return {
                "status": "completed",
                "latest_score": 1.0,
                "environment": {"runner_version": "wendell/0.1.32", "ci_provider": "github_actions", "ci_run_id": "123456"},
                "external_ci_ref": {
                    "provider": "github_actions",
                    "repository": "acme/support-agent",
                    "workflow": "Wendell CI",
                    "run_id": "123456",
                    "run_url": "https://github.com/acme/support-agent/actions/runs/123456",
                    "head_ref": "feature/refund-agent",
                    "sha": "abc123def456",
                },
            }

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0

    exit_code = main(["runs", "watch", "run_123"])

    assert exit_code == 0
    output = capsys.readouterr().out


def test_cli_runs_report_reads_private_run_report(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            assert api_url == "http://127.0.0.1:8765"
            assert api_key == "inkpass-key"

        def get_run_report(self, run_id: str) -> dict:
            assert run_id == "run_123"
            return {"capability_report": {"overall_score": 0.94, "critical_failure_count": 0}}

        def create_cli_session_link(self, payload: dict) -> dict:
            assert payload == {"next": "/dashboard/runs/run_123", "run_id": "run_123"}
            return {"url": "https://wendell.example/dashboard/runs/run_123", "expires_in_seconds": 300}

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0

    exit_code = main(["runs", "report", "run_123"])

    assert exit_code == 0
    output = capsys.readouterr().out


def test_cli_runs_report_json_includes_private_report_link(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            assert api_url == "http://127.0.0.1:8765"
            assert api_key == "inkpass-key"

        def get_run_report(self, run_id: str) -> dict:
            assert run_id == "run_123"
            return {"capability_report": {"overall_score": 0.94, "critical_failure_count": 0}}

        def create_cli_session_link(self, payload: dict) -> dict:
            assert payload == {"next": "/dashboard/runs/run_123", "run_id": "run_123"}
            return {"url": "https://wendell.example/dashboard/runs/run_123", "expires_in_seconds": 300}

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0
    capsys.readouterr()

    exit_code = main(["runs", "report", "run_123", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["capability_report"]["overall_score"] == 0.94
    assert payload["private_report_url"] == "https://wendell.example/dashboard/runs/run_123"
    assert payload["private_report_url_expires_in_seconds"] == 300


def test_cli_runs_report_appends_github_step_summary(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WENDELL_CONFIG_HOME", str(tmp_path / "wendell-config"))
    summary_path = tmp_path / "github-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))

    class FakeRemoteClient:
        def __init__(self, api_url: str, api_key: str | None = None) -> None:
            assert api_url == "http://127.0.0.1:8765"
            assert api_key == "inkpass-key"

        def get_run_report(self, run_id: str) -> dict:
            assert run_id == "run_123"
            return {"capability_report": {"overall_score": 0.94, "critical_failure_count": 0}}

        def create_cli_session_link(self, payload: dict) -> dict:
            assert payload == {"next": "/dashboard/runs/run_123", "run_id": "run_123"}
            return {"url": "https://wendell.example/dashboard/runs/run_123", "expires_in_seconds": 300}

    monkeypatch.setattr("wendell_ci.cli.RemoteWendellClient", FakeRemoteClient)
    assert main(["login", "--api-key", "inkpass-key", "--api-url", "http://127.0.0.1:8765"]) == 0
    capsys.readouterr()

    exit_code = main(["runs", "report", "run_123", "--github-summary"])

    assert exit_code == 0
    summary = summary_path.read_text(encoding="utf-8")
    assert "run_123" in summary
    assert "https://wendell.example/dashboard/runs/run_123" in summary
