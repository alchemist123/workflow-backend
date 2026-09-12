"""Running a workflow.

There is one execution path: `package_runner` renders the workflow's package and
drives it over the package's own A2A surface. The platform has no engine of its
own, so a test run and a deployment execute identical code.

This module previously held a second implementation (`engine.py`, `handlers.py`,
`context.py`) that traversed the IR directly. It has been removed: it had already
drifted from the generated packages, and keeping two traversals in step by hand
was the bug source this migration exists to eliminate.
"""

from .package_runner import RunResult, ensure_package, run_workflow_package

__all__ = ["run_workflow_package", "ensure_package", "RunResult"]
