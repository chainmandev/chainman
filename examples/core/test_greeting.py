import unittest
from greeting import greeting


class GreetingTests(unittest.TestCase):
    def test_trimmed_name(self):
        self.assertEqual(greeting("  Ada  "), "Hello, Ada!")

    def test_invalid_names(self):
        for name in ("", " ", "two\nlines", "two\rlines"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                greeting(name)


if __name__ == "__main__":
    unittest.main()
