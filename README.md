<p align="center">
  <img src="https://raw.githubusercontent.com/Coccinella-Labs/swe-agent/main/.github/assets/thumbnail.png" alt="swe-agent" width="100%">
</p>

# SWE Agent - Coccinella Labs

Software engineering agent for coccinella-labs organization with coccinella-labs repos awareness.

## Overview

This agent knows all coccinella-labs repositories and can clone them to /tmp/coccinella-labs/ for operations.

## Features

- Loads repos dynamically from coccinella-labs/config
- Detects cloned repos in /tmp/coccinella-labs/
- Loads skills from coccinella-labs/skills
- CLI for repo management

## Workflows

Repos in coccinella-labs share the same five workflows. The live repo list is kept in coccinella-labs/config:

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| **CI** | push/PR to main | Lint & test |
| **Release** | push to main (auto) or manual | Auto version bump & release |
| **Automation** | daily schedule + manual | Sync, backup, report |
| **Label PR** | PR opened/updated | Auto-label PRs |
| **Auto Bot** | push/PR | Bot automation |

### Automated Release

Release is fully automated - no manual tagging needed!

**How it works:**
1. On push to `main`, checks commits since last tag
2. If `feat:` commits → bump **minor** version (e.g., v1.0 → v1.1.0)
3. If `fix:` commits → bump **patch** version (e.g., v1.1.0 → v1.1.1)
4. Creates tag automatically
5. Generates release notes with features, bug fixes, docs
6. Creates GitHub release

**Conventional commits:**
```
feat: add new feature     # → minor release
fix: bug fix              # → patch release
docs: update readme       # → no release
```

**Manual trigger (any commit type):**
```bash
gh workflow run release.yml -f version=patch --repo coccinella-labs/repo
gh workflow run release.yml -f version=minor --repo coccinella-labs/repo
gh workflow run release.yml -f version=major --repo coccinella-labs/repo
```

### Release Workflow Fixes

During development, we encountered and fixed several issues:

1. **Tag not detected**: Added `git fetch --tags --force origin` after checkout to ensure latest tags are available
2. **Wrong tag used**: Changed from `git describe` to using `${{ steps.bump.outputs.NEW_TAG }}` to get the correct new tag
3. **Missing id on step**: Added `id: bump` to the bump step so its outputs can be referenced
4. **Changelog link broken**: Fixed `LAST_TAG` in release notes to use `HEAD^` (not `origin/main HEAD^`)
5. **Newline in tag**: Added `| tr -d '\n'` to remove trailing newlines from git describe output

**Release workflow key steps:**
```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0

- name: Fetch all tags
  run: git fetch --tags --force origin

- name: Bump version and tag
  id: bump
  run: |
    LAST_TAG=$(git describe --tags --abbrev=0 origin/main 2>/dev/null || echo "")
    # ... calculate new version ...
    NEW_TAG="v${MAJOR}.${MINOR}.${PATCH}"
    echo "NEW_TAG=$NEW_TAG" >> $GITHUB_OUTPUT

- name: Create Release
  run: |
    TAG="${{ steps.bump.outputs.NEW_TAG }}"
    # Create release for TAG
```

### Testing Release Workflow

```bash
# Check tags vs releases are in sync
for repo in general license react-template gitkeep warnings json-repo gh-pin-repo config swe-agent cli bot skills hub; do
  tags=$(gh api repos/coccinella-labs/$repo/tags --jq '.[].name' | head -1)
  releases=$(gh api repos/coccinella-labs/$repo/releases --jq '.[0].tag_name')
  if [ "$tags" = "$releases" ]; then
    echo "$repo: ✅ $tags"
  else
    echo "$repo: ❌ tag=$tags release=$releases"
  fi
done
```

## Troubleshooting

### Tag exists but no release
- Check if workflow ran successfully
- Verify workflow has latest fixes

### Wrong version bump
- Check commit prefix (`feat:` → minor, `fix:` → patch, `docs:` → none)

### Broken changelog link
- Ensure `| tr -d '\n'` after git describe
- Use `HEAD^` not `origin/main HEAD^`

### Tag/release mismatch
- Use `${{ steps.bump.outputs.NEW_TAG }}` not git describe
- Add `git fetch --tags --force origin` after checkout

