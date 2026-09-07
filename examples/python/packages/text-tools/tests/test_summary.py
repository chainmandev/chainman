import unittest

from text_tools import summarize


class SummaryTests(unittest.TestCase):
    def test_omits_blank_labels(self) -> None:
        self.assertEqual(summarize([" first ", "", " second"]), "first, second")


if __name__ == "__main__":
    unittest.main()
