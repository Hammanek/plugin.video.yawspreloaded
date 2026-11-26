import ast
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
CHECKOUT = "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"
SETUP_PYTHON = "actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c"


def top_level_section(text, name):
    lines = text.splitlines()
    marker = name + ":"
    start = next((index for index, line in enumerate(lines) if line == marker), None)
    if start is None:
        raise AssertionError("missing top-level YAML section: " + name)
    section = []
    for line in lines[start + 1 :]:
        if line and not line[0].isspace():
            break
        section.append(line)
    return section


def nested_keys(section):
    return {
        match.group(1)
        for line in section
        for match in [re.match(r"^\s*([A-Za-z_][A-Za-z0-9_-]*):\s*$", line)]
        if match
    }


class OfflineWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.lines = self.text.splitlines()

    def test_permissions_are_read_only_and_only_push_and_pull_request_trigger(self):
        permissions = top_level_section(self.text, "permissions")
        self.assertEqual(["contents: read"], [line.strip() for line in permissions if line.strip()])
        triggers = top_level_section(self.text, "on")
        self.assertEqual({"push", "pull_request"}, nested_keys(triggers))
        self.assertNotIn("pull_request_target", self.text)
        self.assertNotIn("workflow_run", self.text)

    def test_python_matrix_contains_strings_and_every_action_is_pinned_to_verified_sha(self):
        matrix = top_level_section(self.text, "jobs")
        matrix_values = [
            match.group(1)
            for line in matrix
            for match in [re.match(r"^\s*python-version:\s*(\[.*\])\s*$", line)]
            if match
        ]
        self.assertEqual(1, len(matrix_values))
        versions = ast.literal_eval(matrix_values[0])
        self.assertEqual(["3.8", "3.12"], versions)
        self.assertTrue(all(isinstance(version, str) for version in versions))

        uses = [
            match.group(1).strip()
            for line in self.lines
            for match in [re.match(r"^\s*uses:\s*(\S+)\s*$", line)]
            if match
        ]
        self.assertEqual({CHECKOUT, SETUP_PYTHON}, set(uses))
        self.assertEqual(2, len(uses))
        for use in uses:
            with self.subTest(action=use):
                reference = use.rsplit("@", 1)[1]
                self.assertRegex(reference, r"^[0-9a-f]{40}$")

    def test_matrix_keeps_python_38_on_a_supported_ubuntu_runner(self):
        matrix = top_level_section(self.text, "jobs")
        runner = [line.strip() for line in matrix if line.strip().startswith("runs-on:")]
        self.assertEqual(["runs-on: ubuntu-22.04"], runner)

    def test_checkout_does_not_persist_credentials(self):
        checkout_index = next(index for index, line in enumerate(self.lines) if CHECKOUT in line)
        next_step = next(
            (index for index in range(checkout_index + 1, len(self.lines))
             if re.match(r"^\s*- name:", self.lines[index])),
            len(self.lines),
        )
        checkout_step = self.lines[checkout_index + 1 : next_step]
        self.assertIn("persist-credentials: false", [line.strip() for line in checkout_step])

    def test_workflow_runs_only_pinned_test_install_discovery_and_verifier(self):
        commands = [
            match.group(1).strip()
            for line in self.lines
            for match in [re.match(r"^\s*run:\s*(.+?)\s*$", line)]
            if match
        ]
        self.assertEqual(
            [
                "python -m pip install requests==2.32.3 Unidecode==1.1.1",
                "python -B -m unittest discover -s tests -v",
                "python -B scripts/verify_release.py",
            ],
            commands,
        )

    def test_no_secret_material_upload_release_or_git_publication_path_exists(self):
        lowered = self.text.lower()
        for forbidden in (
            "secrets.",
            "github_token",
            "github-token",
            "token:",
            "contents: write",
            "packages: write",
            "id-token: write",
            "actions/upload-artifact",
            "action-gh-release",
            "gh release",
            "gh auth",
            "git push",
            "git tag",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)


if __name__ == "__main__":
    unittest.main()