### Automation Workflow

All repos have an automation workflow that runs daily and on-demand:

| Action | Description |
|--------|-------------|
| **sync** | Compares config repos.json with actual GitHub repos |
| **report** | Generates org report with repo list |
| **all** | Runs both sync and report |

**Usage:**
```bash
gh workflow run automation.yml -f action=sync --repo coccinella-labs/repo
gh workflow run automation.yml -f action=report --repo coccinella-labs/repo
```

**Release workflow file:**
```yaml
# .github/workflows/release.yml
name: Release

on:
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      version:
        description: 'Version bump (major, minor, patch)'
        default: 'patch'
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
```

Workflows added to all repos:

```bash
$ for dir in /tmp/coccinella-labs/*/; do
    repo=$(basename "$dir")
    mkdir -p "$dir/.github/workflows"
    cp workflows/*.yml "$dir/.github/workflows/"
    git -C "$dir" add -A && git -C "$dir" commit -m "feat: include workflows"
    git -C "$dir" push
  done
Added workflows to bot
Added workflows to cli
Added workflows to config
Added workflows to general
Added workflows to gh-pin-repo
Added workflows to json-repo
Added workflows to license
Added workflows to react-template
Added workflows to skills
Added workflows to swe-agent
Added workflows to warnings
To https://github.com/coccinella-labs/general.git
To https://github.com/coccinella-labs/skills.git
To https://github.com/coccinella-labs/gh-pin-repo.git
To https://github.com/coccinella-labs/bot.git
To https://github.com/coccinella-labs/react-template.git
To https://github.com/coccinella-labs/warnings.git
To https://github.com/coccinella-labs/cli.git
To https://github.com/coccinella-labs/json-repo.git
To https://github.com/coccinella-labs/config.git
To https://github.com/coccinella-labs/license.git
To https://github.com/coccinella-labs/gitkeep.git
```

## Usage

```bash
python agent.py list           # List all coccinella-labs repos
python agent.py cloned         # List cloned repos in /tmp
python agent.py clone <repo>   # Clone a specific repo
python agent.py clone-all      # Clone every repo the agent knows about
python agent.py capabilities   # Show agent capabilities
python agent.py task '<json>'  # Run one task (read, write, test, pr, search, diff, lint, solve)
```

## Tasks

`execute_task` runs one task against a cloned repo. A task is a JSON object
with `repo` and `operation`:

```bash
python agent.py task '{"repo": "config", "operation": "read", "path": "repos.json"}'
python agent.py task '{"repo": "nope", "operation": "read", "path": "x"}'
# {"repo": "nope", "operation": "read", "status": "error",
#  "error": "repo 'nope' is not cloned under /tmp"}
```

| Operation | Needs | Does |
|-----------|-------|------|
| read | `path`, or up to 8 `paths` | Returns the file text from the clone, single file or joined multi-file view |
| write | `path`, string `content` | Writes the file into the clone, uncommitted |
| test | nothing | Runs the repo's own test command |
| pr | `branch`, `message`, `"confirm": true` | Commits the working tree on a new branch, pushes, opens a PR |
| search | `pattern` | git grep inside the clone, returns line matches |
| diff | nothing | git diff of the clone's uncommitted work |
| lint | nothing | ruff over the clone's Python files |
| solve | `instruction`, or `issue` plus a model key | Model loop over view, search, edit, diff, lint, test; ends with a diff. Never pushes |

Rules that hold for every operation:

- The target repo must already be cloned under `/tmp/coccinella-labs/`.
- Paths are resolved inside the clone; anything escaping it is refused.
- The result is a dict with `status` of `ok` or `error`, never a raise.
- `pr` is the only operation that reaches GitHub and it refuses to run
  without `"confirm": true` set in the task itself.
- `test` picks the repo's own command: a Makefile `test:` target, then
  pytest with the interpreter running the agent, then an npm `test` script.

## Solve loop

`solve` is the model-driven operation. It fetches context, clones the repo
if needed, and loops the model through a command allowlist until it calls
`submit`. The delivered result is a diff from the clone; nothing is pushed.

