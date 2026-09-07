"""Real multi-project resolution against disposable, offline Maven fixtures."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile


@unittest.skipUnless(
    os.environ.get("CHAINMAN_TEST_GRADLE") == "1", "requires the pinned Gradle profile"
)
class GradleResolutionTests(unittest.TestCase):
    def test_child_configuration_and_transitive_locks_and_resolution_failure(self):
        with tempfile.TemporaryDirectory(prefix="gradle project ") as temporary:
            root = Path(temporary)
            repository = root / "local repository"
            for name in ("direct", "transitive"):
                directory = repository / "org/example" / name / "1.0.0"
                directory.mkdir(parents=True)
                dependency = (
                    "<dependencies><dependency><groupId>org.example</groupId><artifactId>transitive</artifactId><version>1.0.0</version></dependency></dependencies>"
                    if name == "direct"
                    else ""
                )
                (directory / f"{name}-1.0.0.pom").write_text(
                    f"<project><modelVersion>4.0.0</modelVersion><groupId>org.example</groupId><artifactId>{name}</artifactId><version>1.0.0</version>{dependency}</project>"
                )
                with zipfile.ZipFile(directory / f"{name}-1.0.0.jar", "w") as archive:
                    archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
            (root / "settings.gradle").write_text(
                "rootProject.name = 'fixture'\ninclude 'app'\n"
            )
            (root / "build.gradle").write_text("")
            (root / "app").mkdir()
            path = str(repository).replace("\\", "\\\\").replace("'", "\\'")
            build = root / "app/build.gradle"
            build.write_text(
                "plugins { id 'java' }\nrepositories { maven { url = uri('"
                + path
                + "') } }\ndependencies { implementation 'org.example:direct:1.0.0' }\n"
            )
            env = dict(os.environ, GRADLE_USER_HOME=str(root / "gradle-home"))
            command = [
                shutil.which("gradle"),
                "--offline",
                "--no-daemon",
                "--max-workers=2",
                "--console=plain",
                "--init-script",
                str(
                    Path(__file__).resolve().parents[1]
                    / "scripts/gradle-resolve.init.gradle"
                ),
                "chainmanResolveAll",
                "--write-locks",
                "--write-verification-metadata",
                "sha256",
            ]
            result = subprocess.run(
                command,
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            locked = (root / "app/gradle.lockfile").read_text()
            self.assertIn("org.example:direct:1.0.0=", locked)
            self.assertIn("org.example:transitive:1.0.0=", locked)
            build.write_text(build.read_text().replace("direct:1.0.0", "direct:2.0.0"))
            rejected = subprocess.run(
                command,
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=120,
            )
            self.assertNotEqual(rejected.returncode, 0, rejected.stdout)
            self.assertIn("org.example:direct:2.0.0", rejected.stdout)


if __name__ == "__main__":
    unittest.main()
