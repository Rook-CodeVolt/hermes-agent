"""Fork CI runner compatibility contracts."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
LARGER_RUNNER = re.compile(
    r"(?:ubuntu|windows|macos)-latest-[A-Za-z0-9-]*core"
)


def test_required_pull_request_lanes_use_standard_hosted_runners() -> None:
    """The contribution fork must not wait for unavailable larger runners."""
    expected = {
        ".github/workflows/tests.yml": "runs-on: ubuntu-latest",
        ".github/workflows/tests-os.yml": "runner: windows-latest",
        ".github/workflows/js-tests.yml": "runs-on: ubuntu-latest",
        ".github/workflows/rust-tests.yml": "runs-on: ubuntu-latest",
        ".github/workflows/nix.yml": "runs-on: ubuntu-latest",
    }

    for relative_path, runner_line in expected.items():
        workflow = (ROOT / relative_path).read_text(encoding="utf-8")
        workflow_lines = {line.strip() for line in workflow.splitlines()}
        unavailable_runner = LARGER_RUNNER.search(workflow)
        assert unavailable_runner is None, (
            f"{relative_path} still requests unavailable runner "
            f"{unavailable_runner.group(0)}"
        )
        assert runner_line in workflow_lines, (
            f"{relative_path} must use the standard hosted runner available "
            "to this contribution fork"
        )

    python_workflow = (ROOT / ".github/workflows/tests.yml").read_text(
        encoding="utf-8"
    )
    assert "HERMES_TEST_WORKERS: 96" not in python_workflow
    assert 'HERMES_TEST_WORKERS="$(python -c' in python_workflow
    assert "process_cpu_count" in python_workflow
    assert "export HERMES_TEST_WORKERS" in python_workflow
