"""SWE Agent - Software Engineering Agent."""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from driver import (
    DRIVER_PROVIDERS,
    CapReached,
    Driver,
    DriverError,
    get_provider_class,
    provider_name,
)
from sandbox import executor_from_env

ORG = "coccinella-labs"
CONFIG_URL = f"https://raw.githubusercontent.com/{ORG}/config/main/repos.json"

TMP_BASE = "/tmp"

# Used only when the config repository cannot be reached: the repositories the
# agent itself depends on, so a retry against config stays possible.
FALLBACK_REPOS = [".github", "config", "skills", "swe-agent"]

OPERATIONS = ("read", "write", "test", "pr", "search", "solve", "diff", "lint")

SOLVE_COMMANDS = ("view", "search", "edit", "test", "submit", "diff", "lint")

SOLVE_MAX_TURNS = 10


def _env_int_ceiling(name: str, default: int) -> int:
    """Ceiling from the environment for one run; bad values mean absent."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if 1 <= value <= 50 else default


def _env_float_ceiling(name: str, default: float) -> float:
    """Dollar ceiling from the environment; bad values mean absent."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


SOLVE_SYSTEM = """You are a software engineering agent working in a local clone of the org repo {org}/{repo}. Complete the assigned task with the smallest correct change and no drive-by edits.

Reply with EXACTLY ONE JSON object and nothing else, choosing one command:

{{"command": "view", "path": "rel/path/file.py"}}
{{"command": "view", "paths": ["file_a.py", "rel/file_b.py"]}}
{{"command": "search", "pattern": "git grep pattern"}}
{{"command": "edit", "path": "rel/path/file.py", "content": "entire new file text"}}
{{"command": "diff"}}
{{"command": "lint"}}
{{"command": "test"}}
{{"command": "submit"}}

Rules:
- edit replaces the whole file, so 'content' must carry the complete new text.
- paths are relative to the clone root and must never escape it.
- view takes either one "path" or a "paths" list of at most 8 files.
- diff shows the working-tree status and the current edits as a git diff and stat.
- lint runs ruff check on the clone.
- search patterns are git grep regexes applied to tracked files.
- run "lint" and "test" before "submit"; submitting with failing tests or lint errors is premature.
- "submit" ends the run: the edits made up to that point become the delivered diff.
- there is no shell, no network, and no pull-request command available here."""


