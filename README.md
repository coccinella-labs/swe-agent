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

All 14 coccinella-labs repos have five workflows:

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
python agent.py list          # List all coccinella-labs repos
python agent.py cloned        # List cloned repos in /tmp
python agent.py clone <repo>  # Clone a specific repo
python agent.py clone-all     # Clone all 14 repos
python agent.py capabilities  # Show agent capabilities
```

## Examples

```bash
# Basic run - shows agent info
python agent.py

# List all 14 coccinella-labs repos
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
├── agent.py        # Main agent with repo awareness
├── README.md       # This file
└── .pre-commit-config.yaml
```

## Repos

The agent manages these coccinella-labs repos:
- general, hub, react-template, swe-agent, gh-pin-repo, config, cli, bot, license, warnings, json-repo, gitkeep, .github, skills

## Skills

Skills are loaded from coccinella-labs/skills repo:
- Developer tools, personas

## Experiment

Testing the agent in action:

```bash
# Show capabilities (all 14 repos cloned, skills loaded)
$ python3 /tmp/swe-agent/agent.py capabilities
Agent: coccinella-labs-agent
Total repos: 14
Cloned repos: ['general', 'react-template', 'swe-agent', 'gh-pin-repo', 'config', 'cli', 'bot', 'license', 'warnings', 'json-repo', 'gitkeep', '.github', 'skills']
Skills loaded: ['developer_tools', 'personas']

# Clone all repos
$ python3 /tmp/swe-agent/agent.py clone-all
Cloning all repos...
Cloned: ['general', 'react-template', 'swe-agent', 'gh-pin-repo', 'config', 'cli', 'bot', 'license', 'warnings', 'json-repo', 'gitkeep', '.github', 'skills']

# Clone single repo
$ python3 /tmp/swe-agent/agent.py clone bot
Cloning bot...
Success: True

# List all repos
$ python3 /tmp/swe-agent/agent.py list
Coccinella Labs Repositories:
  - general
  - react-template
  - swe-agent
  - gh-pin-repo
  - config
  - cli
  - bot
  - license
  - warnings
  - json-repo
  - gitkeep
  - .github
  - skills

# Python API usage
$ python3 -c "
from agent import SWEAgent
agent = SWEAgent('test')
result = agent.clone_repo('config')
print(f'Clone config: {result}')
print(f'Cloned repos: {agent.list_cloned_repos()}')
"
Clone config: True
Cloned repos: ['config', 'skills']

# Basic run
$ python3 /tmp/swe-agent/agent.py
Agent: coccinella-labs-agent
Total repos: 14
Cloned repos: ['skills']
Skills loaded: ['developer_tools', 'personas']
```