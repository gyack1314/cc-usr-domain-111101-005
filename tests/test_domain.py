import unittest
from tests.domain_checks import check

class DomainTests(unittest.TestCase):
    def test_existing_primitives(self):
        check(self)