class SWEAgent:
    """Base class for software engineering agents."""

    def __init__(
        self,
        name: str,
        capabilities: list | None = None,
        tmp_base: str | None = None,
        driver_factory=None,
        on_event=None,
    ):
        self.name = name
        self.capabilities = capabilities or []
        self.repos = []
        self.skills = {}
        self.tmp_base = Path(tmp_base) if tmp_base else Path(TMP_BASE)
        self.driver_factory = driver_factory
        self.on_event = on_event
        self.last_error: str | None = None
        self.executor = executor_from_env()
        self._load_repos()
        self._load_skills()

    def _load_repos(self) -> None:
        """Load repos from the config repo, falling back when it is unreachable."""
        fetched = self._fetch_repo_list()
        if fetched is None:
            self.repos = list(FALLBACK_REPOS)
        else:
            self.repos = fetched

    @staticmethod
    def _fetch_repo_list() -> list[str] | None:
        """Fetch repos.json from config.

        Returns None when the request fails or the payload is not shaped the
        way the agent expects; the caller then decides what to fall back to.
        """
        try:
            result = subprocess.run(
                ["curl", "-fsS", "--max-time", "10", CONFIG_URL],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
        except ValueError:
            return None
        repos = data.get("repos") if isinstance(data, dict) else None
        if not isinstance(repos, list):
            return None
        if not all(isinstance(repo, str) for repo in repos):
            return None
        return repos

    def _load_skills(self) -> None:
        """Load skills from a cloned skills repo when one is present."""
        skill_base = self.tmp_base / "coccinella-labs" / "skills" / "agent"
        for skill_type in ("developer_tools", "personas"):
            self.skills[skill_type] = []
            skill_path = skill_base / f"{skill_type}.md"
            if skill_path.exists():
                self.skills[skill_type].append(skill_path.read_text())

    def get_repo_path(self, repo: str) -> Path | None:
        """Get the local path for a cloned repo under tmp_base."""
        repo_path = self.tmp_base / "coccinella-labs" / repo
        return repo_path if repo_path.exists() else None

    def is_repo_cloned(self, repo: str) -> bool:
        """Check whether the repo is cloned under tmp_base."""
        return self.get_repo_path(repo) is not None

    def list_cloned_repos(self) -> list:
        """List all org repos that are cloned under tmp_base."""
        return [repo for repo in self.repos if self.is_repo_cloned(repo)]

    def clone_repo(self, repo: str, force: bool = False) -> bool:
        """Clone a repo to <tmp_base>/coccinella-labs/<repo>.

        Returns True when the clone ran, or when the repo is already cloned and
        force is not set. On failure the reason is left in self.last_error
        instead of being dropped.
        """
        if self.is_repo_cloned(repo) and not force:
            return True
        target = Path(self.tmp_base) / "coccinella-labs" / repo
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/{ORG}/{repo}.git"
        try:
            result = subprocess.run(
                ["git", "clone", url, str(target)],
                check=False,
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.last_error = f"git clone could not run: {exc}"
            return False
        if result.returncode != 0:
            stderr = (result.stderr or b"").decode(errors="replace").strip()
            detail = stderr.splitlines()[-1] if stderr else "no error output"
            self.last_error = f"git clone failed: {detail}"
            return False
        self.last_error = None
        return target.exists()

    def clone_all(self, force: bool = False) -> list:
        """Clone every known repo under tmp_base. Returns the cloned ones."""
        cloned = []
        for repo in self.repos:
            if self.clone_repo(repo, force):
                cloned.append(repo)
        return cloned

    # --- task interface ---------------------------------------------------

    @staticmethod
    def _run(cmd: list, cwd=None, timeout: int = 300) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def can_handle(self, task) -> bool:
        """True when the task is a dict naming a supported operation."""
        return (
            isinstance(task, dict)
            and task.get("repo") is not None
            and task.get("operation") in OPERATIONS
        )

    def execute_task(self, task: dict) -> dict:
        """Run one task against a cloned org repo.

        A task is a dict with 'repo' and 'operation' (one of 'read', 'write',
        'test', 'pr', 'search', 'solve') plus operation-specific fields:

        - read:   'path' exists in the clone; returns file text.
        - write:  'path' and string 'content'; writes the file, uncommitted.
        - test:   runs the repo's own tests; reports the command and outcome.
        - pr:     needs 'branch' and 'message', and confirm=True; commits the
                  working tree on a new branch, pushes it, and opens a PR.
        - search: string 'pattern'; returns git grep hits inside the clone.
        - solve:  model-driven loop that reads, searches, edits, and tests
                  its way to a diff; needs 'instruction' or 'issue'. Clones
                  for itself; never pushes anything.

        Returns a result dict whose status is 'ok' or 'error'.
        """
        if not isinstance(task, dict):
            return {
                "repo": None,
                "operation": None,
                "status": "error",
                "error": "task must be a dict",
            }
        repo = task.get("repo")
        op = task.get("operation")
        if not isinstance(repo, str) or not repo:
            return {
                "repo": repo,
                "operation": op,
                "status": "error",
                "error": "task needs a 'repo' name",
            }
        if op not in OPERATIONS:
            return {
                "repo": repo,
                "operation": op,
                "status": "error",
                "error": f"unknown operation {op!r}; supported are "
                + ", ".join(OPERATIONS),
            }
        if op == "solve":
            return self._solve(task)
        clone = self.get_repo_path(repo)
        if clone is None:
            return {
                "repo": repo,
                "operation": op,
                "status": "error",
                "error": f"repo '{repo}' is not cloned under {self.tmp_base}",
            }
        handler = getattr(self, f"_op_{op}")
        try:
            return handler(clone, task)
        except KeyError as exc:
            return {
                "repo": repo,
                "operation": op,
                "status": "error",
                "error": f"missing or malformed field: {exc}",
            }

    @staticmethod
    def _task_error(task: dict, message: str) -> dict:
        return {
            "repo": task.get("repo"),
            "operation": task.get("operation"),
            "status": "error",
            "error": message,
        }

    @staticmethod
    def _safe_path(clone: Path, rel) -> Path | None:
        """Resolve rel inside the clone, refusing anything that escapes it."""
        if not isinstance(rel, str) or not rel or rel.startswith("/"):
            return None
        root = clone.resolve()
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            return None
        return target

    def _op_read(self, clone: Path, task: dict) -> dict:
        rel = task.get("path")
        rels = task.get("paths")
        if rels is not None:
            return self._op_read_many(clone, task, rels)
        if rel is None:
            return self._task_error(task, "read needs a 'path' or a 'paths' list")
        target = self._safe_path(clone, rel)
        if target is None:
            return self._task_error(task, f"path {rel!r} escapes the repo clone")
        if not target.is_file():
            return self._task_error(task, f"{rel!r} does not exist in this clone")
        text = target.read_text()
        return {
            "repo": task["repo"],
            "operation": "read",
            "status": "ok",
            "data": {"path": rel, "text": text},
        }

    def _op_read_many(self, clone: Path, task: dict, rels) -> dict:
        """Read up to 8 clone-relative files in one call."""
        if (
            not isinstance(rels, list)
            or not rels
            or not all(isinstance(r, str) for r in rels)
        ):
            return self._task_error(task, "'paths' must be a non-empty list of paths")
        if len(rels) > 8:
            return self._task_error(task, "'paths' holds more than 8 files")
        resolved = []
        for rel in rels:
            target = self._safe_path(clone, rel)
            if target is None:
                return self._task_error(task, f"path {rel!r} escapes the repo clone")
            if not target.is_file():
                return self._task_error(task, f"{rel!r} does not exist in this clone")
            resolved.append((rel, target))
        files = []
        for rel, target in resolved:
            files.append({"path": rel, "text": target.read_text()})
        return {
            "repo": task["repo"],
            "operation": "read",
            "status": "ok",
            "data": {"files": files},
        }

    def _op_write(self, clone: Path, task: dict) -> dict:
        rel = task["path"]
        content = task.get("content")
        if not isinstance(content, str):
            return self._task_error(task, "write needs a string 'content'")
        target = self._safe_path(clone, rel)
        if target is None:
            return self._task_error(task, f"path {rel!r} escapes the repo clone")
        if target.exists() and target.is_dir():
            return self._task_error(task, f"{rel!r} is a directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return {
            "repo": task["repo"],
            "operation": "write",
            "status": "ok",
            "data": {"path": rel, "bytes": len(content.encode())},
        }

    def _op_search(self, clone: Path, task: dict) -> dict:
        """git grep inside the clone; exit 1 means no matches, not failure."""
        pattern = task.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return self._task_error(task, "search needs a string 'pattern'")
        result = self.executor.run(
            ["git", "-C", str(clone), "grep", "-n", "-I", "--", pattern],
            timeout=60,
        )
        if result.returncode not in (0, 1):
            detail = (result.stderr or "").strip().splitlines()[-1:] or ["no output"]
            return self._task_error(task, f"git grep failed: {detail[0]}")
        matches = [line for line in (result.stdout or "").splitlines() if line][:200]
        return {
            "repo": task["repo"],
            "operation": "search",
            "status": "ok",
            "data": {"pattern": pattern, "count": len(matches), "matches": matches},
        }

    @staticmethod
    def _detect_test_command(clone: Path) -> list[str] | None:
        """Pick the repo's own test command, or None when none is recognisable."""
        makefile = clone / "Makefile"
        if makefile.is_file() and any(
            line.split(":", 1)[0].strip() == "test"
            for line in makefile.read_text(errors="replace").splitlines()
        ):
            return ["make", "test"]
        has_pytest = (clone / "pytest.ini").exists() or (clone / "tests").is_dir()
        pyproject = clone / "pyproject.toml"
        if has_pytest or (
            pyproject.is_file() and "pytest" in pyproject.read_text(errors="replace")
        ):
            return [sys.executable, "-m", "pytest", "-q"]
        package_json = clone / "package.json"
        if package_json.is_file():
            try:
                scripts = json.loads(package_json.read_text()).get("scripts", {})
            except ValueError:
                scripts = {}
            if "test" in scripts:
                return ["npm", "test"]
        return None

    def _op_diff(self, clone: Path, task: dict) -> dict:
        steps = (
            ("status", ["git", "status", "--porcelain"]),
            ("stat", ["git", "diff", "--stat"]),
            ("diff", ["git", "diff"]),
        )
        parts = []
        for label, args in steps:
            try:
                result = self.executor.run(args, cwd=clone, timeout=60)
            except subprocess.SubprocessError as exc:
                return self._task_error(task, f"git {label} could not run: {exc}")
            if result.returncode != 0:
                tail = (result.stderr or result.stdout or "").strip()
                message = f"git {label} exited {result.returncode}"
                if tail:
                    message += f"; output tail:\n{tail}"
                return self._task_error(task, message)
            parts.append(f"{label}:\n{(result.stdout or '').strip()}")
        text = "\n\n".join(parts)
        return {
            "repo": task["repo"],
            "operation": "diff",
            "status": "ok",
            "data": {"text": text, "truncated": len(text) > 8000},
        }

    def _op_lint(self, clone: Path, task: dict) -> dict:
        try:
            result = self.executor.run(["ruff", "check", "."], cwd=clone, timeout=120)
        except FileNotFoundError:
            return self._task_error(task, "ruff is not installed in this environment")
        except subprocess.SubprocessError as exc:
            return self._task_error(task, f"lint could not complete: {exc}")
        tail = "\n".join(
            ((result.stdout or "") + (result.stderr or "")).splitlines()[-40:]
        )
        if result.returncode != 0:
            return self._task_error(
                task,
                f"lint failed (exit {result.returncode}); output tail:\n{tail}",
            )
        return {
            "repo": task["repo"],
            "operation": "lint",
            "status": "ok",
            "data": {"output_tail": tail},
        }

    def _op_test(self, clone: Path, task: dict) -> dict:
        command = self._detect_test_command(clone)
        if command is None:
            return self._task_error(task, "no recognisable test command in this repo")
        try:
            result = self.executor.run(command, cwd=clone, timeout=600)
        except subprocess.SubprocessError as exc:
            return self._task_error(task, f"test run could not complete: {exc}")
        tail = "\n".join(
            ((result.stdout or "") + (result.stderr or "")).splitlines()[-40:]
        )
        if result.returncode != 0:
            return self._task_error(
                task,
                f"tests failed (exit {result.returncode}); output tail:\n{tail}",
            )
        return {
            "repo": task["repo"],
            "operation": "test",
            "status": "ok",
            "data": {"command": command, "output_tail": tail},
        }

    def _op_pr(self, clone: Path, task: dict) -> dict:
        if task.get("confirm") is not True:
            return self._task_error(
                task, "pr refuses to run without task['confirm'] set to true"
            )
        branch = task.get("branch")
        message = task.get("message")
        if not isinstance(branch, str) or not branch:
            return self._task_error(task, "pr needs a 'branch' name")
        if not isinstance(message, str) or not message:
            return self._task_error(task, "pr needs a commit 'message'")
        repo = task["repo"]
        title = task.get("title", message)
        body = task.get("body", message)
        identity = [
            "-c",
            "user.name=swe-agent",
            "-c",
            "user.email=swe-agent@users.noreply.github.com",
        ]
        steps = [
            ("stage changes", ["git", "-C", str(clone), "add", "-A"]),
            ("commit", ["git", "-C", str(clone), *identity, "commit", "-m", message]),
            ("push", ["git", "-C", str(clone), "push", "-u", "origin", branch]),
            (
                "open pull request",
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    f"{ORG}/{repo}",
                    "--head",
                    branch,
                    "--title",
                    title,
                    "--body",
                    body,
                ],
            ),
        ]
        status = self._run(["git", "-C", str(clone), "status", "--porcelain"])
        if status.returncode != 0:
            return self._task_error(task, "working tree could not be read")
        if not status.stdout.strip():
            return self._task_error(
                task, "nothing to commit: the working tree is clean"
            )
        for step_name, cmd in steps:
            try:
                result = self._run(cmd)
            except subprocess.SubprocessError as exc:
                return self._task_error(task, f"{step_name} could not run: {exc}")
            if result.returncode != 0:
                out = (result.stderr or result.stdout or "").strip()
                detail = out.splitlines()[-1] if out else "no error output"
                return self._task_error(task, f"{step_name} failed: {detail}")
        pr_url = (result.stdout or "").strip().splitlines()[-1]
        return {
            "repo": repo,
            "operation": "pr",
            "status": "ok",
            "data": {"branch": branch, "url": pr_url},
        }

    # --- solve loop --------------------------------------------------------

    def _make_driver(self, task: dict) -> Driver:
        provider = provider_name()
        cls = get_provider_class(provider)
        if cls is None:
            raise DriverError(
                f"unknown SWE_AGENT_PROVIDER {provider!r}; expected one of "
                f"{sorted(DRIVER_PROVIDERS)}"
            )
        return cls(
            model=task.get("model"),
            max_output_tokens=task.get("max_output_tokens", 2048),
            cost_cap_usd=task.get("cost_cap_usd", 2.0),
        )

    def _transcript_path(self, repo: str) -> Path:
        stamp = time.strftime("%Y%m%d-T%H%M%SZ", time.gmtime())
        return (
            self.tmp_base
            / "coccinella-labs"
            / "transcripts"
            / repo
            / f"{stamp}-{os.getpid()}.jsonl"
        )

    @staticmethod
    def _write_transcript(path: Path, lines: list) -> str | None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as handle:
                handle.writelines(json.dumps(line) + "\n" for line in lines)
            return str(path)
        except OSError:
            return None

    def _fetch_issue_brief(self, issue, repo: str) -> str | None:
        """gh issue view -> readable text, or None when unusable."""
        if issue is None:
            return None
        raw = str(issue).strip()
        match = re.search(r"/issues/(\d+)$", raw)
        number = match.group(1) if match else (raw if raw.isdigit() else "")
        if not number:
            return None
        result = self._run(
            [
                "gh",
                "issue",
                "view",
                number,
                "-R",
                f"{ORG}/{repo}",
                "--json",
                "title,body,state,url",
            ],
            timeout=60,
        )
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
        except ValueError:
            return None
        body = data.get("body") or ""
        return (
            f"[{data.get('state', 'UNKNOWN')}] {data.get('title', '')}\n"
            f"{data.get('url', '')}\n\n{body[:8000]}"
        )

    def _solve_brief(self, clone: Path, task: dict) -> str:
        instruction = task.get("instruction")
        if isinstance(instruction, str) and instruction.strip():
            source = instruction.strip()
            origin = "provided instruction"
        else:
            source = self._fetch_issue_brief(task.get("issue"), task["repo"])
            origin = "GitHub issue"
            if not source:
                raise ValueError(
                    "solve needs a non-empty 'instruction' or a usable "
                    "'issue' (a number or an issue URL)"
                )
        listing = self._run(["git", "-C", str(clone), "ls-files"], timeout=60)
        files = "\n".join((listing.stdout or "").splitlines()[:100])
        return (
            f"Task ({origin}):\n{source}\n\n"
            f"Org repo: {ORG}/{task['repo']}\n"
            f"Local clone: {clone}\n"
            f"Tracked files (first 100):\n{files}\n\n"
            "Work one JSON command per turn as described in your system "
            "prompt, and submit when the fix is in place and tested."
        )

    @staticmethod
    def _parse_action(text: str) -> tuple[dict | None, str | None]:
        """Turn one model reply into (action, observation)."""
        try:
            action = json.loads(text)
        except ValueError:
            action = None
        if not isinstance(action, dict) or not isinstance(action.get("command"), str):
            return None, (
                "error: reply must be exactly one JSON object with a "
                f"string 'command'; got: {text[:200]!r}"
            )
        if action["command"] not in SOLVE_COMMANDS:
            return None, (
                f"error: unknown command {action['command']!r}; allowed "
                f"commands are: {', '.join(SOLVE_COMMANDS)}"
            )
        return action, None

    def _observe(self, clone: Path, task: dict, action: dict) -> str:
        """Run one allowed model command against the clone; plain text back."""
        if action["command"] == "view":
            result = self._op_read(
                clone,
                {
                    "repo": task["repo"],
                    "operation": "read",
                    "path": action.get("path"),
                    "paths": action.get("paths"),
                },
            )
            text = None
            if result["status"] == "ok":
                if "files" in result["data"]:
                    text = "\n\n".join(
                        f"# {f['path']}\n{f['text']}" for f in result["data"]["files"]
                    )
                else:
                    text = result["data"]["text"]
            if text is not None:
                if len(text) > 8000:
                    text = text[:8000] + "\n... (truncated)"
                return text
            return f"error: {result['error']}"
        if action["command"] == "search":
            result = self._op_search(clone, dict(task, pattern=action.get("pattern")))
            if result["status"] != "ok":
                return f"error: {result['error']}"
            data = result["data"]
            if not data["count"]:
                return "no matches"
            return "\n".join(data["matches"])[:8000]
        if action["command"] == "edit":
            result = self._op_write(
                clone,
                {
                    "repo": task["repo"],
                    "operation": "write",
                    "path": action.get("path"),
                    "content": action.get("content"),
                },
            )
            if result["status"] != "ok":
                return f"error: {result['error']}"
            return f"wrote {result['data']['path']} ({result['data']['bytes']} bytes)"
        if action["command"] == "test":
            result = self._op_test(clone, dict(task))
            if result["status"] != "ok":
                return f"error: {result['error']}"
            return f"tests passed:\n{result['data']['output_tail']}"[:8000]
        if action["command"] == "diff":
            result = self._op_diff(clone, dict(task))
            if result["status"] != "ok":
                return f"error: {result['error']}"
            return result["data"]["text"][:8000]
        if action["command"] == "lint":
            result = self._op_lint(clone, dict(task))
            if result["status"] != "ok":
                return f"error: {result['error']}"
            return f"lint passed:\n{result['data']['output_tail']}"[:8000]
        return f"error: unreachable command {action['command']!r}"

    def _solve(self, task: dict) -> dict:
        """Model-driven loop within one clone; output is a diff, never a push."""

        # CI ceilings: environment-set values clamp task input, so issue
        # text cannot raise a run's turn or cost limits. Values above the
        # hard 50-turn bound remain errors; the ceiling only tightens.
        # Invalid env values fall back to the default ceiling.
        turns_ceiling = _env_int_ceiling("SWE_AGENT_MAX_TURNS", 50)
        cost_ceiling = _env_float_ceiling("SWE_AGENT_COST_CAP_USD", 2.0)
        clamped: dict | None = None
        max_turns = task.get("max_turns", SOLVE_MAX_TURNS)
        if not isinstance(max_turns, int) or not 1 <= max_turns <= 50:
            return self._task_error(task, "max_turns must be an integer from 1 to 50")
        raw_cost = task.get("cost_cap_usd", 2.0)
        if max_turns > turns_ceiling or (
            isinstance(raw_cost, (int, float)) and raw_cost > cost_ceiling
        ):
            clamped = {}
            if max_turns > turns_ceiling:
                clamped["max_turns"] = max_turns
                max_turns = turns_ceiling
            if isinstance(raw_cost, (int, float)) and raw_cost > cost_ceiling:
                clamped["cost_cap_usd"] = raw_cost
                task = dict(task)
                task["cost_cap_usd"] = cost_ceiling
        repo = task["repo"]
        clone = self.get_repo_path(repo)
        if clone is None:
            if not self.clone_repo(repo):
                return self._task_error(task, f"clone failed: {self.last_error}")
            clone = self.get_repo_path(repo)
        try:
            brief = self._solve_brief(clone, task)
        except ValueError as exc:
            return self._task_error(task, str(exc))
        try:
            driver = (
                self.driver_factory()
                if self.driver_factory
                else self._make_driver(task)
            )
        except DriverError as exc:
            return self._task_error(task, f"driver selection failed: {exc}")
        transcript_path = self._transcript_path(repo)
        lines = [
            {
                "run": {
                    "repo": f"{ORG}/{repo}",
                    "clone": str(clone),
                    "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "instruction": brief[:4000],
                    "max_turns": max_turns,
                    **({"clamped": clamped} if clamped else {}),
                }
            }
        ]
        system = SOLVE_SYSTEM.format(org=ORG, repo=repo)
        messages = [{"role": "user", "content": brief}]
        stop_reason = f"turn cap reached ({max_turns})"
        for turn in range(1, max_turns + 1):
            try:
                reply = driver.chat(system, messages)
            except CapReached as exc:
                stop_reason = f"stopped: {exc}"
                break
            except DriverError as exc:
                stop_reason = f"stopped: model call failed: {exc}"
                break
            action, observation = self._parse_action(reply.text)
            if action and action["command"] == "submit":
                lines.append(
                    {
                        "turn": turn,
                        "action": action,
                        "observation": "submitted",
                        "usage": {
                            "input_tokens": reply.input_tokens,
                            "output_tokens": reply.output_tokens,
                            "cost_estimate": reply.cost_estimate,
                        },
                    }
                )
                if self.on_event:
                    self.on_event(turn, "submit", "submitted")
                files = self._run(
                    ["git", "-C", str(clone), "diff", "HEAD", "--name-only"],
                    timeout=60,
                )
                diff = self._run(["git", "-C", str(clone), "diff", "HEAD"], timeout=60)
                transcript = self._write_transcript(transcript_path, lines)
                data = {
                    "turns": turn,
                    "files_changed": (files.stdout or "").split(),
                    "diff": (diff.stdout or "")[:20000],
                    "transcript": transcript,
                    "usage": driver.usage_summary(),
                }
                return {
                    "repo": repo,
                    "operation": "solve",
                    "status": "ok",
                    "data": data,
                }
            if action:
                observation = self._observe(clone, task, action)
            lines.append(
                {
                    "turn": turn,
                    "action": action if action else reply.text[:2000],
                    "observation": observation,
                    "usage": {
                        "input_tokens": reply.input_tokens,
                        "output_tokens": reply.output_tokens,
                        "cost_estimate": reply.cost_estimate,
                    },
                }
            )
            if self.on_event:
                summary = json.dumps(action) if action else reply.text
                self.on_event(turn, summary[:200], observation[:300])
            messages = messages + [
                {"role": "assistant", "content": reply.text},
                {"role": "user", "content": observation or "no-op"},
            ]
        self._write_transcript(transcript_path, lines)
        return self._task_error(task, f"{stop_reason}; transcript: {transcript_path}")

    def get_capabilities(self) -> dict:
        """Get agent capabilities including skills."""
        return {
            "name": self.name,
            "repos": self.repos,
            "cloned_repos": self.list_cloned_repos(),
            "skills": self.skills,
            "tmp_base": str(self.tmp_base),
            "operations": list(OPERATIONS),
        }


if __name__ == "__main__":
    agent = SWEAgent("coccinella-labs-agent")
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "list":
            print("Coccinella Labs Repositories:")
            for repo in agent.repos:
                print(f"  - {repo}")
        elif cmd == "cloned":
            print("Cloned Repos:")
            for repo in agent.list_cloned_repos():
                print(f"  - {repo}")
        elif cmd == "clone" and len(sys.argv) > 2:
            repo = sys.argv[2]
            print(f"Cloning {repo}...")
            result = agent.clone_repo(repo)
            print(f"Success: {result}")
            if not result and agent.last_error:
                print(f"Error: {agent.last_error}")
        elif cmd == "clone-all":
            print("Cloning all repos...")
            cloned = agent.clone_all()
            print(f"Cloned: {cloned}")
            failed = [repo for repo in agent.repos if repo not in cloned]
            if failed:
                print(f"Failed: {failed}")
        elif cmd == "task" and len(sys.argv) > 2:
            try:
                task = json.loads(sys.argv[2])
            except ValueError:
                task = sys.argv[2]
            print(json.dumps(agent.execute_task(task), indent=2))
        elif cmd == "capabilities":
            info = agent.get_capabilities()
            print(f"Agent: {info['name']}")
            print(f"Total repos: {len(info['repos'])}")
            print(f"Cloned repos: {info['cloned_repos']}")
            print(f"Skills loaded: {list(info['skills'].keys())}")
        else:
            print("Usage: python agent.py <command>")
            print("Commands: list, cloned, clone <repo>, clone-all, capabilities,")
            print("           task '<json>'")
    else:
        info = agent.get_capabilities()
        print(f"Agent: {info['name']}")
        print(f"Total repos: {len(info['repos'])}")
        print(f"Cloned repos: {info['cloned_repos']}")
        print(f"Skills loaded: {list(info['skills'].keys())}")
