"""The source entry uses the same native retention manager as consumers."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update_cache


class UpdateCacheEntry(unittest.TestCase):
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
