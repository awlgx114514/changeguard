import unittest

from api import celsius


class CelsiusApiTests(unittest.TestCase):
    def test_converts_freezing_point(self):
        self.assertEqual(celsius(32), 0)


if __name__ == "__main__":
    unittest.main()
