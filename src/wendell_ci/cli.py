from __future__ import annotations

import argparse
from dataclasses import replace
import getpass
from importlib import metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
from typing import Any, Sequence

from .auth import StoredCredentials, credentials_path, delete_credentials, load_credentials, store_credentials
from .config import RunnerConfig
from .gates import evaluate_gates
from .models import ScenarioResult, SuiteResult
from .remote_client import DEFAULT_API_URL, RemoteWendellClient
from .worldsim_client import LocalWorldsimClient, WorldsimUnavailableError


def _package_version() -> str:
    try:
        return metadata.version("wendell")
    except metadata.PackageNotFoundError:
        return "0.0.0+unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description="Wendell CLI for Playbook-backed agent regression tests.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Primary commands:\n"
            "  wendell register                 Register this runner.\n"
            "  wendell playbook create ...      Create a Playbook draft.\n"
            "  wendell playbook review ...      Review extracted Playbook questions.\n"
            "  wendell playbook approve ...     Approve and generate a hosted suite.\n"
            "  wendell suites publish ...       Publish the generated suite.\n"
            "  wendell suites configure ...     Write wendell.toml and an adapter template.\n"
            "  wendell run --suite ...          Run your agent against a hosted suite.\n\n"
            "Diagnostics:\n"
            "  wendell doctor                   Check local/CI setup before running a suite.\n\n"
            "Quickstart: https://docs.wendellai.com/quickstart"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_package_version()}")
    parser.add_argument("--config", default="wendell.toml", help="Path to Wendell config.")
    parser.add_argument("--project", help="Override project from config.")
    parser.add_argument("--api-url", help="Override Wendell API URL from config.")
    parser.add_argument("--world", help=argparse.SUPPRESS)
    parser.add_argument("--world-version", help=argparse.SUPPRESS)
    parser.add_argument("--scenario-pack", help=argparse.SUPPRESS)
    parser.add_argument("--scenario-pack-version", help=argparse.SUPPRESS)
    parser.add_argument("--agent", help="Override agent name from config.")
    parser.add_argument("--agent-command", help="Override external agent command from config.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "playbook":
        return _playbook_main(argv[1:])
    if argv and argv[0] == "runs":
        return _runs_main(argv[1:])
    if argv and argv[0] == "suites":
        return _suites_main(argv[1:])
    if argv and argv[0] == "run":
        return _suite_run_main(argv[1:])
    if argv and argv[0] == "test":
        return _test_main(argv[1:])
    if argv and argv[0] == "init":
        return _init_main(argv[1:])
    if argv and argv[0] == "register":
        return _register_main(argv[1:])
    if argv and argv[0] == "doctor":
        return _doctor_main(argv[1:])
    if argv and argv[0] in {"login", "logout", "whoami", "auth"}:
        return _auth_main(argv)
    args = build_parser().parse_args(argv)
    config_path = Path(args.config)
    if config_path.exists():
        config = RunnerConfig.from_file(config_path)
    else:
        print(
            f"Wendell error: config file `{config_path}` was not found. "
            "Run `wendell suites configure --suite <suite-slug>` to configure a hosted suite.",
            file=sys.stderr,
        )
        return 2
    config = _apply_overrides(config, args)
    _load_env_files(config_path, config)

    remote_payload = None
    try:
        if _should_upload_traces(config):
            suite, remote_payload = _run_suite_with_remote_upload(config)
        else:
            suite = _run_suite(config)
    except (WorldsimUnavailableError, ValueError) as exc:
        print(f"Wendell error: {exc}", file=sys.stderr)
        return 2
    decision = evaluate_gates(suite, config.gates, mode=config.mode)
    payload = {
        "decision": decision.status,
        "exit_code": decision.exit_code,
        "advisory": not decision.blocking,
        "reasons": list(decision.reasons),
        "suite": suite.to_dict(),
    }
    if remote_payload is not None:
        payload["remote"] = remote_payload

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Wendell: {decision.status}")
        print(f"Mode: {config.mode}")
        print(f"Suite score: {suite.suite_score:.2f}")
        print(f"Critical failures: {suite.critical_failure_count}")
        for reason in decision.reasons:
            print(f"- {reason}")
    return decision.exit_code


def _init_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{Path(sys.argv[0]).name} init",
        description=(
            "Legacy local/offline initializer for Wendell development. "
            "Production projects should use `wendell suites configure --suite <suite-slug>`."
        ),
    )
    parser.add_argument("--project", default=None, help="Project slug. Defaults to the current directory name.")
    parser.add_argument("--name", default=None, help="Suite display name. Defaults to '<project> Agent Regression'.")
    parser.add_argument("--workflow-summary", default="Evaluate this production agent against the approved Playbook.", help="Workflow summary for the generated suite.")
    parser.add_argument("--domain", default="production_agent", help="Workflow domain.")
    parser.add_argument("--playbook", default="playbook.md", help="Playbook markdown path to create or use.")
    parser.add_argument("--suite", default=".wendell/suite.json", help="Generated suite JSON path.")
    parser.add_argument("--config", default="wendell.toml", help="Generated Wendell config path.")
    parser.add_argument("--adapter", default="scripts/wendell_agent_adapter.py", help="Agent adapter template path.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing generated files.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args(list(argv))
    try:
        result = _initialize_local_project(args)
    except Exception as exc:
        print(f"Wendell init failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Initialized Wendell project: {result['project']}")
        print(f"Playbook: {result['playbook']}")
        print(f"Suite: {result['suite']}")
        print(f"Adapter: {result['adapter']}")
        print(f"Config: {result['config']}")
        print(f"Run (legacy local/offline only): wendell test --config {result['config']}")
        print("Next for production: wendell suites configure --suite <suite-slug>")
    return 0


def _test_main(argv: Sequence[str]) -> int:
    parser = build_parser()
    parser.prog = f"{Path(sys.argv[0]).name} test"
    parser.description = "Run Wendell agent behavior tests from a local Wendell config."
    args = parser.parse_args(list(argv))
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Wendell test failed: config file `{config_path}` was not found.", file=sys.stderr)
        return 2
    try:
        config = RunnerConfig.from_file(config_path)
    except (OSError, ValueError) as exc:
        print(f"Wendell test failed: {exc}", file=sys.stderr)
        return 2
    config = _apply_overrides(config, args)
    if not config.agent_command:
        print("Wendell test failed: production tests require `agent_command` in the config file.", file=sys.stderr)
        return 2
    _load_env_files(config_path, config)
    try:
        if _should_upload_traces(config):
            suite, remote_payload = _run_suite_with_remote_upload(config)
        else:
            suite = _run_suite(config)
            remote_payload = None
    except (WorldsimUnavailableError, ValueError) as exc:
        print(f"Wendell test failed: {exc}", file=sys.stderr)
        return 2
    decision = evaluate_gates(suite, config.gates, mode=config.mode)
    payload = {
        "decision": decision.status,
        "exit_code": decision.exit_code,
        "advisory": not decision.blocking,
        "reasons": list(decision.reasons),
        "suite": suite.to_dict(),
    }
    if remote_payload is not None:
        payload["remote"] = remote_payload
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_test_result(decision, suite)
    return decision.exit_code


def _suites_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{Path(sys.argv[0]).name} suites", description="Inspect Wendell test suites.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    list_cmd = subcommands.add_parser("list", help="List accessible published test suites.")
    list_cmd.add_argument("--api-url", help="Override stored Wendell API URL.")
    list_cmd.add_argument("--json", action="store_true", help="Print JSON output.")
    show_cmd = subcommands.add_parser("show", help="Show a published test suite.")
    show_cmd.add_argument("suite", help="Published test suite slug.")
    show_cmd.add_argument("--api-url", help="Override stored Wendell API URL.")
    show_cmd.add_argument("--json", action="store_true", help="Print JSON output.")
    generate_cmd = subcommands.add_parser("generate", help="Generate a suite candidate from an approved Playbook draft.")
    generate_cmd.add_argument("--draft", required=True, help="Playbook draft id.")
    generate_cmd.add_argument("--api-url", help="Override stored Wendell API URL.")
    generate_cmd.add_argument("--json", action="store_true", help="Print JSON output.")
    publish_cmd = subcommands.add_parser("publish", help="Publish a generated suite candidate.")
    publish_cmd.add_argument("--draft", required=True, help="Playbook draft id.")
    publish_cmd.add_argument("--api-url", help="Override stored Wendell API URL.")
    publish_cmd.add_argument("--json", action="store_true", help="Print JSON output.")
    configure_cmd = subcommands.add_parser("configure", help="Write wendell.toml and an adapter template for a hosted suite.")
    configure_cmd.add_argument("--suite", required=True, help="Published test suite slug.")
    configure_cmd.add_argument("--project", help="Project slug to write into wendell.toml. Defaults to the suite slug.")
    configure_cmd.add_argument("--config", default="wendell.toml", help="Config path to write.")
    configure_cmd.add_argument("--adapter", default="scripts/wendell_agent_adapter.py", help="Adapter template path to write.")
    configure_cmd.add_argument("--agent-command", help="Agent command to write. Defaults to 'python <adapter>'.")
    configure_cmd.add_argument("--api-url", help="Override stored Wendell API URL.")
    configure_cmd.add_argument("--force", action="store_true", help="Overwrite existing config or adapter files.")
    configure_cmd.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args(list(argv))
    if args.command == "list":
        return _suites_list(args)
    if args.command == "show":
        return _suites_show(args)
    if args.command == "generate":
        return _suites_generate(args)
    if args.command == "publish":
        return _suites_publish(args)
    if args.command == "configure":
        return _suites_configure(args)
    parser.error(f"Unsupported suites command: {args.command}")
    return 2


def _suites_list(args: argparse.Namespace) -> int:
    client = _authenticated_client(args.api_url)
    if client is None:
        return 1
    try:
        payload = client.list_test_suites()
    except Exception as exc:
        print(f"Wendell suites list failed: {exc}", file=sys.stderr)
        return 1
    suites = payload.get("test_suites") if isinstance(payload.get("test_suites"), list) else []
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if not suites:
            print("No published test suites found.")
        for suite in suites:
            print(
                f"{suite.get('slug')}  "
                f"version={suite.get('world_version')}  "
                f"pack={suite.get('scenario_pack')}:{suite.get('scenario_pack_version')}"
            )
    return 0


def _suites_show(args: argparse.Namespace) -> int:
    client = _authenticated_client(args.api_url)
    if client is None:
        return 1
    try:
        payload = client.get_test_suite(args.suite)
    except Exception as exc:
        print(f"Wendell suites show failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        world = payload.get("world") if isinstance(payload.get("world"), dict) else {}
        version = payload.get("version") if isinstance(payload.get("version"), dict) else {}
        scenario_pack = payload.get("scenario_pack") if isinstance(payload.get("scenario_pack"), dict) else {}
        print(f"Suite: {world.get('slug') or args.suite}")
        if version.get("version"):
            print(f"Suite version: {version['version']}")
        if scenario_pack.get("slug"):
            print(f"Scenario pack: {scenario_pack['slug']}:{scenario_pack.get('version') or 'latest'}")
    return 0


def _suites_generate(args: argparse.Namespace) -> int:
    client = _authenticated_client(args.api_url)
    if client is None:
        return 1
    try:
        payload = client.generate_test_suite_candidate(args.draft)
    except Exception as exc:
        print(f"Wendell suites generate failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else {}
        latest_snapshot = draft.get("latest_snapshot") if isinstance(draft.get("latest_snapshot"), dict) else {}
        metadata = draft.get("metadata") if isinstance(draft.get("metadata"), dict) else {}
        snapshot_id = latest_snapshot.get("id") or metadata.get("latest_snapshot_id") or "created"
        print(f"Generated suite candidate: {snapshot_id}")
        print(f"Draft: {draft.get('id') or args.draft}")
        if draft.get("status"):
            print(f"Status: {draft['status']}")
        print(f"Next: wendell suites publish --draft {draft.get('id') or args.draft}")
    return 0


def _suites_publish(args: argparse.Namespace) -> int:
    client = _authenticated_client(args.api_url)
    if client is None:
        return 1
    try:
        payload = client.publish_test_suite_draft(args.draft)
    except Exception as exc:
        print(f"Wendell suites publish failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        world = payload.get("world") if isinstance(payload.get("world"), dict) else {}
        version = payload.get("version") if isinstance(payload.get("version"), dict) else {}
        scenario_pack = payload.get("scenario_pack") if isinstance(payload.get("scenario_pack"), dict) else {}
        suite_slug = world.get("slug") or "unknown"
        print(f"Published suite: {suite_slug}")
        if version.get("version"):
            print(f"Suite version: {version['version']}")
        if scenario_pack.get("slug"):
            print(f"Scenario pack: {scenario_pack['slug']}:{scenario_pack.get('version') or 'latest'}")
        if suite_slug != "unknown":
            print(f"Next: wendell suites configure --suite {suite_slug}")
            print(f"Run: wendell run --suite {suite_slug} --config wendell.toml")
    return 0


def _suites_configure(args: argparse.Namespace) -> int:
    client = _authenticated_client(args.api_url)
    if client is None:
        return 1
    try:
        suite = _resolve_suite_run_config(client, args.suite)
        if not suite["world"] or not suite["world_version"] or not suite["scenario_pack"] or not suite["scenario_pack_version"]:
            raise ValueError(f"suite `{args.suite}` is not bound to a ready scenario pack.")
        tool_contracts = _suite_tool_contracts(client, args.suite)
        config_path = Path(args.config)
        adapter_path = Path(args.adapter)
        manifest_path = config_path.parent / "wendell_tool_manifest.json"
        agent_command = args.agent_command or f"python {adapter_path.as_posix()}"
        for path in [config_path, adapter_path, manifest_path]:
            if path.exists() and not args.force:
                raise ValueError(f"`{path}` already exists. Re-run with --force to overwrite.")
        _write_hosted_suite_config(
            config_path,
            project=args.project or args.suite,
            agent_command=agent_command,
        )
        _write_text_file(manifest_path, _tool_manifest_template(args.suite, tool_contracts))
        _write_text_file(adapter_path, _agent_adapter_template(tool_contracts=tool_contracts))
        adapter_path.chmod(adapter_path.stat().st_mode | 0o111)
    except Exception as exc:
        print(f"Wendell suites configure failed: {exc}", file=sys.stderr)
        return 1
    result = {
        "suite": args.suite,
        "project": args.project or args.suite,
        "config": str(config_path),
        "adapter": str(adapter_path),
        "tool_manifest": str(manifest_path),
        "agent_command": agent_command,
        "world": suite["world"],
        "world_version": suite["world_version"],
        "scenario_pack": suite["scenario_pack"],
        "scenario_pack_version": suite["scenario_pack_version"],
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Configured hosted suite: {args.suite}")
        print(f"Config: {config_path}")
        print(f"Adapter: {adapter_path}")
        print(f"Tool manifest: {manifest_path}")
        print(f"Next: set WENDELL_APP_AGENT_COMMAND or replace {adapter_path}")
        print(f"Run: wendell run --suite {args.suite} --config {config_path}")
    return 0


def _playbook_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{Path(sys.argv[0]).name} playbook",
        description="Author Wendell Playbooks from business source material.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True, metavar="{create,review,apply,approve}")

    create = subcommands.add_parser("create", help="Create a Playbook draft from source material.")
    create.add_argument("--name", required=True, help="Playbook or test suite name.")
    create.add_argument("--workflow-summary", required=True, help="Short description of the workflow under test.")
    create.add_argument("--source", action="append", default=[], help="Source file to ingest. Can be passed multiple times.")
    create.add_argument("--source-text", help="Inline source material.")
    create.add_argument("--project-ref", default="default", help="Customer/project reference.")
    create.add_argument("--org-ref", help="Organization reference used when auth is unscoped.")
    create.add_argument("--created-by", default="wendell-cli", help="Human or agent creating the draft.")
    create.add_argument("--domain", default="customer_support", help="Workflow domain.")
    create.add_argument("--api-url", help="Override stored Wendell API URL.")
    create.add_argument("--extract", action="store_true", help="Immediately generate the first Playbook summary.")
    create.add_argument("--json", action="store_true", help="Print JSON output.")

    compile_cmd = subcommands.add_parser(
        "compile",
        help=argparse.SUPPRESS,
        description=(
            "Legacy local/offline compiler for Wendell development. "
            "Production projects should use hosted Playbook review and suite generation."
        ),
    )
    compile_cmd.add_argument("--source", required=True, help="Playbook source file to compile.")
    compile_cmd.add_argument("--name", required=True, help="Suite name.")
    compile_cmd.add_argument("--workflow-summary", required=True, help="Short description of the workflow under test.")
    compile_cmd.add_argument("--domain", default="customer_support", help="Workflow domain.")
    compile_cmd.add_argument("--output", default="wendell-suite.json", help="Output customer-input suite JSON path.")
    compile_cmd.add_argument("--config-output", help="Optional Wendell config path to write.")
    compile_cmd.add_argument("--agent-command", help="Agent command to include in --config-output.")
    compile_cmd.add_argument("--json", action="store_true", help="Print JSON output.")
    subcommands._choices_actions = [  # type: ignore[attr-defined]
        action for action in subcommands._choices_actions if action.dest != "compile"  # type: ignore[attr-defined]
    ]

    review = subcommands.add_parser("review", help="Review extracted Playbook primitives and questions.")
    review.add_argument("playbook_ref", help="Playbook draft id or Playbook summary id.")
    review.add_argument("--api-url", help="Override stored Wendell API URL.")
    review.add_argument("--json", action="store_true", help="Print JSON output.")

    apply = subcommands.add_parser("apply", help="Apply a patch-style review file to a Playbook summary.")
    apply.add_argument("playbook_ref", help="Playbook draft id or Playbook summary id.")
    apply.add_argument("--file", required=True, help="JSON or YAML review file.")
    apply.add_argument("--api-url", help="Override stored Wendell API URL.")
    apply.add_argument("--json", action="store_true", help="Print JSON output.")

    approve = subcommands.add_parser("approve", help="Approve the latest Playbook summary.")
    approve.add_argument("playbook_ref", help="Playbook draft id or Playbook summary id.")
    approve.add_argument("--reviewer", help="Reviewer reference to store with the approval.")
    approve.add_argument("--generate-suite", action="store_true", help="Generate a suite candidate after approval.")
    approve.add_argument("--api-url", help="Override stored Wendell API URL.")
    approve.add_argument("--json", action="store_true", help="Print JSON output.")

    args = parser.parse_args(list(argv))
    if args.command == "create":
        return _playbook_create(args)
    if args.command == "compile":
        return _playbook_compile(args)
    if args.command == "review":
        return _playbook_review(args)
    if args.command == "apply":
        return _playbook_apply(args)
    if args.command == "approve":
        return _playbook_approve(args)
    parser.error(f"Unsupported playbook command: {args.command}")
    return 2


def _playbook_create(args: argparse.Namespace) -> int:
    client = _authenticated_client(args.api_url)
    if client is None:
        return 1
    try:
        source_material = _read_playbook_sources([Path(item) for item in args.source], args.source_text)
    except OSError as exc:
        print(f"Wendell playbook create failed: {exc}", file=sys.stderr)
        return 1
    payload = {
        "name": args.name,
        "workflow_summary": args.workflow_summary,
        "project_ref": args.project_ref,
        "created_by": args.created_by,
        "domain": args.domain,
        "source_material": source_material or None,
        "guided_sections": {},
    }
    if args.org_ref:
        payload["org_ref"] = args.org_ref
    try:
        draft_payload = client.create_test_suite_draft(payload)
        draft = _require_dict(draft_payload.get("draft"), "draft")
        summary_payload = client.generate_playbook_summary(str(draft["id"])) if args.extract else None
    except Exception as exc:
        print(f"Wendell playbook create failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        output = {"draft": draft}
        if summary_payload is not None:
            output["playbook_summary"] = summary_payload.get("playbook_summary")
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        print(f"Playbook draft: {draft.get('id')}")
        print(f"Name: {draft.get('name') or args.name}")
        if summary_payload is not None:
            _print_playbook_summary(_require_dict(summary_payload.get("playbook_summary"), "playbook_summary"), compact=True)
        print(f"Next: wendell playbook review {draft.get('id')}")
    return 0


def _playbook_compile(args: argparse.Namespace) -> int:
    try:
        source_path = Path(args.source)
        source_text = source_path.read_text(encoding="utf-8")
        output_path = Path(args.output)
        bundle = _compile_local_playbook_source(
            name=args.name,
            domain=args.domain,
            source_text=f"{args.workflow_summary.strip()}\n\n{source_text}",
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config_path = Path(args.config_output) if args.config_output else None
        if config_path is not None:
            _write_local_wendell_config(
                config_path,
                project=_slugify_cli(args.name),
                suite_path=output_path,
                agent_command=args.agent_command,
            )
    except Exception as exc:
        print(f"Wendell playbook compile failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"suite": str(output_path), "config": None if config_path is None else str(config_path)}, indent=2, sort_keys=True))
    else:
        print(f"Compiled suite: {output_path}")
        if config_path is not None:
            print(f"Config: {config_path}")
            print(f"Run: wendell test --config {config_path}")
        else:
            print("Next: add this suite path to wendell.toml as `worldsim_input`.")
    return 0


def _playbook_review(args: argparse.Namespace) -> int:
    client = _authenticated_client(args.api_url)
    if client is None:
        return 1
    try:
        resolved = _resolve_playbook_summary(client, args.playbook_ref, generate_if_missing=True)
    except Exception as exc:
        print(f"Wendell playbook review failed: {exc}", file=sys.stderr)
        return 1
    summary = resolved["summary"]
    if args.json:
        print(json.dumps({"draft": resolved.get("draft"), "playbook_summary": summary}, indent=2, sort_keys=True))
    else:
        _print_playbook_summary(summary)
        _print_playbook_review_next_step(summary, args.playbook_ref)
    return 0


def _playbook_apply(args: argparse.Namespace) -> int:
    client = _authenticated_client(args.api_url)
    if client is None:
        return 1
    try:
        patch = _load_review_patch(Path(args.file))
        resolved = _resolve_playbook_summary(client, args.playbook_ref, generate_if_missing=False)
        summary = resolved["summary"]
        applied = _apply_review_operations(client, summary, _review_operations(patch))
    except Exception as exc:
        print(f"Wendell playbook apply failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"playbook_summary": applied["summary"], "applied_operations": applied["count"]}, indent=2, sort_keys=True))
    else:
        print(f"Applied {applied['count']} operation(s) to Playbook summary: {applied['summary'].get('id')}")
        _print_required_questions(applied["summary"])
        _print_playbook_review_next_step(applied["summary"], args.playbook_ref)
    return 0


def _playbook_approve(args: argparse.Namespace) -> int:
    client = _authenticated_client(args.api_url)
    if client is None:
        return 1
    try:
        resolved = _resolve_playbook_summary(client, args.playbook_ref, generate_if_missing=False)
        summary = resolved["summary"]
        approved_payload = client.approve_playbook_summary(str(summary["id"]), {"reviewer_ref": args.reviewer} if args.reviewer else {})
        approved = _require_dict(approved_payload.get("playbook_summary"), "playbook_summary")
        generated = None
        draft = resolved.get("draft")
        if args.generate_suite:
            draft_id = str(draft["id"]) if isinstance(draft, dict) and draft.get("id") else str(approved.get("world_draft_id") or "")
            if not draft_id:
                raise ValueError("--generate-suite requires a Playbook draft id or a summary payload linked to a draft.")
            generated = client.generate_test_suite_candidate(draft_id)
    except Exception as exc:
        print(f"Wendell playbook approve failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"playbook_summary": approved, "generated_suite": generated}, indent=2, sort_keys=True))
    else:
        print(f"Approved Playbook summary: {approved.get('id')}")
        print(f"Status: {approved.get('status')}")
        if generated is not None:
            draft_payload = generated.get("draft") if isinstance(generated.get("draft"), dict) else {}
            latest_snapshot = draft_payload.get("latest_snapshot") if isinstance(draft_payload.get("latest_snapshot"), dict) else {}
            metadata = draft_payload.get("metadata") if isinstance(draft_payload.get("metadata"), dict) else {}
            snapshot_id = latest_snapshot.get("id") or metadata.get("latest_snapshot_id") or "created"
            print(f"Generated suite candidate: {snapshot_id}")
            draft_id = draft_payload.get("id") or (draft.get("id") if isinstance(draft, dict) else None)
            if draft_id:
                print(f"Next: wendell suites publish --draft {draft_id}")
    return 0


def _read_playbook_sources(paths: list[Path], source_text: str | None) -> str:
    chunks: list[str] = []
    for path in paths:
        chunks.append(f"# Source: {path}\n{path.read_text(encoding='utf-8').strip()}")
    if source_text:
        chunks.append(str(source_text).strip())
    return "\n\n".join(chunk for chunk in chunks if chunk.strip()).strip()


def _compile_local_playbook_source(*, name: str, domain: str, source_text: str) -> dict[str, Any]:
    try:
        from worldsim.playbook_source_extractor import compile_playbook_source_to_bundle, customer_input_bundle_to_dict
    except ImportError as exc:
        raise RuntimeError("local Playbook compilation requires the worldsim package in this Python environment.") from exc
    bundle = compile_playbook_source_to_bundle(name=name, domain=domain, source_text=source_text)
    return customer_input_bundle_to_dict(bundle)


def _require_local_playbook_compiler() -> None:
    try:
        compiler = importlib.util.find_spec("worldsim.playbook_source_extractor")
    except ModuleNotFoundError:
        compiler = None
    if compiler is None:
        raise RuntimeError(
            "wendell init is a legacy local/offline workflow and requires the internal worldsim compiler, "
            "which is not included in the public Wendell CLI package. For production projects, create or "
            "publish a hosted suite, then run `wendell suites configure --suite <suite-slug>`."
        )


def _initialize_local_project(args: argparse.Namespace) -> dict[str, str]:
    _require_local_playbook_compiler()
    cwd = Path.cwd()
    project = args.project or _slugify_cli(cwd.name)
    name = args.name or f"{project.replace('-', ' ').title()} Agent Regression"
    playbook_path = Path(args.playbook)
    suite_path = Path(args.suite)
    config_path = Path(args.config)
    adapter_path = Path(args.adapter)
    for path in [playbook_path, suite_path, config_path, adapter_path]:
        if path.exists() and not args.force:
            raise ValueError(f"`{path}` already exists. Re-run with --force to overwrite.")
    _write_text_file(playbook_path, _default_playbook_template(name, args.workflow_summary))
    _write_text_file(adapter_path, _agent_adapter_template())
    adapter_path.chmod(adapter_path.stat().st_mode | 0o111)
    source_text = playbook_path.read_text(encoding="utf-8")
    suite = _compile_local_playbook_source(name=name, domain=args.domain, source_text=f"{args.workflow_summary}\n\n{source_text}")
    suite_path.parent.mkdir(parents=True, exist_ok=True)
    suite_path.write_text(json.dumps(suite, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_local_wendell_config(
        config_path,
        project=project,
        suite_path=suite_path,
        agent_command=f"python {adapter_path.as_posix()}",
    )
    return {
        "project": project,
        "playbook": str(playbook_path),
        "suite": str(suite_path),
        "adapter": str(adapter_path),
        "config": str(config_path),
    }


def _write_text_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _default_playbook_template(name: str, workflow_summary: str) -> str:
    return f"""# {name} Playbook

{workflow_summary}

## Actors
- customer
- support agent

## Systems and tools
- workflow console: inspect request, record evidence, complete workflow

## Workflow
- inspect request
- record evidence
- complete workflow

## Required behavior
- Inspect the request before completing the workflow.
- Record evidence before completing the workflow.
- Do not claim success without tool or trajectory evidence.

## Evidence requirements
- request inspection event
- evidence recording event
- completion event

## Critical failures
- Critical failure: workflow completed without evidence.
"""


def _agent_adapter_template(*, tool_contracts: list[dict[str, Any]] | None = None) -> str:
    if tool_contracts:
        return _exact_agent_adapter_template(tool_contracts)
    return '''#!/usr/bin/env python3
"""Wendell agent adapter template.

Wire this file to your production agent before relying on Wendell test results.
By default it proxies the Wendell scenario payload to the command in
WENDELL_APP_AGENT_COMMAND. That command must read JSON from stdin and print JSON:

{
  "message": "customer-facing response",
  "tool_calls": [{"name": "system.tool", "args": {"case_id": "case_123"}}]
}

Docs: https://docs.wendellai.com/agent-adapter-contract
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys


ADAPTER_HELP = """Wendell adapter is not wired.

Wire this template to your production agent before relying on Wendell results.

Fast path:
  export WENDELL_APP_AGENT_COMMAND="python scripts/run_my_agent.py"

That command must:
  1. read Wendell's JSON scenario payload from stdin
  2. run your production agent or agent service
  3. print a JSON object with:
     {
       "message": "customer-facing response",
       "tool_calls": [{"name": "system.tool", "args": {"case_id": "case_123"}}],
       "metrics": {}
     }

Alternative: replace scripts/wendell_agent_adapter.py with code that directly
calls your production agent and prints the same JSON object.

Docs: https://docs.wendellai.com/agent-adapter-contract
"""


def main() -> None:
    payload = json.loads(sys.stdin.read())
    result = run_agent(payload)
    validate_result(result)
    print(json.dumps(result))


def run_agent(payload: dict) -> dict:
    command = os.environ.get("WENDELL_APP_AGENT_COMMAND")
    if not command:
        raise SystemExit(ADAPTER_HELP)
    try:
        command_args = shlex.split(command)
    except ValueError as exc:
        raise SystemExit(f"agent command is invalid: {exc}") from exc
    if not command_args:
        raise SystemExit("agent command is empty")
    completed = subprocess.run(
        command_args,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stderr.strip() or f"agent command exited {completed.returncode}")
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        preview = completed.stdout.strip()[:500]
        detail = f" Output started with: {preview!r}" if preview else ""
        raise SystemExit(
            "agent command must print JSON with `message` and `tool_calls` fields."
            f"{detail}"
        ) from exc
    if not isinstance(parsed, dict):
        raise SystemExit("agent command must print a JSON object")
    return normalize_agent_result(parsed)


def normalize_agent_result(parsed: dict) -> dict:
    """Unwrap common agent CLI envelopes into Wendell's adapter contract."""
    if isinstance(parsed.get("structured_output"), dict):
        return parsed["structured_output"]
    result = parsed.get("result")
    if isinstance(result, dict):
        return result
    if isinstance(result, str) and result.strip().startswith("{"):
        try:
            decoded = json.loads(result)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            return decoded
    return parsed


def validate_result(result: dict) -> None:
    if not isinstance(result.get("message"), str):
        raise SystemExit("agent result must include string field `message`")
    tool_calls = result.get("tool_calls", [])
    if not isinstance(tool_calls, list):
        raise SystemExit("agent result `tool_calls` must be a list")
    for index, call in enumerate(tool_calls):
        if not isinstance(call, dict) or not isinstance(call.get("name"), str):
            raise SystemExit(f"tool_calls[{index}] must include string field `name`")
        if not isinstance(call.get("args", {}), dict):
            raise SystemExit(f"tool_calls[{index}].args must be an object")


if __name__ == "__main__":
    main()
'''


def _exact_agent_adapter_template(tool_contracts: list[dict[str, Any]]) -> str:
    contracts = [dict(item) for item in tool_contracts if item.get("name")]
    contract_json = json.dumps(contracts, indent=2, sort_keys=True)
    functions = "\n\n".join(_adapter_tool_function(str(contract["name"]), dict(contract.get("arguments") or {})) for contract in contracts)
    return f'''#!/usr/bin/env python3
"""Generated Wendell adapter for exact suite tool contracts.

This stub is intentionally deterministic: it advertises every generated suite
tool and returns success-shaped tool calls for first-run integration readiness.
Replace individual tool functions with production integrations when ready.
"""

from __future__ import annotations

import json
import sys


TOOL_CONTRACTS = {contract_json}
SUPPORTED_TOOLS = [contract["name"] for contract in TOOL_CONTRACTS]


def main() -> None:
    payload = json.loads(sys.stdin.read() or "{{}}")
    if payload.get("type") == "wendell.handshake":
        print(json.dumps({{"adapter_name": "wendell_generated_adapter", "supported_tools": SUPPORTED_TOOLS}}))
        return
    tool_calls = []
    for tool in payload.get("available_tools", []):
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "")
        if name not in DISPATCH:
            continue
        tool_calls.append(DISPATCH[name](tool))
    print(json.dumps({{"message": "Generated Wendell adapter completed supported tool calls.", "tool_calls": tool_calls, "metrics": {{"adapter_mode": "generated_stub"}}}}))


def _default_args(tool: dict, fallback_arguments: dict) -> dict:
    arguments = tool.get("arguments") if isinstance(tool.get("arguments"), dict) else fallback_arguments
    args = {{}}
    for name in arguments:
        args[str(name)] = _default_value(str(name))
    if not args:
        args["case_id"] = str((tool.get("case") or {{}}).get("case_id") or "case_123") if isinstance(tool.get("case"), dict) else "case_123"
    return args


def _default_value(name: str) -> str:
    if name.endswith("_id") or name in {{"case_id", "request_id"}}:
        return f"{{name}}_123"
    if "email" in name:
        return "owner@example.com"
    if name == "decision":
        return "completed"
    return f"{{name}}_value"


{functions}


DISPATCH = {{
{_adapter_dispatch_entries(contracts)}
}}


if __name__ == "__main__":
    main()
'''


def _adapter_tool_function(tool_name: str, arguments: dict[str, Any]) -> str:
    function_name = _adapter_function_name(tool_name)
    return (
        f"def {function_name}(tool: dict) -> dict:\n"
        f"    return {{\"name\": {tool_name!r}, \"args\": _default_args(tool, {arguments!r})}}"
    )


def _adapter_dispatch_entries(contracts: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"    {str(contract['name'])!r}: {_adapter_function_name(str(contract['name']))},"
        for contract in contracts
        if contract.get("name")
    )


def _adapter_function_name(tool_name: str) -> str:
    normalized = tool_name.replace(".", "__")
    chars = [char if char.isalnum() or char == "_" else "_" for char in normalized]
    return "".join(chars).strip("_") or "tool"


def _write_local_wendell_config(
    path: Path,
    *,
    project: str,
    suite_path: Path,
    agent_command: str | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    relative_suite = os.path.relpath(suite_path.resolve(), path.parent.resolve())
    lines = [
        f"project = {_toml_string(project)}",
        'mode = "blocking"',
        f"worldsim_input = {_toml_string(relative_suite)}",
        f"agent_command = {_toml_string(agent_command or 'python scripts/run_agent_adapter.py')}",
        "upload_traces = false",
        "",
        "[gates]",
        "suite_min_score = 0.90",
        "scenario_min_score = 0.85",
        "critical_failures_allowed = 0",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_hosted_suite_config(path: Path, *, project: str, agent_command: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"project = {_toml_string(project)}",
        'mode = "advisory"',
        f"agent_command = {_toml_string(agent_command)}",
        "upload_traces = true",
        "",
        "[gates]",
        "suite_min_score = 0.80",
        "scenario_min_score = 0.75",
        "critical_failures_allowed = 0",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _slugify_cli(value: str) -> str:
    lowered = value.lower()
    chars = [char if char.isalnum() else "-" for char in lowered]
    return "-".join(part for part in "".join(chars).split("-") if part) or "wendell-suite"


def _resolve_playbook_summary(
    client: RemoteWendellClient,
    playbook_ref: str,
    *,
    generate_if_missing: bool,
) -> dict[str, Any]:
    if playbook_ref.startswith("psum_"):
        payload = client.get_playbook_summary(playbook_ref)
        return {"summary": _require_dict(payload.get("playbook_summary"), "playbook_summary")}
    draft_payload = client.get_test_suite_draft(playbook_ref)
    draft = _require_dict(draft_payload.get("draft"), "draft")
    summary = draft_payload.get("latest_playbook_summary")
    if not isinstance(summary, dict) and generate_if_missing:
        summary_payload = client.generate_playbook_summary(str(draft["id"]))
        summary = summary_payload.get("playbook_summary")
    if not isinstance(summary, dict):
        raise ValueError(f"Playbook draft `{playbook_ref}` has no Playbook summary. Run `wendell playbook review {playbook_ref}` first.")
    return {"draft": draft, "summary": summary}


def _print_playbook_summary(summary: dict[str, Any], *, compact: bool = False) -> None:
    print(f"Playbook summary: {summary.get('id')}")
    print(f"Status: {summary.get('status') or 'unknown'}")
    payload = summary.get("summary_payload") if isinstance(summary.get("summary_payload"), dict) else {}
    sections = [
        ("actors", payload.get("actors")),
        ("systems_tools", payload.get("systems_tools")),
        ("policies", payload.get("policies")),
        ("allowed_actions", payload.get("allowed_actions")),
        ("risks", payload.get("risks")),
    ]
    for label, value in sections:
        items = value if isinstance(value, list) else []
        if compact:
            print(f"{label}: {len(items)}")
            continue
        print(f"\n{label} ({len(items)}):")
        for item in items[:10]:
            print(f"- {item}")
        if len(items) > 10:
            print(f"- ... {len(items) - 10} more")
    _print_required_questions(summary)


def _print_required_questions(summary: dict[str, Any]) -> None:
    questions = _required_questions(summary)
    if not questions:
        print("Required questions: none")
        return
    print(f"Required questions: {len(questions)}")
    for index, question in enumerate(questions, start=1):
        if isinstance(question, dict):
            print(f"{index}. [{question.get('section') or 'unknown'}] {question.get('question') or question}")
        else:
            print(f"{index}. {question}")


def _required_questions(summary: dict[str, Any]) -> list[Any]:
    payload = summary.get("summary_payload") if isinstance(summary.get("summary_payload"), dict) else {}
    questions = payload.get("required_questions")
    return questions if isinstance(questions, list) else []


def _print_playbook_review_next_step(summary: dict[str, Any], playbook_ref: str) -> None:
    if _required_questions(summary):
        print(f"Next: answer required questions with `wendell playbook apply {playbook_ref} --file review.json`")
    else:
        print(f"Next: wendell playbook approve {playbook_ref} --reviewer \"you@example.com\" --generate-suite")


def _load_review_patch(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        payload = json.loads(text)
    else:
        payload = _load_yaml_like_review_file(text)
    if not isinstance(payload, dict):
        raise ValueError("review file must contain an object with an `operations` list.")
    return payload


def _load_yaml_like_review_file(text: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-not-found]

        payload = yaml.safe_load(text)
        return payload if isinstance(payload, dict) else {}
    except ModuleNotFoundError:
        pass
    stripped = text.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    raise ValueError("YAML review files require PyYAML in this environment. Use JSON or install PyYAML.")


def _review_operations(patch: dict[str, Any]) -> list[dict[str, Any]]:
    operations = patch.get("operations")
    if not isinstance(operations, list):
        raise ValueError("review file must contain an `operations` list.")
    normalized = [operation for operation in operations if isinstance(operation, dict)]
    if len(normalized) != len(operations):
        raise ValueError("every review operation must be an object.")
    return normalized


def _apply_review_operations(client: RemoteWendellClient, summary: dict[str, Any], operations: list[dict[str, Any]]) -> dict[str, Any]:
    current = summary
    count = 0
    for operation in operations:
        op = str(operation.get("op") or "").strip()
        if op == "answer_question":
            section = str(operation.get("section") or operation.get("question_id") or "").strip()
            if not section:
                raise ValueError("answer_question operation requires `section`.")
            payload = {
                "decision_type": "playbook_summary.answer_question",
                "section_key": section,
                "summary": f"Answered required Playbook question for {section}.",
                "question_answers": [{"section": section, "answer": operation.get("answer", operation.get("value"))}],
            }
        elif op in {"update_primitive", "add_primitive"}:
            section_key = str(operation.get("section_key") or operation.get("section") or "").strip()
            if not section_key:
                raise ValueError(f"{op} operation requires `section_key`.")
            value = operation.get("value", operation.get("text"))
            if op == "add_primitive":
                value = [*_summary_list(current, section_key), *_operation_values(value)]
            elif isinstance(value, str):
                value = [value]
            payload = {
                "decision_type": f"playbook_summary.{op}",
                "section_key": section_key,
                "summary": f"Applied {op} to {section_key}.",
                "changed_fields": {"value": _dedupe_cli_values(_operation_values(value))},
            }
        elif op in {"remove_primitive", "reject_primitive"}:
            section_key = str(operation.get("section_key") or operation.get("section") or "").strip()
            value = str(operation.get("value") or operation.get("text") or "").strip()
            if not section_key or not value:
                raise ValueError(f"{op} operation requires `section_key` and `value`.")
            remaining = [item for item in _summary_list(current, section_key) if item != value]
            payload = {
                "decision_type": f"playbook_summary.{op}",
                "section_key": section_key,
                "summary": f"Applied {op} to {section_key}.",
                "changed_fields": {"value": remaining},
            }
        elif op in {"resolve_conflict", "accept_assumption"}:
            section_key = str(operation.get("section_key") or op).strip()
            payload = {
                "decision_type": f"playbook_summary.{op}",
                "section_key": section_key,
                "summary": str(operation.get("summary") or operation.get("answer") or f"Applied {op}."),
                "changed_fields": {"value": operation.get("value", operation.get("answer", operation.get("summary")))},
            }
        else:
            raise ValueError(f"unsupported review operation `{op}`.")
        response = client.review_playbook_summary(str(current["id"]), payload)
        current = _require_dict(response.get("playbook_summary"), "playbook_summary")
        count += 1
    return {"summary": current, "count": count}


def _summary_list(summary: dict[str, Any], section_key: str) -> list[str]:
    payload = summary.get("summary_payload") if isinstance(summary.get("summary_payload"), dict) else {}
    return [str(item) for item in payload.get(section_key, []) if isinstance(item, str)]


def _operation_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _dedupe_cli_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"response did not include `{label}`.")
    return value


def _suite_run_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{Path(sys.argv[0]).name} run", description="Run an agent against a Wendell test suite.")
    parser.add_argument("--suite", required=True, help="Published test suite slug.")
    parser.add_argument("--config", default="wendell.toml", help="Path to Wendell config.")
    parser.add_argument("--api-url", help="Override stored Wendell API URL.")
    parser.add_argument(
        "--github-summary",
        action="store_true",
        help="Append the run result and private report link to $GITHUB_STEP_SUMMARY.",
    )
    parser.add_argument("--skip-preflight", action="store_true", help="Skip adapter tool-contract preflight.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args(list(argv))

    config_path = Path(args.config)
    if not config_path.exists():
        print(
            f"Wendell run failed: config file `{config_path}` was not found. "
            f"Run `wendell suites configure --suite {args.suite} --config {config_path}` first.",
            file=sys.stderr,
        )
        return 1
    try:
        config = _load_runner_config(config_path)
    except (OSError, ValueError) as exc:
        print(f"Wendell run failed: {exc}", file=sys.stderr)
        return 1
    _load_env_files(config_path, config)
    client = _authenticated_client(args.api_url or config.api_url)
    if client is None:
        return 1
    try:
        suite = _resolve_suite_run_config(client, args.suite)
    except Exception as exc:
        print(f"Wendell run failed: could not resolve suite `{args.suite}`: {exc}", file=sys.stderr)
        return 1
    run_config = replace(
        config,
        api_url=client.api_url,
        world=str(suite["world"]),
        world_version=str(suite["world_version"]),
        scenario_pack=str(suite["scenario_pack"]),
        scenario_pack_version=str(suite["scenario_pack_version"]),
        upload_traces=True,
        metadata={**dict(config.metadata), "test_suite": {"slug": args.suite}},
        external_ci_ref={
            **_detected_ci_reference(),
            **dict(config.external_ci_ref),
            "source": "wendell_suite_cli",
            "suite_slug": args.suite,
        },
    )
    if not run_config.world or not run_config.world_version or not run_config.scenario_pack or not run_config.scenario_pack_version:
        print(f"Wendell run failed: suite `{args.suite}` is not bound to a ready scenario pack.", file=sys.stderr)
        return 1
    if not args.skip_preflight:
        try:
            _preflight_adapter_tool_contracts(run_config, _suite_tool_contracts(client, args.suite), suite_slug=args.suite, config_path=config_path)
        except Exception as exc:
            print(f"Wendell run failed: preflight failed: {exc}", file=sys.stderr)
            return 1
    try:
        result, remote_payload = _run_suite_with_remote_upload(run_config, client=client)
    except Exception as exc:
        print(f"Wendell run failed: {exc}", file=sys.stderr)
        return 1
    decision = evaluate_gates(result, run_config.gates, mode=run_config.mode)
    payload = {
        "decision": decision.status,
        "exit_code": decision.exit_code,
        "advisory": not decision.blocking,
        "reasons": list(decision.reasons),
        "suite": result.to_dict(),
        "remote": remote_payload,
    }
    if remote_payload.get("run_id"):
        link = _create_live_session_link(
            client,
            {"next": f"/dashboard/runs/{remote_payload['run_id']}", "run_id": remote_payload["run_id"]},
            warning_label=None if args.json else "Wendell run warning",
        )
        if link.get("url"):
            payload["remote"] = {
                **dict(remote_payload),
                "private_report_url": link["url"],
                "private_report_url_expires_in_seconds": link.get("expires_in_seconds"),
            }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Suite run: {remote_payload.get('run_id')}")
        print(f"Status: {remote_payload.get('status') or 'uploaded'}")
        print(f"Gate: {decision.status}")
        if decision.reasons:
            print("\nGate failures:")
            for reason in decision.reasons:
                print(f"- {reason}")
        _print_failure_details(result)
        if payload["remote"].get("private_report_url"):
            _print_live_session_url(
                str(payload["remote"]["private_report_url"]),
                label="Live run",
                expires_in_seconds=payload["remote"].get("private_report_url_expires_in_seconds"),
            )
    if args.github_summary:
        try:
            _append_github_step_summary(payload)
        except OSError as exc:
            print(f"Wendell run warning: could not write GitHub step summary: {exc}", file=sys.stderr)
    return decision.exit_code


def _resolve_suite_run_config(client: RemoteWendellClient, suite_slug: str) -> dict[str, str]:
    suites_payload = client.list_test_suites()
    suites = suites_payload.get("test_suites") if isinstance(suites_payload.get("test_suites"), list) else []
    suite = next((item for item in suites if isinstance(item, dict) and item.get("slug") == suite_slug), None)
    if suite is None:
        detail = client.get_test_suite(suite_slug)
        world = detail.get("world") if isinstance(detail.get("world"), dict) else {}
        versions = detail.get("versions") if isinstance(detail.get("versions"), list) else []
        packs = detail.get("scenario_packs") if isinstance(detail.get("scenario_packs"), list) else []
        version = versions[0] if versions and isinstance(versions[0], dict) else {}
        pack = packs[0] if packs and isinstance(packs[0], dict) else {}
        suite = {
            "slug": world.get("slug") or suite_slug,
            "world_version": version.get("version"),
            "scenario_pack": pack.get("slug"),
            "scenario_pack_version": pack.get("version"),
        }
    return {
        "world": _suite_runtime_world_slug(suite, suite_slug),
        "world_version": str(suite.get("world_version") or ""),
        "scenario_pack": str(suite.get("scenario_pack") or ""),
        "scenario_pack_version": str(suite.get("scenario_pack_version") or ""),
    }


def _suite_tool_contracts(client: RemoteWendellClient, suite_slug: str) -> list[dict[str, Any]]:
    get_test_suite = getattr(client, "get_test_suite", None)
    if not callable(get_test_suite):
        return []
    try:
        detail = get_test_suite(suite_slug)
    except Exception:
        return []
    contracts = detail.get("tool_contracts") if isinstance(detail, dict) else None
    if not isinstance(contracts, list):
        return []
    return [dict(item) for item in contracts if isinstance(item, dict) and item.get("name")]


def _tool_manifest_template(suite_slug: str, tool_contracts: list[dict[str, Any]]) -> str:
    payload = {
        "schema_version": "wendell.tool_manifest.v1",
        "suite": suite_slug,
        "tool_contracts": tool_contracts,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _suite_runtime_world_slug(suite: dict[str, Any], suite_slug: str) -> str:
    world = suite.get("world")
    if isinstance(world, dict):
        value = world.get("slug")
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("world_slug", "world", "slug"):
        value = suite.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return suite_slug


def _detected_ci_reference() -> dict[str, Any]:
    github_run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    github_repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not github_run_id and not github_repository:
        return {}
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com").strip().rstrip("/") or "https://github.com"
    run_url = f"{server_url}/{github_repository}/actions/runs/{github_run_id}" if github_repository and github_run_id else None
    fields = {
        "provider": "github_actions",
        "repository": github_repository,
        "workflow": os.environ.get("GITHUB_WORKFLOW", "").strip(),
        "job": os.environ.get("GITHUB_JOB", "").strip(),
        "run_id": github_run_id,
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "").strip(),
        "run_url": run_url,
        "event_name": os.environ.get("GITHUB_EVENT_NAME", "").strip(),
        "actor": os.environ.get("GITHUB_ACTOR", "").strip(),
        "ref": os.environ.get("GITHUB_REF", "").strip(),
        "ref_name": os.environ.get("GITHUB_REF_NAME", "").strip(),
        "ref_type": os.environ.get("GITHUB_REF_TYPE", "").strip(),
        "head_ref": os.environ.get("GITHUB_HEAD_REF", "").strip(),
        "base_ref": os.environ.get("GITHUB_BASE_REF", "").strip(),
        "sha": os.environ.get("GITHUB_SHA", "").strip(),
    }
    return {key: value for key, value in fields.items() if value}


def _authenticated_client(api_url_override: str | None = None) -> RemoteWendellClient | None:
    env_key = os.environ.get("WENDELL_INKPASS_API_KEY")
    if env_key:
        api_url = _effective_api_url(api_url_override)
        return RemoteWendellClient(api_url, api_key=env_key)
    credentials = load_credentials()
    if credentials is None:
        print(
            "Wendell error: no runner credential found.\n"
            "For a new local runner, run `wendell register`.\n"
            "For CI, set `WENDELL_INKPASS_API_KEY` from your secret store.\n"
            f"Existing API keys can be stored with `wendell login --api-key-stdin --validate`.\n"
            f"Expected local credentials at {credentials_path()}.",
            file=sys.stderr,
        )
        return None
    api_url = _effective_api_url(api_url_override or credentials.api_url)
    return RemoteWendellClient(api_url, api_key=credentials.api_key)


def _register_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{Path(sys.argv[0]).name} register", description="Register this runner with Wendell.")
    parser.add_argument("--api-url", default=_effective_api_url())
    parser.add_argument("--email", help="Wendell account email.")
    parser.add_argument("--agent", help="Agent display name for this runner.")
    parser.add_argument("--provider", default="unknown", help="Agent model provider.")
    parser.add_argument("--model", default="unknown", help="Agent model name.")
    parser.add_argument("--computer-name", default=os.uname().nodename if hasattr(os, "uname") else "Wendell Runner")
    parser.add_argument("--password-stdin", action="store_true", help="Read password from stdin.")
    args = parser.parse_args(list(argv))

    email = args.email or input("Email: ").strip()
    password = sys.stdin.read().strip() if args.password_stdin else getpass.getpass("Password: ").strip()
    agent = args.agent or input("Agent name: ").strip()
    if not email or not password or not agent:
        print("Wendell register failed: email, password, and agent are required.", file=sys.stderr)
        return 2
    client = RemoteWendellClient(args.api_url)
    try:
        runner_session = client.start_runner_session(
            {
                "computer_name": args.computer_name,
                "cli_version": f"wendell/{_package_version()}",
                "platform": sys.platform,
            }
        )
        registered = client.register_cli(
            {
                "runner_session_id": runner_session["runner_session_id"],
                "email": email,
                "password": password,
                "computer_name": args.computer_name,
                "agent_name": agent,
                "provider": args.provider,
                "model": args.model,
            }
        )
    except Exception as exc:
        print(f"Wendell register failed: {exc}", file=sys.stderr)
        return 1
    runner = registered.get("runner") if isinstance(registered.get("runner"), dict) else {}
    store_credentials(
        StoredCredentials(
            api_key=str(registered["api_key"]),
            api_url=args.api_url,
            runner_id=str(runner.get("runner_id")) if runner.get("runner_id") else None,
        )
    )
    print("Wendell runner registered.")
    print("New project: create and publish your first suite from a Playbook:")
    print("  https://docs.wendellai.com/quickstart")
    print("Existing suite:")
    print("  wendell suites list")
    print("  wendell suites configure --suite <suite-slug>")
    print("  wendell run --suite <suite-slug> --config wendell.toml")
    return 0


def _print_live_session_link(
    client: RemoteWendellClient,
    payload: dict,
    *,
    label: str = "Live session",
    warning_label: str = "Wendell warning",
) -> None:
    link = _create_live_session_link(client, payload, warning_label=warning_label)
    url = link.get("url")
    if not url:
        return
    _print_live_session_url(str(url), label=label, expires_in_seconds=link.get("expires_in_seconds"))


def _create_live_session_link(
    client: RemoteWendellClient,
    payload: dict,
    *,
    warning_label: str | None,
) -> dict:
    try:
        link = client.create_cli_session_link(payload)
    except Exception as exc:
        if warning_label:
            print(f"{warning_label}: live session link failed: {exc}", file=sys.stderr)
        return {}
    return link if isinstance(link, dict) else {}


def _print_live_session_url(url: str, *, label: str, expires_in_seconds: Any = None) -> None:
    print(f"{label}: {url}")
    if expires_in_seconds:
        print(f"Expires in: {expires_in_seconds} seconds")


def _doctor_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{Path(sys.argv[0]).name} doctor",
        description="Check Wendell local or CI setup before running a hosted suite.",
    )
    parser.add_argument("--config", default="wendell.toml", help="Path to Wendell config.")
    parser.add_argument("--api-url", help="Override configured Wendell API URL.")
    parser.add_argument("--validate", action="store_true", help="Validate credentials against the Wendell API.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args(list(argv))

    result = _doctor_result(Path(args.config), api_url_override=args.api_url, validate=bool(args.validate))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Wendell doctor: {result['status']}")
        print(f"API: {result['api_url']}")
        for check in result["checks"]:
            marker = "ok" if check["ok"] else "fail"
            print(f"- {check['name']}: {marker} - {check['detail']}")
        if result["next_steps"]:
            print("\nNext steps:")
            for step in result["next_steps"]:
                print(f"- {step}")
    return 0 if result["status"] == "pass" else 1


def _doctor_result(config_path: Path, *, api_url_override: str | None, validate: bool) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    next_steps: list[str] = []
    config: RunnerConfig | None = None
    config_api_url: str | None = None

    if not config_path.exists():
        checks.append(
            {
                "name": "config",
                "ok": False,
                "detail": f"config file `{config_path}` was not found",
            }
        )
        next_steps.append(f"Run `wendell suites configure --suite <suite-slug> --config {config_path}`.")
    else:
        try:
            config = RunnerConfig.from_file(config_path)
            _load_env_files(config_path, config)
            config_api_url = config.api_url
            checks.append({"name": "config", "ok": True, "detail": f"loaded `{config_path}` for project `{config.project}`"})
        except (OSError, ValueError) as exc:
            checks.append({"name": "config", "ok": False, "detail": str(exc)})
            next_steps.append(f"Fix `{config_path}` or regenerate it with `wendell suites configure --suite <suite-slug>`.")

    api_url = _effective_api_url(api_url_override or config_api_url)
    credential = _doctor_credential_source()
    checks.append({"name": "auth", "ok": credential["ok"], "detail": credential["detail"]})
    if not credential["ok"]:
        next_steps.append("Run `wendell register` locally, or set `WENDELL_INKPASS_API_KEY` in CI.")

    if config is None:
        checks.append({"name": "agent_command", "ok": False, "detail": "`agent_command` could not be checked without a valid config"})
    else:
        agent_check = _doctor_agent_command_check(config)
        checks.append(agent_check)
        if not agent_check["ok"]:
            next_steps.append("Set `agent_command` in `wendell.toml` or rerun `wendell suites configure --suite <suite-slug>`.")

    if validate:
        validation = _doctor_validate_identity(api_url, credential)
        checks.append(validation)
        if not validation["ok"]:
            next_steps.append("Validate the runner credential with `wendell whoami` or `wendell login --api-key-stdin --validate`.")

    return {
        "status": "pass" if all(bool(check["ok"]) for check in checks) else "fail",
        "api_url": api_url,
        "config": str(config_path),
        "checks": checks,
        "next_steps": _dedupe_cli_values(next_steps),
    }


def _doctor_credential_source() -> dict[str, Any]:
    if os.environ.get("WENDELL_INKPASS_API_KEY"):
        return {"ok": True, "source": "env", "detail": "using WENDELL_INKPASS_API_KEY from environment"}
    credentials = load_credentials()
    if credentials is None:
        return {"ok": False, "source": "missing", "detail": f"no runner credential found at {credentials_path()}"}
    return {"ok": True, "source": "stored", "detail": f"profile `{credentials.profile}` loaded from {credentials_path()}"}


def _doctor_agent_command_check(config: RunnerConfig) -> dict[str, Any]:
    if not config.agent_command:
        return {"name": "agent_command", "ok": False, "detail": "`agent_command` is missing from config"}
    try:
        command = _agent_command_args(config.agent_command)
    except ValueError as exc:
        return {"name": "agent_command", "ok": False, "detail": str(exc)}
    executable = command[0]
    base_dir = Path(config.project_dir) if config.project_dir else Path.cwd()
    if not _doctor_executable_exists(executable, base_dir=base_dir):
        return {"name": "agent_command", "ok": False, "detail": f"executable `{executable}` was not found"}
    script_check = _doctor_command_script_check(command, base_dir=base_dir)
    if script_check is not None:
        return script_check
    return {"name": "agent_command", "ok": True, "detail": f"command parses and executable `{executable}` is available"}


def _doctor_executable_exists(executable: str, *, base_dir: Path) -> bool:
    executable_path = Path(executable)
    if executable_path.is_absolute():
        return executable_path.exists()
    if executable_path.parent != Path("."):
        return (base_dir / executable_path).exists()
    return shutil.which(executable) is not None


def _doctor_command_script_check(command: list[str], *, base_dir: Path) -> dict[str, Any] | None:
    if len(command) < 2:
        return None
    executable = Path(command[0]).name.lower()
    if not (executable.startswith("python") or executable in {"py", "uv"}):
        return None
    script_index = 2 if executable == "uv" and len(command) > 2 and command[1] == "run" else 1
    if len(command) <= script_index:
        return None
    script = command[script_index]
    if script.startswith("-") or script == "-m":
        return None
    script_path = Path(script)
    if script_path.suffix != ".py" and script_path.parent == Path("."):
        return None
    resolved = script_path if script_path.is_absolute() else base_dir / script_path
    if not resolved.exists():
        return {"name": "agent_command", "ok": False, "detail": f"agent command script `{script}` was not found relative to `{base_dir}`"}
    if _is_generated_wendell_adapter(resolved) and not os.environ.get("WENDELL_APP_AGENT_COMMAND"):
        return {
            "name": "agent_command",
            "ok": False,
            "detail": (
                f"generated adapter `{script}` is not wired; set WENDELL_APP_AGENT_COMMAND "
                "or replace the adapter with code that runs your production agent"
            ),
        }
    return None


def _is_generated_wendell_adapter(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return "Wendell agent adapter template." in text and "ADAPTER_HELP" in text and "WENDELL_APP_AGENT_COMMAND" in text


def _doctor_validate_identity(api_url: str, credential: dict[str, Any]) -> dict[str, Any]:
    if not credential["ok"]:
        return {"name": "api_identity", "ok": False, "detail": "skipped because no runner credential is available"}
    env_key = os.environ.get("WENDELL_INKPASS_API_KEY")
    credentials = load_credentials()
    api_key = env_key or (None if credentials is None else credentials.api_key)
    if not api_key:
        return {"name": "api_identity", "ok": False, "detail": "skipped because no runner credential is available"}
    try:
        identity = RemoteWendellClient(api_url, api_key=api_key).get_identity()
    except Exception as exc:
        return {"name": "api_identity", "ok": False, "detail": f"identity check failed: {exc}"}
    return {"name": "api_identity", "ok": True, "detail": _identity_summary(identity)}


def _runs_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{Path(sys.argv[0]).name} runs", description="Inspect Wendell runs.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    watch = subcommands.add_parser("watch", help="Fetch the current status for a run.")
    watch.add_argument("run_id", help="Wendell run id.")
    watch.add_argument("--api-url", help="Override stored Wendell API URL.")
    watch.add_argument("--json", action="store_true", help="Print JSON output.")
    report = subcommands.add_parser("report", help="Fetch the private report for a run.")
    report.add_argument("run_id", help="Wendell run id.")
    report.add_argument("--api-url", help="Override stored Wendell API URL.")
    report.add_argument(
        "--github-summary",
        action="store_true",
        help="Append the run report and private report link to $GITHUB_STEP_SUMMARY.",
    )
    report.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args(list(argv))
    if args.command == "watch":
        return _runs_watch(args)
    if args.command == "report":
        return _runs_report(args)
    parser.error(f"Unsupported runs command: {args.command}")
    return 2


def _runs_watch(args: argparse.Namespace) -> int:
    client = _authenticated_client(args.api_url)
    if client is None:
        return 1
    try:
        run = client.get_run(args.run_id)
    except Exception as exc:
        print(f"Wendell run watch failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(run, indent=2, sort_keys=True))
    else:
        print(f"Run: {args.run_id}")
        print(f"Status: {run.get('status') or 'unknown'}")
        if run.get("latest_score") is not None:
            print(f"Score: {run['latest_score']}")
        for line in _run_ci_context_lines(run):
            print(line)
    return 0


def _run_ci_context_lines(run: dict[str, Any]) -> list[str]:
    ci_ref = run.get("external_ci_ref") if isinstance(run.get("external_ci_ref"), dict) else {}
    environment = run.get("environment") if isinstance(run.get("environment"), dict) else {}
    provider = _string_value(ci_ref.get("provider") or environment.get("ci_provider"))
    repository = _string_value(ci_ref.get("repository"))
    workflow = _string_value(ci_ref.get("workflow"))
    run_id = _string_value(ci_ref.get("run_id") or environment.get("ci_run_id"))
    run_url = _string_value(ci_ref.get("run_url"))
    ref = _string_value(ci_ref.get("head_ref") or ci_ref.get("ref"))
    sha = _string_value(ci_ref.get("sha") or environment.get("commit_sha"))
    runner_version = _string_value(environment.get("runner_version"))

    lines: list[str] = []
    source = " / ".join(part for part in [repository, workflow] if part)
    if source:
        lines.append(f"CI source: {source}")
    elif provider or run_id:
        lines.append(f"CI source: {provider or 'ci'}{f' run {run_id}' if run_id else ''}")
    if run_url:
        lines.append(f"CI run: {run_url}")
    revision_parts = [part for part in [ref, _short_cli_value(sha)] if part]
    if revision_parts:
        lines.append(f"Revision: {' @ '.join(revision_parts)}")
    if runner_version:
        lines.append(f"Runner: {runner_version}")
    return lines


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _short_cli_value(value: str | None) -> str | None:
    if not value:
        return None
    return f"{value[:8]}...{value[-6:]}" if len(value) > 18 else value


def _runs_report(args: argparse.Namespace) -> int:
    client = _authenticated_client(args.api_url)
    if client is None:
        return 1
    try:
        report = client.get_run_report(args.run_id)
    except Exception as exc:
        print(f"Wendell runs report failed: {exc}", file=sys.stderr)
        return 1
    link = _create_live_session_link(
        client,
        {"next": f"/dashboard/runs/{args.run_id}", "run_id": args.run_id},
        warning_label=None if args.json else "Wendell runs report warning",
    )
    if link.get("url"):
        report = {
            **dict(report),
            "private_report_url": link["url"],
            "private_report_url_expires_in_seconds": link.get("expires_in_seconds"),
        }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        capability_report = report.get("capability_report") if isinstance(report.get("capability_report"), dict) else {}
        print(f"Run report: {args.run_id}")
        if capability_report.get("overall_score") is not None:
            print(f"Score: {capability_report['overall_score']}")
        if capability_report.get("critical_failure_count") is not None:
            print(f"Critical failures: {capability_report['critical_failure_count']}")
        if report.get("private_report_url"):
            _print_live_session_url(
                str(report["private_report_url"]),
                label="Private report",
                expires_in_seconds=report.get("private_report_url_expires_in_seconds"),
            )
    if args.github_summary:
        try:
            _append_github_step_summary({"remote": {"run_id": args.run_id}, "report": report})
        except OSError as exc:
            print(f"Wendell runs report warning: could not write GitHub step summary: {exc}", file=sys.stderr)
    return 0


def _append_github_step_summary(payload: dict[str, Any]) -> bool:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return False
    remote = payload.get("remote") if isinstance(payload.get("remote"), dict) else {}
    suite = payload.get("suite") if isinstance(payload.get("suite"), dict) else {}
    report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    capability_report = report.get("capability_report") if isinstance(report.get("capability_report"), dict) else {}

    run_id = _string_value(remote.get("run_id"))
    decision = _string_value(payload.get("decision"))
    private_report_url = _string_value(remote.get("private_report_url") or report.get("private_report_url"))
    score = suite.get("suite_score", capability_report.get("overall_score"))
    critical_failures = capability_report.get("critical_failure_count")

    lines = ["## Wendell report", ""]
    if decision:
        lines.append(f"- Gate: `{decision}`")
    if run_id:
        lines.append(f"- Run: `{run_id}`")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        lines.append(f"- Score: `{score:.2f}`")
    if isinstance(critical_failures, int) and not isinstance(critical_failures, bool):
        lines.append(f"- Critical failures: `{critical_failures}`")
    if private_report_url:
        lines.append(f"- Private report: {private_report_url}")
    if len(lines) == 2:
        return False

    with Path(summary_path).open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")
    return True


def _auth_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{Path(sys.argv[0]).name} auth", description="Manage Wendell CLI authentication.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    login = subcommands.add_parser("login", help="Store Wendell CLI credentials.")
    login.add_argument("--api-key", help="InkPass API key. Omit to enter it securely.")
    login.add_argument("--api-key-stdin", action="store_true", help="Read the InkPass API key from stdin.")
    login.add_argument("--email", help="Use email/password login instead of an API key.")
    login.add_argument("--password-stdin", action="store_true", help="Read password from stdin for email/password login.")
    login.add_argument("--computer-name", default=os.uname().nodename if hasattr(os, "uname") else "Wendell Runner")
    login.add_argument("--api-url", help="Default Wendell API URL to store with this credential.")
    login.add_argument("--profile", default="default", help="Credential profile name.")
    login.add_argument("--validate", action="store_true", help="Validate credentials against the Wendell API before storing.")

    subcommands.add_parser("logout", help="Delete stored Wendell CLI credentials.")
    whoami = subcommands.add_parser("whoami", help="Show the active Wendell CLI credential profile.")
    whoami.add_argument("--api-url", help="Override stored Wendell API URL for identity validation.")
    auth = subcommands.add_parser("auth", help="Inspect Wendell CLI authentication.")
    auth_subcommands = auth.add_subparsers(dest="auth_command", required=True)
    auth_subcommands.add_parser("status", help="Show where credentials are loaded from.")
    export = auth_subcommands.add_parser("export", help="Export the stored runner credential for CI secret setup.")
    export.add_argument("--profile", default="default", help="Credential profile to export.")
    export.add_argument(
        "--format",
        choices=("env", "json", "raw"),
        default="env",
        help="Output format. Use raw for piping into secret managers.",
    )
    export.add_argument(
        "--env-var",
        default="WENDELL_INKPASS_API_KEY",
        help="Environment variable name to use for env/json output.",
    )

    args = parser.parse_args(list(argv))

    command = args.command
    if command == "auth":
        command = args.auth_command
    if command == "login":
        return _login(args)
    if command == "logout":
        return _logout()
    if command == "whoami":
        return _whoami(args)
    if command == "status":
        return _status()
    if command == "export":
        return _export_auth(args)
    parser.error(f"Unsupported auth command: {command}")
    return 2


def _login(args: argparse.Namespace) -> int:
    if args.email:
        return _login_with_email(args)
    if args.api_key_stdin:
        api_key = sys.stdin.read().strip()
    elif args.api_key:
        api_key = str(args.api_key).strip()
    else:
        api_key = getpass.getpass("Wendell InkPass API key: ").strip()
    if not api_key:
        print("Wendell login failed: API key is required.", file=sys.stderr)
        return 2
    if args.validate:
        api_url = _effective_api_url(args.api_url)
        try:
            identity = RemoteWendellClient(api_url, api_key=api_key).get_identity()
        except Exception as exc:
            print(f"Wendell login failed: credentials were rejected by {api_url}: {exc}", file=sys.stderr)
            return 1
        print(_identity_summary(identity))
    path = store_credentials(StoredCredentials(api_key=api_key, profile=args.profile, api_url=args.api_url))
    print(f"Wendell credentials stored at {path}")
    return 0


def _login_with_email(args: argparse.Namespace) -> int:
    api_url = _effective_api_url(args.api_url)
    password = sys.stdin.read().strip() if args.password_stdin else getpass.getpass("Password: ").strip()
    if not password:
        print("Wendell login failed: password is required.", file=sys.stderr)
        return 2
    client = RemoteWendellClient(api_url)
    try:
        runner_session = client.start_runner_session(
            {
                "computer_name": args.computer_name,
                "cli_version": f"wendell/{_package_version()}",
                "platform": sys.platform,
            }
        )
        logged_in = client.login_cli(
            {
                "runner_session_id": runner_session["runner_session_id"],
                "email": args.email,
                "password": password,
                "computer_name": args.computer_name,
            }
        )
    except Exception as exc:
        print(f"Wendell login failed: {exc}", file=sys.stderr)
        return 1
    runner = logged_in.get("runner") if isinstance(logged_in.get("runner"), dict) else {}
    path = store_credentials(
        StoredCredentials(
            api_key=str(logged_in["api_key"]),
            profile=args.profile,
            api_url=api_url,
            runner_id=str(runner.get("runner_id")) if runner.get("runner_id") else None,
        )
    )
    print(f"Wendell credentials stored at {path}")
    return 0


def _logout() -> int:
    removed = delete_credentials()
    print("Wendell credentials removed." if removed else "No Wendell credentials were stored.")
    return 0


def _export_auth(args: argparse.Namespace) -> int:
    credentials = load_credentials(args.profile)
    if credentials is None:
        print(
            f"Wendell auth export failed: no stored credential found for profile `{args.profile}`. "
            "Run `wendell register` first.",
            file=sys.stderr,
        )
        return 1

    env_var = str(args.env_var).strip() or "WENDELL_INKPASS_API_KEY"
    if args.format == "raw":
        print(credentials.api_key)
        return 0
    if args.format == "json":
        print(
            json.dumps(
                {
                    "api_key": credentials.api_key,
                    "api_url": credentials.api_url,
                    "env_var": env_var,
                    "profile": credentials.profile,
                    "runner_id": credentials.runner_id,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"export {env_var}={shlex.quote(credentials.api_key)}")
    return 0


def _status() -> int:
    env_key = os.environ.get("WENDELL_INKPASS_API_KEY")
    if env_key:
        print("Wendell auth: using WENDELL_INKPASS_API_KEY from environment.")
        api_url = os.environ.get("WENDELL_API_URL")
        if api_url:
            print(f"Default API URL: {api_url}")
        return 0
    credentials = load_credentials()
    if credentials is None:
        print(f"Wendell auth: not logged in. Expected credentials at {credentials_path()}.")
        return 1
    print(f"Wendell auth: profile `{credentials.profile}` loaded from {credentials_path()}.")
    if credentials.api_url:
        print(f"Default API URL: {credentials.api_url}")
    return 0


def _whoami(args: argparse.Namespace) -> int:
    env_key = os.environ.get("WENDELL_INKPASS_API_KEY")
    api_url_override = getattr(args, "api_url", None)
    if env_key:
        api_url = _effective_api_url(api_url_override)
        return _print_remote_identity(RemoteWendellClient(api_url, api_key=env_key))
    credentials = load_credentials()
    if credentials is None:
        print(f"Wendell auth: not logged in. Expected credentials at {credentials_path()}.")
        return 1
    api_url = _effective_api_url(api_url_override or credentials.api_url)
    return _print_remote_identity(RemoteWendellClient(api_url, api_key=credentials.api_key), profile=credentials.profile)


def _print_remote_identity(client: RemoteWendellClient, *, profile: str | None = None) -> int:
    try:
        identity = client.get_identity()
    except Exception as exc:
        print(f"Wendell identity check failed: {exc}", file=sys.stderr)
        return 1
    if profile:
        print(f"Wendell auth: profile `{profile}` loaded from {credentials_path()}.")
    print(_identity_summary(identity))
    return 0


def _identity_summary(identity: dict) -> str:
    auth_type = identity.get("auth_type") or "unknown"
    org = identity.get("external_org_id") or "unknown-org"
    user = identity.get("external_user_id") or "no-user"
    api_key_id = identity.get("api_key_id")
    runner_id = identity.get("runner_id")
    suffix = f", api_key={api_key_id}" if api_key_id else ""
    if runner_id:
        suffix = f"{suffix}, runner={runner_id}"
    return f"Wendell identity: auth={auth_type}, org={org}, user={user}{suffix}"


def _apply_overrides(config: RunnerConfig, args: argparse.Namespace) -> RunnerConfig:
    overrides = {
        "project": args.project,
        "api_url": args.api_url,
        "world": args.world,
        "world_version": args.world_version,
        "scenario_pack": args.scenario_pack,
        "scenario_pack_version": args.scenario_pack_version,
        "agent": args.agent,
        "agent_command": args.agent_command,
    }
    return replace(config, **{key: value for key, value in overrides.items() if value is not None})


def _load_runner_config(config_path: Path) -> RunnerConfig:
    if not config_path.exists():
        raise OSError(f"config file `{config_path}` was not found.")
    return RunnerConfig.from_file(config_path)


def _load_env_files(config_path: Path, config: RunnerConfig) -> None:
    candidates = [
        Path.cwd() / ".env",
        config_path.parent / ".env",
    ]
    if config.project_dir is not None:
        candidates.append(Path(config.project_dir) / ".env")
    for path in candidates:
        if path.exists():
            _load_env_file(path)


def _load_env_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _normalize_env_value(value.strip())


def _normalize_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _effective_api_url(api_url: str | None = None) -> str:
    return api_url or os.environ.get("WENDELL_API_URL") or DEFAULT_API_URL


def _has_api_credentials(api_key_env: str) -> bool:
    return bool(os.environ.get(api_key_env)) or load_credentials() is not None


def _should_upload_traces(config: RunnerConfig) -> bool:
    return config.upload_traces and (bool(config.api_url) or _has_api_credentials(config.api_key_env))


def _print_test_result(decision, suite: SuiteResult) -> None:
    print(f"Wendell Test: {decision.status}")
    print(f"Suite: {suite.project}")
    print(f"Score: {suite.suite_score:.2f}")
    print(f"Critical failures: {suite.critical_failure_count}")
    if decision.reasons:
        print("\nGate failures:")
        for reason in decision.reasons:
            print(f"- {reason}")
    _print_failure_details(suite)


def _print_failure_details(suite: SuiteResult) -> None:
    failed_results = [
        result
        for result in suite.scenario_results
        if result.score < 1.0 or result.critical_failures or _failed_assertions(result)
    ]
    if not failed_results:
        return
    print("\nFailure details:")
    for result in failed_results:
        print(f"- {result.scenario_id}: score {result.score:.2f}")
        for failure in result.critical_failures:
            print(f"  critical: {failure}")
        for assertion in _failed_assertions(result):
            rule_id = assertion.get("rule_id") or "unknown_rule"
            assertion_id = assertion.get("assertion_id") or assertion.get("id") or "unknown_assertion"
            message = assertion.get("message") or assertion.get("critical_failure") or "assertion failed"
            indexes = assertion.get("event_indexes") or []
            evidence = f" events={indexes}" if indexes else " missing expected event"
            print(f"  rule {rule_id}: {assertion_id} - {message}{evidence}")
        for step_id, status in result.step_statuses.items():
            if status != "completed":
                print(f"  step {step_id}: {status}")
        for prompt in result.improvement_prompts[:2]:
            print(f"  fix: {prompt}")


def _failed_assertions(result: ScenarioResult) -> list[dict[str, Any]]:
    return [
        assertion
        for assertion in result.assertion_results
        if assertion.get("status") == "failed"
    ]


def _run_suite(config: RunnerConfig) -> SuiteResult:
    if config.worldsim_input:
        return LocalWorldsimClient().run_builtin_agent(
            config.worldsim_input,
            config.agent,
            project=config.project,
            world=config.world,
            scenario_pack=config.scenario_pack,
            agent_command=config.agent_command,
            agent_cwd=config.project_dir,
            max_turns=1,
        )
    raise ValueError(
        "no local suite input is configured. Set `worldsim_input` for local-only tests, "
        "or authenticate and run a hosted suite with `wendell run --suite <suite-slug>`."
    )


def _run_suite_with_remote_upload(
    config: RunnerConfig,
    client: RemoteWendellClient | None = None,
) -> tuple[SuiteResult, dict]:
    client = client or RemoteWendellClient.from_env(_effective_api_url(config.api_url), config.api_key_env)
    run = client.create_run(
        {
            "project": config.project,
            "world": config.world,
            "world_version": config.world_version,
            "scenario_pack": config.scenario_pack,
            "scenario_pack_version": config.scenario_pack_version,
            "mode": config.mode,
            "external_ci_ref": config.external_ci_ref,
            "metadata": config.metadata,
        }
    )
    run_id = str(run["run_id"])
    run_envelope = client.get_run(run_id)
    if not config.worldsim_input:
        return _run_remote_runtime_suite(config, client, run_id, run, run_envelope)
    scenario_pack = {}
    if config.world and config.scenario_pack:
        scenario_pack = client.fetch_scenario_pack(
            config.world,
            config.scenario_pack,
            world_version=config.world_version,
            scenario_pack_version=config.scenario_pack_version,
        )
    scenario_execution_by_key = {
        item["scenario_key"]: item["id"]
        for item in run_envelope.get("scenario_executions", [])
        if item.get("scenario_key")
    }
    uploads: list[dict] = []

    def upload_scenario(result: ScenarioResult) -> None:
        if not result.trajectory:
            return
        scenario_execution_id = scenario_execution_by_key.get(result.scenario_id)
        if not scenario_execution_id:
            return
        trace_response = client.upload_trace(run_id, _trace_payload(result.trajectory, scenario_execution_id))
        result_response = client.upload_result(run_id, {"trajectory_id": trace_response["trajectory_id"]})
        uploads.append(
            {
                "scenario_id": result.scenario_id,
                "scenario_execution_id": scenario_execution_id,
                "trajectory_id": trace_response["trajectory_id"],
                "run_score_id": result_response.get("run_score_id"),
                "gate_decision_id": result_response.get("gate_decision_id"),
                "ci_status": result_response.get("ci_status"),
            }
        )

    suite = LocalWorldsimClient().run_builtin_agent(
        config.worldsim_input,
        config.agent,
        project=config.project,
        world=config.world,
        scenario_pack=config.scenario_pack,
        agent_command=config.agent_command,
        agent_cwd=config.project_dir,
        max_turns=1,
        on_scenario_result=upload_scenario,
    )
    completion = client.complete_run(run_id) if uploads else {}
    return suite, {
        "run_id": run_id,
        "url": run.get("url"),
        "scenario_pack_id": scenario_pack.get("scenario_pack_id"),
        "world_version_id": scenario_pack.get("world_version_id"),
        "status": completion.get("status"),
        "uploaded": True,
        "uploaded_scenarios": len(uploads),
        "uploads": uploads,
    }


def _run_remote_runtime_suite(
    config: RunnerConfig,
    client: RemoteWendellClient,
    run_id: str,
    run_payload: dict,
    run_envelope: dict,
) -> tuple[SuiteResult, dict]:
    results: list[ScenarioResult] = []
    turn_responses: list[dict] = []
    max_work_items = _remote_runtime_max_work_items()
    while len(turn_responses) < max_work_items:
        work = client.get_run_work(run_id)
        if work.get("done"):
            break
        scenario_execution = work.get("scenario_execution") if isinstance(work.get("scenario_execution"), dict) else {}
        scenario_execution_id = str(scenario_execution.get("id") or "")
        if not scenario_execution_id:
            raise ValueError("remote runtime work item is missing scenario_execution.id")
        agent_response = _call_remote_runtime_agent(config, work)
        turn = client.submit_agent_turn(
            run_id,
            scenario_execution_id,
            _redact_outbound_value({
                "agent_name": config.agent,
                "message": agent_response.get("message", ""),
                "tool_calls": agent_response.get("tool_calls", []),
                "metrics": agent_response.get("metrics", {}),
                "complete": True,
            }),
        )
        turn_responses.append(turn)
        scenario = work.get("scenario") if isinstance(work.get("scenario"), dict) else {}
        results.append(
            _remote_turn_scenario_result(
                scenario_id=str(scenario.get("id") or scenario_execution.get("scenario_key") or scenario_execution_id),
                turn=turn,
            )
        )
    else:
        raise RuntimeError(
            f"remote runtime exceeded {max_work_items} work item(s) without completion. "
            "This usually means Wendell kept returning runnable work for the same run."
        )
    completion = client.complete_run(run_id)
    final_envelope = client.get_run(run_id)
    suite = SuiteResult(
        project=config.project,
        world=config.world,
        scenario_pack=config.scenario_pack,
        scenario_results=tuple(results),
        metadata={"source": "remote_runtime", "trajectory_count": len(results)},
    )
    return suite, {
        "run_id": run_id,
        "url": run_payload.get("url"),
        "scenario_pack_id": final_envelope.get("scenario_pack_id") or run_envelope.get("scenario_pack_id"),
        "world_version_id": final_envelope.get("world_version_id") or run_envelope.get("world_version_id"),
        "status": completion.get("status"),
        "uploaded": True,
        "uploaded_scenarios": len(turn_responses),
        "runtime": "remote",
        "turns": [
            {
                "scenario_execution_id": item.get("scenario_execution_id"),
                "trajectory_id": item.get("trajectory_id"),
                "scenario_score": item.get("scenario_score"),
            }
            for item in turn_responses
        ],
    }


def _remote_turn_scenario_result(*, scenario_id: str, turn: dict) -> ScenarioResult:
    score = turn.get("scenario_score") if isinstance(turn.get("scenario_score"), dict) else {}
    return ScenarioResult(
        scenario_id=scenario_id,
        score=_remote_score_value(score.get("overall_score", 0.0)),
        critical_failures=_remote_score_critical_failures(score),
        step_statuses=_remote_score_step_statuses(score),
        dimensions=_remote_score_dimensions(score),
        assertion_results=_remote_score_assertion_results(score),
        missed_expectations=_remote_score_missed_expectations(score),
        improvement_prompts=_remote_score_improvement_prompts(score),
        trace_id=str(turn.get("trajectory_id")) if turn.get("trajectory_id") else None,
    )


def _remote_score_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _remote_score_critical_failures(score: dict) -> tuple[str, ...]:
    failures = score.get("critical_failures")
    if isinstance(failures, list):
        messages = []
        for failure in failures:
            if isinstance(failure, dict):
                message = failure.get("message") or failure.get("critical_failure") or failure.get("id")
                if message:
                    messages.append(str(message))
            elif failure:
                messages.append(str(failure))
        if messages:
            return tuple(messages)
    return tuple(["server_scored_failure"] * int(_remote_score_value(score.get("critical_failure_count", 0))))


def _remote_score_step_statuses(score: dict) -> dict[str, str]:
    step_results = score.get("step_results")
    if isinstance(step_results, dict):
        statuses: dict[str, str] = {}
        for step_id, result in step_results.items():
            if isinstance(result, dict):
                statuses[str(step_id)] = str(result.get("status") or "unknown")
            elif result:
                statuses[str(step_id)] = str(result)
        return statuses
    step_reports = score.get("step_reports")
    if isinstance(step_reports, list):
        return {
            str(item["step_id"]): str(item.get("status") or "unknown")
            for item in step_reports
            if isinstance(item, dict) and item.get("step_id")
        }
    return {}


def _remote_score_dimensions(score: dict) -> dict[str, float]:
    metric_scores = score.get("metric_scores")
    if isinstance(metric_scores, list):
        dimensions: dict[str, float] = {}
        for item in metric_scores:
            if isinstance(item, dict) and item.get("metric"):
                dimensions[str(item["metric"])] = _remote_score_value(item.get("value", 0.0))
        return dimensions
    scores = score.get("scores")
    if isinstance(scores, dict):
        return {str(key): _remote_score_value(value) for key, value in scores.items()}
    return {}


def _remote_score_assertion_results(score: dict) -> tuple[dict[str, Any], ...]:
    results = score.get("assertion_results")
    if isinstance(results, list):
        return tuple(dict(item) for item in results if isinstance(item, dict))
    return ()


def _remote_score_missed_expectations(score: dict) -> tuple[str, ...]:
    missed = score.get("missed_expectations")
    if isinstance(missed, list):
        return tuple(str(item) for item in missed)
    return tuple(step_id for step_id, status in _remote_score_step_statuses(score).items() if status != "completed")


def _remote_score_improvement_prompts(score: dict) -> tuple[str, ...]:
    prompts = score.get("improvement_prompts") or score.get("suggested_improvements")
    if isinstance(prompts, list):
        return tuple(str(item) for item in prompts)
    return ()


def _remote_runtime_max_work_items() -> int:
    raw = os.environ.get("WENDELL_REMOTE_RUNTIME_MAX_WORK_ITEMS", "500")
    try:
        return max(1, int(raw))
    except ValueError:
        return 500


def _preflight_adapter_tool_contracts(
    config: RunnerConfig,
    tool_contracts: list[dict[str, Any]],
    *,
    suite_slug: str,
    config_path: Path,
) -> None:
    required_tools = sorted({str(contract.get("name")) for contract in tool_contracts if contract.get("name")})
    if not required_tools:
        return
    if not config.agent_command:
        raise ValueError(f"agent_command is missing; run `wendell suites configure --suite {suite_slug} --config {config_path}`.")
    command_args = _agent_command_args(config.agent_command)
    handshake = {
        "type": "wendell.handshake",
        "required_tools": required_tools,
        "tool_contracts": tool_contracts,
    }
    try:
        completed = subprocess.run(
            command_args,
            input=json.dumps(handshake),
            text=True,
            capture_output=True,
            cwd=config.project_dir,
            timeout=config.agent_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"adapter handshake timed out after {config.agent_timeout_seconds:g}s") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or "no stderr"
        raise ValueError(f"adapter handshake failed: {stderr}")
    try:
        payload = json.loads(completed.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("adapter handshake must print JSON with `supported_tools`.") from exc
    if not isinstance(payload, dict):
        raise ValueError("adapter handshake must print a JSON object.")
    supported = payload.get("supported_tools")
    if not isinstance(supported, list):
        raise ValueError("adapter handshake response must include `supported_tools`.")
    supported_tools = {str(item) for item in supported}
    missing = [name for name in required_tools if name not in supported_tools]
    if missing:
        raise ValueError(
            "adapter is missing required tool(s): "
            + ", ".join(missing)
            + f". Run `wendell suites configure --suite {suite_slug} --config {config_path}` to regenerate the adapter, or pass --skip-preflight to bypass intentionally."
        )


def _call_remote_runtime_agent(config: RunnerConfig, work: dict) -> dict:
    if not config.agent_command:
        raise ValueError("wendell run requires `agent_command` in the config file for remote runtime suites.")
    scenario = _agent_visible_scenario(work.get("scenario") or {})
    transcript = work.get("transcript") or []
    latest_message = ""
    if transcript:
        last = transcript[-1]
        if isinstance(last, dict):
            latest_message = str(last.get("text") or last.get("message") or "")
    payload = {
        "schema_version": "wendell.agent_input.v1",
        "task": "Respond as an agent in a Wendell remote runtime scenario.",
        "scenario": scenario,
        "transcript": _agent_visible_value(transcript),
        "available_tools": _agent_visible_available_tools(work.get("available_tools") or []),
        "case": _agent_visible_value(
            work.get("case")
            or {
            "case_id": str(scenario.get("id") or work.get("scenario_execution", {}).get("id") or "case"),
            "request": latest_message,
            }
        ),
        "instruction": (
            "Return JSON with `message`, `tool_calls`, and optional `metrics`. "
            "Use names from `available_tools`; Wendell scoring criteria are not included."
        ),
    }
    try:
        command_args = _agent_command_args(config.agent_command)
        completed = subprocess.run(
            command_args,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=config.project_dir,
            timeout=config.agent_timeout_seconds,
            check=False,
        )
    except ValueError as exc:
        return {
            "message": f"[external agent command invalid: {exc}]",
            "tool_calls": [],
            "metrics": {"agent_error": True, "adapter_contract_error": "agent_command is invalid"},
        }
    except subprocess.TimeoutExpired:
        return {
            "message": f"[external agent timed out after {config.agent_timeout_seconds:g}s]",
            "tool_calls": [],
            "metrics": {"agent_error": True, "agent_timeout": True},
        }
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or "no stderr"
        return {"message": f"[external agent failed: {stderr}]", "tool_calls": [], "metrics": {"agent_error": True}}
    output = completed.stdout.strip()
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        preview = output[:500]
        return {
            "message": preview,
            "tool_calls": [],
            "metrics": {
                "agent_error": True,
                "adapter_contract_error": "stdout must be a JSON object",
            },
        }
    if not isinstance(parsed, dict):
        return {
            "message": str(parsed),
            "tool_calls": [],
            "metrics": {
                "agent_error": True,
                "adapter_contract_error": "stdout must be a JSON object",
            },
        }
    parsed = _normalize_agent_command_result(parsed)
    return _validated_agent_command_result(parsed)


def _agent_command_args(command: str) -> list[str]:
    try:
        args = shlex.split(command)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    if not args:
        raise ValueError("agent_command is empty")
    return args


def _validated_agent_command_result(parsed: dict) -> dict:
    message = parsed.get("message")
    tool_calls = parsed.get("tool_calls", [])
    metrics = parsed.get("metrics", {})
    if not isinstance(message, str):
        return {
            "message": "" if message is None else str(message),
            "tool_calls": tool_calls if isinstance(tool_calls, list) else [],
            "metrics": {"agent_error": True, "adapter_contract_error": "message must be a string"},
        }
    if not isinstance(tool_calls, list):
        return {
            "message": message,
            "tool_calls": [],
            "metrics": {"agent_error": True, "adapter_contract_error": "tool_calls must be a list"},
        }
    if not isinstance(metrics, dict):
        return {
            "message": message,
            "tool_calls": tool_calls,
            "metrics": {"agent_error": True, "adapter_contract_error": "metrics must be an object"},
        }
    return {"message": message, "tool_calls": tool_calls, "metrics": metrics}


def _agent_visible_scenario(scenario: dict) -> dict:
    if not isinstance(scenario, dict):
        return {}
    return _agent_visible_dict(scenario)


def _agent_visible_dict(payload: dict) -> dict[str, Any]:
    return {
        str(key): _agent_visible_value(value)
        for key, value in payload.items()
        if not _is_hidden_eval_field(str(key))
    }


def _agent_visible_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _agent_visible_dict(value)
    if isinstance(value, list):
        return [_agent_visible_value(item) for item in value]
    return value


def _is_hidden_eval_field(field_name: str) -> bool:
    normalized = field_name.lower()
    hidden_eval_fields = {
        "rubric",
        "rubric_snapshot",
        "success_criteria",
        "failure_criteria",
        "hidden_facts",
        "source_lineage",
        "terminal_outcome",
        "expected_outcome",
        "expected_result",
        "expected_response",
        "expected_answer",
        "answer_key",
        "gold_answer",
        "golden_answer",
        "oracle_answer",
        "scoring",
        "scoring_criteria",
        "evaluation",
        "evaluation_notes",
    }
    return normalized in hidden_eval_fields


def _agent_visible_available_tools(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    hidden_tool_fields = {
        "step_id",
        "step_ids",
        "requires",
        "required",
        "required_before",
        "source_lineage",
        "rubric",
        "assertions",
    }
    visible_tools: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        visible_tools.append(
            {
                str(key): _agent_visible_value(value)
                for key, value in tool.items()
                if str(key) not in hidden_tool_fields and not _is_hidden_eval_field(str(key))
            }
        )
    return visible_tools


def _normalize_agent_command_result(parsed: dict) -> dict:
    if isinstance(parsed.get("structured_output"), dict):
        return parsed["structured_output"]
    result = parsed.get("result")
    if isinstance(result, dict):
        return result
    if isinstance(result, str) and result.strip().startswith("{"):
        try:
            decoded = json.loads(result)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            return decoded
    return parsed


def _upload_remote_run(config: RunnerConfig, suite: SuiteResult) -> dict:
    client = RemoteWendellClient.from_env(_effective_api_url(config.api_url), config.api_key_env)
    scenario_pack = {}
    if config.world and config.scenario_pack:
        scenario_pack = client.fetch_scenario_pack(
            config.world,
            config.scenario_pack,
            world_version=config.world_version,
            scenario_pack_version=config.scenario_pack_version,
        )
    run = client.create_run(
        {
            "project": config.project,
            "world": config.world,
            "world_version": config.world_version,
            "scenario_pack": config.scenario_pack,
            "scenario_pack_version": config.scenario_pack_version,
            "mode": config.mode,
            "external_ci_ref": config.external_ci_ref,
            "metadata": config.metadata,
            "suite_score": suite.suite_score,
            "critical_failure_count": suite.critical_failure_count,
        }
    )
    run_id = str(run["run_id"])
    run_envelope = client.get_run(run_id)
    scenario_execution_by_key = {
        item["scenario_key"]: item["id"]
        for item in run_envelope.get("scenario_executions", [])
        if item.get("scenario_key")
    }
    uploads = []
    for result in suite.scenario_results:
        if not result.trajectory:
            continue
        scenario_execution_id = scenario_execution_by_key.get(result.scenario_id)
        if not scenario_execution_id:
            continue
        trace_response = client.upload_trace(
            run_id,
            _trace_payload(result.trajectory, scenario_execution_id),
        )
        result_response = client.upload_result(
            run_id,
            {"trajectory_id": trace_response["trajectory_id"]},
        )
        uploads.append(
            {
                "scenario_id": result.scenario_id,
                "scenario_execution_id": scenario_execution_id,
                "trajectory_id": trace_response["trajectory_id"],
                "run_score_id": result_response.get("run_score_id"),
                "gate_decision_id": result_response.get("gate_decision_id"),
                "ci_status": result_response.get("ci_status"),
            }
        )
    completion = client.complete_run(run_id) if uploads else {}
    return {
        "run_id": run_id,
        "url": run.get("url"),
        "scenario_pack_id": scenario_pack.get("scenario_pack_id"),
        "world_version_id": scenario_pack.get("world_version_id"),
        "status": completion.get("status"),
        "uploaded": True,
        "uploaded_scenarios": len(uploads),
        "uploads": uploads,
    }


def _trace_payload(trajectory: dict, scenario_execution_id: str) -> dict:
    return _redact_outbound_value({
        "external_trace_id": trajectory.get("run_id"),
        "scenario_execution_id": scenario_execution_id,
        "agent_name": trajectory.get("agent_name", "wendell-cli-agent"),
        "complete": True,
        "metadata": dict(trajectory.get("metadata") or {}),
        "events": [
            {
                "index": event["index"],
                "type": event["type"],
                "source": event["source"],
                "message": event.get("message"),
                "payload": dict(event.get("payload") or {}),
                "tool_calls": list(event.get("tool_calls") or []),
                "observation": event.get("observation"),
                "evaluation": event.get("evaluation"),
                "metrics": dict(event.get("metrics") or {}),
            }
            for event in trajectory.get("events", [])
        ],
    })


_INLINE_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\b(?:wpk|opk|pk)_(?:live|test)_[A-Za-z0-9_-]{8,}"),
)


def _redact_outbound_value(value: Any, *, key: str | None = None) -> Any:
    if _is_secret_field_name(key or ""):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _redact_outbound_value(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_outbound_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_outbound_value(item) for item in value]
    if isinstance(value, str):
        redacted = value
        for pattern in _INLINE_SECRET_PATTERNS:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted
    return value


def _is_secret_field_name(field_name: str) -> bool:
    normalized = field_name.lower().replace("-", "_")
    secret_fragments = (
        "api_key",
        "apikey",
        "auth_header",
        "authorization",
        "access_token",
        "refresh_token",
        "id_token",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "cookie",
        "session_token",
        "private_key",
    )
    return any(fragment in normalized for fragment in secret_fragments)


if __name__ == "__main__":
    raise SystemExit(main())
