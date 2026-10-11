import unittest

from names import normalize_name


class NormalizeNameTests(unittest.TestCase):
    def test_trims_before_title_casing(self):
        self.assertEqual(normalize_name("  ada lovelace  "), "Ada Lovelace")


if __name__ == "__main__":
    unittest.main()
