"""The oldest Python this package runs on, checked before anything else loads.

Hooks run ``python3`` from the host's PATH, which can be older than the
package supports. The entry scripts call this before importing the rest of the
package, so an old interpreter gets one plain line instead of a traceback.
This module and the package ``__init__`` must stay importable on old Python
versions, so neither uses syntax newer than Python 3.6.
"""

MINIMUM_PYTHON = (3, 11)


def unsupported_python_message(version_info):
    """Return one line when ``version_info`` is too old, else None."""

    if tuple(version_info[:2]) >= MINIMUM_PYTHON:
        return None
    found = ".".join(str(part) for part in version_info[:3])
    minimum = ".".join(str(part) for part in MINIMUM_PYTHON)
    return (
        "Agent Efficiency needs Python {} or newer, but this python3 is {}; "
        "put a newer python3 on PATH.".format(minimum, found)
    )
