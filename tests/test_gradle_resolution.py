"""Real multi-project resolution against disposable, offline Maven fixtures."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lock_adapters
import toolchain


@unittest.skipUnless(
    os.environ.get("CHAINMAN_TEST_GRADLE") == "1", "requires the pinned Gradle profile"
)
class GradleResolutionTests(unittest.TestCase):
    def test_managed_defaults_end_the_build_jvm_after_completion(self):
        with tempfile.TemporaryDirectory(
            prefix="chainman gradle lifetime "
        ) as directory:
            root = Path(directory).resolve()
            (root / "toolchain.toml").write_text('schema=1\nmodules=["core"]\n')
            (root / "settings.gradle").write_text("rootProject.name='lifetime'\n")
            (root / "gradle.properties").write_text("org.gradle.jvmargs=-Xmx256m\n")
            (root / "build.gradle").write_text(
                "tasks.register('probe') { doLast {\n"
                " assert project.property('kotlin.compiler.execution.strategy') == 'in-process'\n"
                " file('build-jvm.pid').text = ProcessHandle.current().pid().toString()\n"
                "} }\n"
            )
            env = toolchain.environment(root)
            env["GRADLE_USER_HOME"] = str(root / "gradle-home")
            result = subprocess.run(
                [
                    shutil.which("gradle"),
                    "--offline",
                    "--console=plain",
                    "--max-workers=2",
                    "probe",
                ],
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            pid = int((root / "build-jvm.pid").read_text())
            for _ in range(100):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                self.fail(
                    f"Gradle build JVM {pid} survived its command: {result.stdout}"
                )

    def test_composite_graph_binds_actual_sources_and_inspection_is_read_only(self):
        with tempfile.TemporaryDirectory(prefix="gradle composite ") as temporary:
            root = Path(temporary).resolve()
            (root / "settings.gradle").write_text(
                "rootProject.name='app'\nincludeBuild('library')\n"
            )
            (root / "build.gradle").write_text(
                "plugins { id 'java' }\ndependencies { implementation 'sample:library:1.0.0' }\n"
            )
            library = root / "library"
            library.mkdir()
            (library / "settings.gradle").write_text("rootProject.name='library'\n")
            (library / "build.gradle").write_text(
                "plugins { id 'java-library' }\ngroup='sample'\nversion='1.0.0'\n"
            )
            reports = root / "reports"
            reports.mkdir()
            env = dict(
                os.environ,
                GRADLE_USER_HOME=str(root / "gradle-home"),
                CHAINMAN_GRADLE_REPORT_DIR=str(reports),
            )
            base = [
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
            ]

            def run(arguments):
                for previous in reports.glob("*.json"):
                    previous.unlink()
                result = subprocess.run(
                    base + arguments,
                    cwd=root,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=120,
                )
                self.assertEqual(result.returncode, 0, result.stdout)
                return [
                    json.loads(p.read_text()) for p in sorted(reports.glob("*.json"))
                ]

            report = run(
                [
                    "chainmanResolveAll",
                    "--write-locks",
                    "--write-verification-metadata",
                    "sha256",
                ]
            )
            spec = {"local_projects": {"sample:library": "library"}}
            graph = lock_adapters.validate_gradle_projects(root, spec, report)
            self.assertTrue(
                any(edge["coordinate"] == "sample:library" for edge in graph["edges"])
            )
            locks = {
                p: p.read_bytes()
                for pattern in ("*.lockfile", "verification-metadata.xml")
                for p in root.rglob(pattern)
            }
            self.assertTrue(locks)
            inspected = run(["--dependency-verification", "strict", "chainmanInspect"])
            self.assertEqual(
                lock_adapters.validate_gradle_projects(root, spec, inspected), graph
            )
            self.assertEqual({p: p.read_bytes() for p in locks}, locks)
            dummy = root / "dummy"
            dummy.mkdir()
            (dummy / "build.gradle").write_text("")
            with self.assertRaisesRegex(ValueError, "binding"):
                lock_adapters.validate_gradle_projects(
                    root, {"local_projects": {"sample:library": "dummy"}}, inspected
                )
            with self.assertRaisesRegex(ValueError, "binding"):
                lock_adapters.validate_gradle_projects(root, {}, inspected)
            escaped = json.loads(json.dumps(inspected))
            escaped[0]["projects"][0]["directory"] = str(root.parent / "outside")
            with self.assertRaisesRegex(ValueError, "escapes"):
                lock_adapters.validate_gradle_projects(root, spec, escaped)
            (root / "settings.gradle").write_text(
                "rootProject.name='app'\nincludeBuild('dummy') { dependencySubstitution { substitute module('sample:library') using project(':') } }\n"
            )
            (dummy / "build.gradle").write_text((library / "build.gradle").read_text())
            (dummy / "settings.gradle").write_text("rootProject.name='dummy'\n")
            # A reconciliation hook can redirect substitution after resolution.
            # Reinspect the actual graph instead of trusting the earlier report.
            for lock in library.glob("*.lockfile"):
                shutil.copyfile(lock, dummy / lock.name)
            redirected = run(["--dependency-verification", "strict", "chainmanInspect"])
            with self.assertRaisesRegex(ValueError, "binding"):
                lock_adapters.validate_gradle_projects(root, spec, redirected)

    def test_child_configuration_and_transitive_locks_and_resolution_failure(self):
        with tempfile.TemporaryDirectory(prefix="gradle project ") as temporary:
            root = Path(temporary).resolve()
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
