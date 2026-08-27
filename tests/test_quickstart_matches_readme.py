"""examples/quickstart.py is the executable copy of the README's Quickstart block.

CI runs the file; nothing runs the README. Without this check the two can drift
and the documented snippet silently stops being the tested one -- which is how
the README came to claim an output of [6, 4, 2] that the code never produced.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _readme_quickstart() -> str:
    md = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"## Quickstart\n\n```python\n(.*?)```", md, re.S)
    assert match, "README.md no longer has a '## Quickstart' python block"
    return match.group(1).rstrip("\n")


def test_quickstart_file_contains_the_readme_snippet_verbatim():
    script = (REPO_ROOT / "examples" / "quickstart.py").read_text(encoding="utf-8")
    snippet = _readme_quickstart()
    assert snippet in script, (
        "examples/quickstart.py has drifted from the README Quickstart block. "
        "Update whichever is wrong so the snippet appears verbatim in the file."
    )


def test_quickstart_asserts_the_documented_order():
    """The README prints the tokens, so order is documented behaviour; the file
    must assert it unsorted."""
    script = (REPO_ROOT / "examples" / "quickstart.py").read_text(encoding="utf-8")
    assert "== [2, 4, 6]" in script
    assert "sorted(" not in script
