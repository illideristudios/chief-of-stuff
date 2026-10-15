import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import guard_commands  # noqa: E402


class WildcardProtectedBranchTest(unittest.TestCase):
    def denied(self, command, protected=("main", "release/*")):
        return guard_commands.denial_reason(command, protected) is not None

    def test_wildcard_protected_branch_is_denied(self):
        for command in ("git push origin HEAD:release/1.0", "git push origin +HEAD:refs/heads/release/1.0",
                        "git push origin HEAD:release/1.0/hotfix"):
            with self.subTest(command=command):
                self.assertTrue(self.denied(command))

    def test_unprotected_and_exact_branches_still_decided_exactly(self):
        self.assertFalse(self.denied("git push origin HEAD:work/issue-1"))
        self.assertFalse(self.denied("git push origin HEAD:releases/1.0"))
        self.assertTrue(self.denied("git push origin HEAD:main"))

    def test_gh_pr_create_head_matches_wildcards(self):
        self.assertTrue(self.denied("gh pr create --head release/1.0 --title t --body b"))


if __name__ == "__main__":
    unittest.main()
