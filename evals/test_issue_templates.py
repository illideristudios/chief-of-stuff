import re
import unittest
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / ".github" / "ISSUE_TEMPLATE"


class IssueTemplatesTest(unittest.TestCase):
    def test_no_template_asks_for_file_ownership(self):
        files = sorted(TEMPLATES.glob("*.yml"))
        self.assertTrue(files)
        for path in files:
            with self.subTest(template=path.name):
                self.assertIsNone(re.search(r"(?i)file[ _]ownership", path.read_text()))


if __name__ == "__main__":
    unittest.main()
