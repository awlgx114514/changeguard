import unittest

from ports import parse_port


class ParsePortTests(unittest.TestCase):
    def test_accepts_the_rule_boundary_values(self):
        self.assertEqual(parse_port("1"), 1)
        self.assertEqual(parse_port("65535"), 65535)

    def test_rejects_values_outside_the_rule(self):
        with self.assertRaises(ValueError):
            parse_port("0")
        with self.assertRaises(ValueError):
            parse_port("65536")


if __name__ == "__main__":
    unittest.main()
