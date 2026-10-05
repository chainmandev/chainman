"""The source entry uses the same native retention manager as consumers."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update_cache
import git_runtime
import updates


class UpdateCacheEntry(unittest.TestCase):
    def test_export_rejects_existing_destination_without_replaying_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "export"
            target.mkdir()
            result = target / "result.json"
            previous = b"previous invocation result\n"
            result.write_bytes(previous)
            with (
                patch.object(update_cache.hook_worker, "export_binary") as build,
                contextlib.redirect_stdout(io.StringIO()) as output,
                self.assertRaisesRegex(FileExistsError, "already exists"),
            ):
                update_cache.run(
                    root, "deps-update", ["--export-candidate", str(target)]
                )
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(result.read_bytes(), previous)
            build.assert_not_called()

    def test_export_reports_only_the_current_operations_result(self):
        for current in (True, False):
            with (
                self.subTest(current=current),
                tempfile.TemporaryDirectory() as directory,
            ):
                # Successful export fixtures use canonical paths; aliases are
                # rejected by the independent export containment tests.
                root = Path(directory).resolve()
                target = root / "export"
                record = None

                def supervise(argv, *, env, **kwargs):
                    nonlocal record
                    # Model another invocation winning the exclusive mkdir after
                    # this invocation checked that the destination was absent.
                    target.mkdir()
                    record = {
                        "schema": 1,
                        "kind": "chainman.update-result",
                        "operation": env["CHAINMAN_SOURCE_EXPORT_OPERATION"]
                        if current
                        else str(uuid.uuid4()),
                        "outcome": "failure_before_acceptance",
                        "stage": "resolution",
                        "exit_code": 23,
                        "snapshot_sha256": None,
                    }
                    (target / "result.json").write_text(json.dumps(record))
                    return subprocess.CompletedProcess(argv, 23)

                with (
                    patch.object(update_cache.hook_worker, "export_binary"),
                    patch.object(
                        updates, "repository", return_value=("main", "a" * 40)
                    ),
                    patch.object(git_runtime, "store", return_value=root / "runtime"),
                    patch.object(
                        update_cache.tc,
                        "nix_temporary_directory",
                        return_value=contextlib.nullcontext(directory),
                    ),
                    patch.object(update_cache.tc, "managed_run", side_effect=supervise),
                    contextlib.redirect_stdout(io.StringIO()) as output,
                    self.assertRaises(SystemExit if current else ValueError) as stopped,
                ):
                    update_cache.run(
                        root, "deps-update", ["--export-candidate", str(target)]
                    )
                if current:
                    self.assertEqual(stopped.exception.code, 23)
                    self.assertEqual(json.loads(output.getvalue()), record)
                else:
                    self.assertIn("another operation", str(stopped.exception))
                    self.assertEqual(output.getvalue(), "")
                self.assertEqual(
                    json.loads((target / "result.json").read_text()), record
                )

    def test_source_and_maintenance_share_native_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.dict(os.environ, XDG_CACHE_HOME=str(root / "cache")),
                patch.object(update_cache.hook_worker, "export_binary") as export,
                patch.object(update_cache.tc, "managed_run") as run,
            ):
                run.return_value.returncode = 0
                update_cache.run(root, "deps-update", ["commit=off"])
                argv = run.call_args.args[0]
                self.assertEqual(
                    argv[1:6],
                    [
                        "update-cache",
                        "run",
                        str(root / "cache/chainman/updates"),
                        "-",
                        "deps-update",
                    ],
                )
                self.assertEqual(argv[-2:], ["deps-update", "commit=off"])
                self.assertEqual(
                    export.call_args.args[-2:], ("task", "chainman-control")
                )
                update_cache.run(root, "prune", ["--all"])
                self.assertEqual(
                    run.call_args.args[0][1:],
                    [
                        "update-cache",
                        "prune",
                        str(root / "cache/chainman/updates"),
                        "--all",
                    ],
                )
                run.return_value.returncode = 130
                with self.assertRaises(SystemExit) as stopped:
                    update_cache.run(root, "deps-update", [])
                self.assertEqual(stopped.exception.code, 130)


if __name__ == "__main__":
    unittest.main()
