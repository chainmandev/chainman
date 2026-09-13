"""Dependency omissions and exception expiry cannot appear as successful audits."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import date
from unittest.mock import patch
from contextlib import redirect_stdout
import io
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dependency_audit
import dependency_reports
import updates


class ReportTests(unittest.TestCase):
    def test_native_audit_tools_remain_rooted_through_scanner_execution(self):
        cfg = {
            "updates": {
                "adapters": {"go": {"adapter": "go", "profile": "host"}},
                "steps": [{"resolve": "go"}],
            }
        }
        roots = []

        def build(argv, **kwargs):
            root = Path(argv[argv.index("--out-link") + 1])
            root.symlink_to("/nix/store/fixture-audit")
            roots.append(root)
            return subprocess.CompletedProcess(argv, 0, "/nix/store/fixture-audit\n")

        def execute(root, profile, argv, **kwargs):
            self.assertEqual(
                argv, ["/nix/store/fixture-audit/bin/govulncheck", "./..."]
            )
            self.assertTrue(roots[0].is_symlink())
            return subprocess.CompletedProcess(argv, 0, "", "")

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(dependency_audit.tc, "config", return_value=cfg),
            patch.object(dependency_audit.tc, "environment", return_value={}),
            patch.object(dependency_audit.tc, "managed_run", side_effect=build),
            patch.object(dependency_audit.chainman, "execute", side_effect=execute),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(dependency_audit.run(Path(temporary).resolve(), []), 0)
        self.assertEqual(len(roots), 1)
        self.assertFalse(roots[0].parent.exists())

    def test_native_exception_declarations_fail_before_scanner_execution(self):
        cfg = {
            "updates": {
                "adapters": {"go": {"adapter": "go", "profile": "host"}},
                "steps": [{"resolve": "go"}],
            },
            "audits": {
                "exceptions": {
                    "go": [
                        {
                            "id": "GO-123",
                            "package": "demo",
                            "reason": "old",
                            "review_after": "2000-01-01",
                        }
                    ]
                }
            },
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(dependency_audit.tc, "config", return_value=cfg),
            patch.object(dependency_audit.chainman, "execute") as execute,
        ):
            with self.assertRaisesRegex(ValueError, "JavaScript adapter"):
                dependency_audit.run(Path(temporary).resolve(), [])
            execute.assert_not_called()

    def test_selected_audit_executes_only_its_ecosystem_and_propagates_failure(self):
        cfg = {
            "updates": {
                "adapters": {
                    "js": {"adapter": "javascript", "profile": "host"},
                    "pub": {"adapter": "flutter", "profile": "host"},
                },
                "steps": [{"resolve": "js"}, {"resolve": "pub"}],
            }
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(dependency_audit.tc, "config", return_value=cfg),
            patch.object(dependency_audit.tc, "environment", return_value={}),
            patch.object(
                dependency_audit.chainman,
                "execute",
                return_value=subprocess.CompletedProcess(
                    [], 1, '{"advisories":{"123":{"module_name":"demo"}}}', ""
                ),
            ) as execute,
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                status = dependency_audit.run(Path(temporary).resolve(), ["targets=js"])
            self.assertEqual(status, 1)
            self.assertEqual(execute.call_count, 1)
            report = json.loads(output.getvalue())
            self.assertEqual([row["adapter"] for row in report["lanes"]], ["js"])
            self.assertEqual(report["lanes"][0]["findings"], {"123": "demo"})

    def test_coverage_distinguishes_managed_excluded_and_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "fixture").mkdir()
            (root / "fixture/package.json").write_text("{}")
            (root / "go.mod").write_text("module example.invalid/demo\ngo 1.23\n")
            (root / "Cargo.toml").write_text('[package]\nname="demo"\nversion="0.1.0"')
            (root / "chainman.toml").write_text("""schema=3
[updates.adapters.go]
adapter="go"
directories=["."]
[dependencies]
exclusions=[{pattern="fixture/**",reason="Literal manifest fixture"}]
""")
            updates.git(root, "init")
            updates.git(root, "add", ".")
            result = dependency_reports.coverage(root)
            rows = {row["path"]: row["status"] for row in result["inputs"]}
            self.assertEqual(
                rows,
                {
                    "Cargo.toml": "unmanaged",
                    "fixture/package.json": "excluded",
                    "go.mod": "managed",
                },
            )
            self.assertFalse(result["complete"])

    def test_audit_report_requires_a_complete_recognized_response(self):
        for document in ({}, {"error": "registry unavailable", "advisories": {}}):
            with self.assertRaises(ValueError):
                dependency_audit.javascript(document)
        self.assertEqual(
            dependency_audit.javascript(
                {"advisories": {"123": {"module_name": "demo"}}}
            ),
            {"123": "demo"},
        )

    def test_exceptions_match_identity_and_expire(self):
        entry = {
            "id": "123",
            "package": "demo",
            "reason": "Reviewed unreachable path",
            "review_after": "2027-01-01",
        }
        self.assertEqual(
            dependency_audit.evaluate({"123": "demo"}, [entry], date(2026, 9, 12)), {}
        )
        for findings, today in (
            ({}, date(2026, 9, 12)),
            ({"123": "other"}, date(2026, 9, 12)),
            ({"123": "demo"}, date(2027, 1, 1)),
        ):
            with self.assertRaises(ValueError):
                dependency_audit.evaluate(findings, [entry], today)


if __name__ == "__main__":
    unittest.main()
