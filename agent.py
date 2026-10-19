"""SWE Agent - Software Engineering Agent."""

import json
import subprocess
import sys
from pathlib import Path

ORG = "coccinella-labs"
CONFIG_URL = f"https://raw.githubusercontent.com/{ORG}/config/main/repos.json"

TMP_BASE = "/tmp"

# Used only when the config repository cannot be reached: the repositories the
# agent itself depends on, so a retry against config stays possible.
FALLBACK_REPOS = [".github", "config", "skills", "swe-agent"]

OPERATIONS = ("read", "write", "test", "pr")


class SWEAgent:
    """Base class for software engineering agents."""

    def __init__(
        self,
        name: str,
        capabilities: list | None = None,
        tmp_base: str | None = None,
    ):
        self.name = name
        self.capabilities = capabilities or []
        self.repos = []
        self.skills = {}
        self.tmp_base = Path(tmp_base) if tmp_base else Path(TMP_BASE)
        self.last_error: str | None = None
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
        'test', 'pr') plus operation-specific fields:

        - read:  'path' exists in the clone; returns file text.
        - write: 'path' and string 'content'; writes the file, uncommitted.
        - test:  runs the repo's own tests; reports the command and outcome.
        - pr:    needs 'branch' and 'message', and confirm=True; commits the
                 working tree on a new branch, pushes it, and opens a PR.

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
        rel = task["path"]
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

    def _op_test(self, clone: Path, task: dict) -> dict:
        command = self._detect_test_command(clone)
        if command is None:
            return self._task_error(task, "no recognisable test command in this repo")
        try:
            result = self._run(command, cwd=clone, timeout=600)
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