```bash
export ANTHROPIC_API_KEY=...        # required; never committed
export SWE_AGENT_MODEL=claude-sonnet-5-5   # optional override
python agent.py task '{"repo": "calculator", "operation": "solve",
  "issue": 4, "max_turns": 12, "cost_cap_usd": 2.0}'
```

Behavior worth knowing before first use:

- The model can only issue `view`, `search`, `edit`, `diff`, `lint`,
  `test`, and `submit`. `view` takes `path` or up to 8 `paths`. There is
  no shell, no network, and no pull-request command in the loop.
- `lint` runs before `submit` is honored; a failing lint sends the loop
  back to work.
- `edit` writes a whole file inside the clone; the path guard refuses
  anything escaping it.
- Turn cap defaults to 10 (hard ceiling 50), the cost cap defaults to 2 USD
  with an internal price table, and the driver refuses the next call once
  the cumulative estimate passes the cap.
- Every turn goes to a JSONL transcript under
  `/tmp/coccinella-labs/transcripts/<repo>/` for auditing.
- Costs are rough estimates used only for the cap, never billing figures.

## Sandbox

Operation execution goes through a `SandboxExecutor` seam. The default is
`local`, which runs the ops as ordinary subprocesses on the host:

```bash
export SWE_AGENT_SANDBOX=local    # default; plain subprocess execution
```

Setting `docker` selects the containerized executor: each op runs inside
the sandbox image (`coccinella-labs/swe-agent-sandbox`, built from
`docker/sandbox.Dockerfile`) with `--network none`, `--cap-drop ALL`,
`--read-only` except `/tmp`, resource ceilings, and uid 1001. The daemon
and the model key stay on the host; the container executes code only,
so nothing inside it can touch GitHub or the key.

Selection is fail-closed: a `docker` request that the machine cannot
honor produces an error result, never a silent fallback to local. The
image carries exactly python 3.12, git, pytest 9.1.1, and ruff 0.17.1;
anything else fails closed for lack of the dependency instead of
installing it from the network the container does not have. See
`docs/sandbox-design.md` for the design and `docs/sandbox-smoke.md`
for the build-and-probe procedure.

## Examples

```bash
# Basic run - shows agent info
python agent.py

# List every repo the agent loaded from coccinella-labs/config
python agent.py list

# Clone a specific repo
python agent.py clone bot

# Clone all repos to /tmp/coccinella-labs/
python agent.py clone-all

# Show full capabilities with skills
python agent.py capabilities
```

## Structure

```
swe-agent/
├── agent.py             # Main agent: ops, guard rails, and the solve loop
├── driver.py            # Anthropic Messages client with caps (offline-testable)
├── tests/               # Pytest suite, offline (network and transport stubbed)
├── conftest.py          # Shared stubs; puts the repo root on sys.path
├── requirements-dev.txt # pytest and ruff pins used by CI
├── README.md
└── .pre-commit-config.yaml
```

## Repos

Repos are loaded live from coccinella-labs/config@main (repos.json, 150+
entries today). When config cannot be reached, the agent falls back to the
four repos it depends on: .github, config, skills, swe-agent.

## Skills

Skills are loaded from coccinella-labs/skills repo:
- Developer tools, personas

## Experiment

Testing the agent in action:

```bash
# Show capabilities (fresh run against coccinella-labs/config)
$ python3 agent.py capabilities
Agent: coccinella-labs-agent
Total repos: 153
Cloned repos: []
Skills loaded: ['developer_tools', 'personas']

# List repos
$ python3 agent.py list | head -6
Coccinella Labs Repositories:
  - .github
  - activity
  - agent-sdk
  - agentware
  - ai
  - and

# Task that targets an uncloned repo
$ python3 agent.py task '{"repo": "nope", "operation": "read", "path": "x"}'
{
  "repo": "nope",
  "operation": "read",
  "status": "error",
  "error": "repo 'nope' is not cloned under /tmp"
}

# Python API usage
$ python3 -c "
from agent import SWEAgent
agent = SWEAgent('test')
result = agent.clone_repo('config')
print(f'Clone config: {result}')
print(f'Cloned repos: {agent.list_cloned_repos()}')
"
Clone config: True
Cloned repos: ['config']
```
