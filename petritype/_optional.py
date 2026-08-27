"""Import guards for features that live behind an install extra.

Modules that need a third-party package which the base install does not provide
call :func:`require` before importing it, so the failure names the extra to
install rather than just the missing module.

Deliberately a presence check and nothing more: if the package *is* installed
but broken, the real import that follows raises its own error untouched. A
missing dependency of an extra must never be reported as a missing extra --
that would tell the user to install something they already have.
"""

from importlib.util import find_spec


def require(module_name: str, extra: str) -> None:
    """Raise, naming ``extra``, if ``module_name`` is not installed."""
    if find_spec(module_name) is None:
        raise ModuleNotFoundError(
            f"petritype's '{extra}' features require {module_name}. "
            f"Install them with: pip install 'petritype[{extra}]'",
            name=module_name,
        )
