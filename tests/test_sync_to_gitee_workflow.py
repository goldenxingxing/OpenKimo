import re
import unittest
from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "sync-to-gitee.yml"
)


class SyncToGiteeWorkflowTests(unittest.TestCase):
    def test_workflow_has_git_and_release_triggers(self):
        text = WORKFLOW.read_text()

        self.assertIn("workflow_run:", text)
        self.assertIn("workflows: [Release]", text)
        self.assertIn("types: [completed]", text)
        self.assertIn("tags:", text)
        self.assertIn("- 'v*'", text)
        self.assertIn("delete:", text)

    def test_release_job_waits_for_successful_version_run(self):
        text = WORKFLOW.read_text()

        self.assertIn(
            "github.event.workflow_run.conclusion == 'success'", text
        )
        self.assertIn(
            "startsWith(github.event.workflow_run.head_branch, 'v')", text
        )
        self.assertIn("GITEE_ACCESS_TOKEN", text)
        self.assertIn("sync_gitee_release.py", text)

    def test_jobs_have_separate_concurrency_groups(self):
        text = WORKFLOW.read_text()

        groups = re.findall(r"^\s+group:\s*(.+)$", text, re.MULTILINE)
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(set(groups)), 2)

    def test_workflow_references_secrets_without_literal_values(self):
        text = WORKFLOW.read_text()

        self.assertIn("${{ secrets.GITEE_RSA_PRIVATE_KEY }}", text)
        self.assertIn("${{ secrets.GITEE_ACCESS_TOKEN }}", text)
        self.assertNotRegex(text, r"\b(?:ghp_|gitee_)[A-Za-z0-9_-]{20,}")


if __name__ == "__main__":
    unittest.main()
