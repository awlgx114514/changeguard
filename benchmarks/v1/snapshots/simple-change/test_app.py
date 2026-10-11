import unittest

from app import greet


class GreetTests(unittest.TestCase):
    def test_greet_uses_hello(self):
        self.assertEqual(greet("Ada"), "Hello, Ada!")


if __name__ == "__main__":
    unittest.main()
