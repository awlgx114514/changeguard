import unittest

from maths import total


class TotalTests(unittest.TestCase):
    def test_sums_values(self):
        self.assertEqual(total([2, 3, 5]), 10)


if __name__ == "__main__":
    unittest.main()
