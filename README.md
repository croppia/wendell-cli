# Wendell CLI

Wendell is a Python CLI for running production agents against hosted Wendell test suites.

The CLI is advisory by default: it reports scores, captures traces, and returns a successful process exit unless the project explicitly enables blocking gates. In blocking mode, `wendell run` returns a nonzero exit code when gates fail.

The `wendell` CLI package is open source under the MIT License. The public CLI
does not include the hosted Wendell service, internal suite compiler, scoring
service, web app, or production infrastructure.

## Agent skill

[![skills.sh](https://skills.sh/b/WendellOfficial/wendell-cli)](https://skills.sh/WendellOfficial/wendell-cli)

Install the Wendell skill for coding agents that support Vercel's Skills
Registry:

```bash
npx skills add WendellOfficial/wendell-cli --skill wendell
```

The skill teaches agents how to install the CLI, configure `wendell.toml`, build
hosted suites from playbooks, wire the runner into CI, and inspect Wendell
failures without embedding credentials.

## Intended split

- Wendell system: turns reviewed Playbooks into hosted test suites, owns rubrics, stores traces, and reports regressions.
- Wendell CLI runner: runs in a repo or CI job, fetches scenario work from a published suite, invokes the customer's agent adapter, uploads turns/results, and prints a concise summary.

## Install

Install the latest published CLI:

```bash
uv tool install --force wendell
```

Alternative installers:

```bash
pipx install --force wendell
python3 -m pip install --user --upgrade wendell
```

The packaged CLI does not require an OpenAI, OpenRouter, Anthropic, or other LLM
provider key to install, register, create Playbook drafts, or run hosted suites.
Wendell's hosted service generates suites from reviewed Playbooks. Your own
`agent_command` may need provider credentials if your agent uses an LLM.

For unreleased changes from current main:

```bash
python3 -m pip install "git+https://github.com/WendellOfficial/wendell-cli.git"
```

For release validation from this repository, run:

```bash
python -m pytest
python -m build
python -m twine check dist/*
```

The tests exercise the hosted-suite contract path against local mock clients.
They assert the customer-facing payload uses `wendell.agent_input.v1` and does
not expose rubrics, hidden facts, source lineage, terminal outcomes, or
success/failure criteria.

The package installs the `wendell` command only. Customer-facing examples and
CI jobs should use `wendell run`, not compatibility aliases.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]" build twine
pytest
wendell --help
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines and
[SECURITY.md](SECURITY.md) for vulnerability reporting.

## Login

For a first-time Wendell runner, install and register the CLI:

```bash
curl -fsSL https://www.wendellai.com/install | bash
wendell register
```

Registration creates a short-lived runner session, asks for email/password and agent details, and stores a scoped local runner credential.

For local development with an existing InkPass API key, store it once:

```bash
wendell login --api-key-stdin --validate
wendell auth status
```

Credentials are stored at `~/.config/wendell/credentials.json` by default, or `$WENDELL_CONFIG_HOME/credentials.json` when set. The directory is created with `0700` permissions and the credentials file with `0600` permissions.

CI should continue to use `WENDELL_INKPASS_API_KEY`; environment variables take precedence over stored credentials.

## Minimal config

```toml
project = "support-agent"
mode = "advisory"
agent = "support_agent"
agent_command = "python scripts/wendell_agent_adapter.py"
agent_timeout_seconds = 120
upload_traces = true

[gates]
suite_min_score = 0.80
scenario_min_score = 0.75
critical_failures_allowed = 0
```

When you run a hosted suite with `wendell run --suite`, Wendell resolves the
locked runtime assets and scoring contract from the published suite.

## Production app test loop

Use `wendell run` as the local and CI entry point for production agents.

```bash
wendell suites configure --suite refund-agent-regression --project support-agent
wendell doctor --config wendell.toml --validate
wendell run --suite refund-agent-regression --config wendell.toml
```

Your adapter receives a production-facing JSON payload. Wendell does not send
the scoring rubric, hidden facts, expected outcomes, or success/failure criteria
to the agent process.

```json
{
  "schema_version": "wendell.agent_input.v1",
  "task": "Respond as a support agent for Support Agent.",
  "policies": ["Inspect the request before completing the workflow."],
  "transcript": [{"speaker": "customer", "text": "I need help with this request."}],
  "available_tools": [
    {
      "name": "workflow_console.inspect_request",
      "arguments": {"case_id": "str"},
      "description": "inspect request"
    }
  ],
  "case": {"case_id": "case_123", "request": "I need help with this request."},
  "scenario": {"id": "case_123", "kind": "realistic"}
}
```

The adapter must print JSON:

```json
{
  "message": "I inspected the request and recorded the required evidence.",
  "tool_calls": [
    {"name": "workflow_console.inspect_request", "args": {"case_id": "case_123"}}
  ],
  "metrics": {}
}
```

`wendell suites configure` creates:

- `wendell.toml`: the hosted-suite run config
- `scripts/wendell_agent_adapter.py`: the adapter boundary for your production agent

The generated adapter is intentionally not a fake passing agent. It requires
`WENDELL_APP_AGENT_COMMAND` or replacement code that calls your production agent
and maps its real actions back to Wendell tool names. Wendell no longer
generates a passing example adapter because that makes validation look real
without testing the customer's agent.

Wendell parses `agent_command` and `WENDELL_APP_AGENT_COMMAND` into command-line
arguments and executes them directly, without a shell. Keep those values to an
executable plus arguments. Put pipes, redirects, command chaining, or
environment setup inside your adapter script or CI environment instead.

Create, review, and publish hosted suites with the `wendell playbook` and
`wendell suites` commands documented in the SDK docs. New customer projects
should use hosted suite creation and `wendell run`; the public CLI package does
not ship the internal local compiler/runtime.

For CI, set `mode = "blocking"` so failed gates return a nonzero exit code:

```toml
project = "support-agent"
mode = "blocking"
agent = "support_agent"
agent_command = "python scripts/wendell_agent_adapter.py"
agent_timeout_seconds = 120
upload_traces = true

[gates]
suite_min_score = 0.90
scenario_min_score = 0.85
critical_failures_allowed = 0
```

Failure output includes the failed scenario, gate reason, incomplete workflow
steps, Playbook rule id, assertion id, trajectory event indexes, and fix prompts
when the suite provides assertions.

`agent_timeout_seconds` controls the maximum duration for one adapter invocation.
The default is 120 seconds; increase it for production agents that need more
time for hosted tools or long-running workflow turns.

### What to commit

Commit these files:

- `wendell.toml`
- your production adapter, usually `scripts/wendell_agent_adapter.py`

Do not commit secrets, raw customer transcripts, API keys, or generated run
outputs. Keep production credentials in your CI secret store.

### GitHub Actions

```yaml
name: Wendell Agent Tests

on:
  pull_request:
  push:
    branches: [main]

jobs:
  wendell:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - name: Install Wendell
        run: |
          python -m pip install --upgrade pip
          python -m pip install wendell
      - name: Run Wendell
        env:
          WENDELL_INKPASS_API_KEY: ${{ secrets.WENDELL_INKPASS_API_KEY }}
          WENDELL_APP_AGENT_COMMAND: python scripts/run_my_agent.py
        run: |
          set +e
          wendell run \
            --suite refund-agent-regression \
            --config wendell.toml \
            --json | tee wendell-run.json
          status=${PIPESTATUS[0]}
          set -e

          python - <<'PY'
          import json
          import os
          from pathlib import Path

          summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
          if not summary_path:
              raise SystemExit(0)

          payload = json.loads(Path("wendell-run.json").read_text(encoding="utf-8"))
          remote = payload.get("remote") if isinstance(payload.get("remote"), dict) else {}
          suite = payload.get("suite") if isinstance(payload.get("suite"), dict) else {}

          lines = [
              "## Wendell report",
              "",
              f"- Gate: `{payload.get('decision', 'unknown')}`",
              f"- Run: `{remote.get('run_id', 'unknown')}`",
          ]
          if suite.get("suite_score") is not None:
              lines.append(f"- Score: `{suite['suite_score']}`")
          if remote.get("private_report_url"):
              lines.append(f"- Private report: {remote['private_report_url']}")
          with Path(summary_path).open("a", encoding="utf-8") as summary:
              summary.write("\n".join(lines) + "\n")
          PY

          exit "$status"
```

For hosted Wendell reporting, keep `upload_traces = true` and provide
`WENDELL_INKPASS_API_KEY` from CI secrets. Add `api_url` only when targeting a
staging, preview, or local API.
The JSON output includes `remote.private_report_url` when Wendell can create a
private dashboard handoff link for the run.

For a newly published hosted suite, create the repo-local config and adapter
boundary first:

```bash
wendell suites show refund-agent-regression
wendell suites configure --suite refund-agent-regression --project refund-agent
```

After a hosted run uploads, inspect its private status and report:

```bash
wendell runs watch run_abc123
wendell runs report run_abc123
```

The intended production flow is:

1. `wendell run` runs on the developer machine or CI worker.
2. It fetches pinned public scenario work and tool schemas from Wendell.
3. It invokes the agent locally through an adapter such as `agent_command`.
4. It captures local traces and uploads agent turns/results back to Wendell for server-side scoring.
5. Advisory mode exits `0`; blocking mode exits nonzero only when explicitly enabled.

Remote uploads authenticate with an InkPass API key sent as `X-API-Key`. The key needs at least `wendell:test-suites:read`, `wendell:runs:create`, and `wendell:runs:read`.
