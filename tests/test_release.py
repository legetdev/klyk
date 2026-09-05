"""Portable release-name checks for private plans, secrets, and evidence."""

import unittest

from release_check import check_names


class ReleaseNameTests(unittest.TestCase):
    """Ensure publication checks reject private or generated path entries."""

    def test_private_and_generated_entries_are_rejected(self):
        """Reject private plans, environment files, and verification artifacts anywhere in a path."""
        rejected = (
            "ROADMAP.md",
            "archive/POLARSTAR.md",
            ".env",
            ".verification/live.json",
        )
        for name in rejected:
            with self.subTest(name=name), self.assertRaises(ValueError):
                check_names([name])

    def test_public_source_entries_are_allowed(self):
        """Allow ordinary source, documentation, and test entries used in a release."""
        check_names(
            [
                "README.md",
                "SECURITY.md",
                "klyk/clients.py",
                "tests/test_release.py",
                "dist/klyk-0.5.0-py3-none-any.whl",
            ]
        )


if __name__ == "__main__":
    unittest.main()
