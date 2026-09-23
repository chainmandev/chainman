"""Consent transport fails closed if its foreground owner disappears."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import setup_channel


class SetupChannelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="consent channel ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "incoming").mkdir()
        (self.root / "outgoing").mkdir()
        (self.root / "outgoing/alive").write_text("1")

    def test_response_is_scoped_to_request_and_private_request_is_removed(self):
        for answer in ("yes", "no", "garbage"):

            def respond(_):
                (request,) = (self.root / "incoming").iterdir()
                self.assertEqual((request / "question").read_text(), "Install fixture?")
                (self.root / "outgoing" / request.name).write_text(answer + "\n")

            with patch("setup_channel.time.sleep", side_effect=respond):
                self.assertEqual(
                    setup_channel.request(self.root, "Install fixture?"),
                    answer == "yes",
                )
            self.assertEqual(list((self.root / "incoming").iterdir()), [])

    def test_owner_heartbeat_stops_without_answer(self):
        with (
            patch("setup_channel.time.monotonic", side_effect=[0, 0, 11]),
            patch("setup_channel.time.sleep"),
        ):
            with self.assertRaisesRegex(OSError, "owner stopped"):
                setup_channel.request(self.root, "Install fixture?")
        self.assertEqual(list((self.root / "incoming").iterdir()), [])

    def test_missing_owner_refuses_and_cleans_request(self):
        (self.root / "outgoing/alive").unlink()
        with self.assertRaises(OSError):
            setup_channel.request(self.root, "Install fixture?")
        self.assertEqual(list((self.root / "incoming").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
