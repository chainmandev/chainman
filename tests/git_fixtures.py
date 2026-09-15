"""Real Git source fixtures with no public-network dependency or archive format."""

from pathlib import Path
import subprocess
import tempfile


def pin_tree(source: Path, cache_home: Path) -> str:
    cache_home.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="git-fixture-", dir=cache_home
    ) as temporary:
        repository = Path(temporary) / "repository.git"
        subprocess.run(["git", "init", "--bare", "-q", str(repository)], check=True)
        command = ["git", "--git-dir=" + str(repository), "--work-tree=" + str(source)]
        subprocess.run([*command, "add", "-A"], check=True)
        subprocess.run(
            [
                *command,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-qm",
                "Runtime fixture",
            ],
            check=True,
        )
        revision = subprocess.check_output(
            [*command, "rev-parse", "HEAD"], text=True
        ).strip()
        cache = (
            cache_home
            / "chainman/git/github.com-chainmandev-chainman"
            / (revision + ".git")
        )
        if not cache.exists():
            cache.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--bare", "-q", str(repository), str(cache)],
                check=True,
            )
    return revision
