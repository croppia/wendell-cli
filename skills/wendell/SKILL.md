---
name: wendell
description: Use when installing the Wendell CLI, configuring wendell.toml, creating Wendell repo skills, turning playbooks/SOPs/tool contracts/tickets into hosted agent test suites, running Wendell suites locally or in CI, or debugging Wendell gates, traces, and regressions.
---

# Wendell

Use Wendell to turn business playbooks into hosted agent test suites and run a production agent against those suites locally or in CI.

## Start by inspecting the repo

Before changing files, inspect:

- Existing agent entrypoints, adapters, prompts, tool contracts, and test commands.
- CI workflows and secret-management conventions.
- Playbooks, SOPs, policy docs, ticket examples, or known regression notes.
- Existing `wendell.toml`, `.wendell/`, or `scripts/wendell_agent_adapter.py` files.

Prefer existing repo scripts and docs over inventing a new workflow.

## Install and authenticate

Install the published CLI:

```bash
curl -fsSL https://www.wendellai.com/install | bash
wendell --help
```

For first-time local setup:

```bash
wendell register
wendell whoami
```

For CI, use `WENDELL_INKPASS_API_KEY` from the repo's secret store. Do not write API keys, passwords, raw transcripts, or generated run outputs into committed files or skill instructions.

## Configure a hosted suite

If a suite already exists:

```bash
wendell suites configure --suite <suite-slug> --project <project-slug>
wendell doctor --config wendell.toml --validate
```

This writes `wendell.toml` and usually `scripts/wendell_agent_adapter.py`. Replace or edit the generated adapter so it calls the production agent. The adapter must read Wendell JSON from stdin and print the agent response JSON to stdout.

## Create a suite from a playbook

When source material is available, create a Playbook draft:

```bash
wendell playbook create \
  --name "<suite name>" \
  --workflow-summary "<one sentence workflow summary>" \
  --source <playbook-or-policy-file> \
  --project-ref <project-slug> \
  --domain <domain-slug> \
  --extract
```

Then review, answer required questions, approve, publish, and configure:

```bash
wendell playbook review <draft-id>
wendell playbook approve <draft-id> --reviewer "<reviewer>" --generate-suite
wendell suites publish --draft <draft-id>
wendell suites configure --suite <suite-slug> --project <project-slug>
```

Ask for confirmation before creating hosted production resources when the user's request is ambiguous.

## Run and report

Run the suite:

```bash
wendell run --suite <suite-slug> --config wendell.toml
```

For CI gating, set `mode = "blocking"` in `wendell.toml` only when the user wants failures to return a nonzero exit code. Otherwise keep advisory mode.

When debugging failures, inspect Wendell's scenario summary, gate reason, trace evidence, missing workflow steps, Playbook rule id, assertion id, and suggested fix prompt. Report concrete failing behaviors, not just scores.

## Commit boundaries

Usually commit only:

- `wendell.toml`
- The production agent adapter, usually `scripts/wendell_agent_adapter.py`
- Intentional CI and docs updates

Do not commit credentials, raw customer data, generated run JSON, private reports, or temporary local state.
