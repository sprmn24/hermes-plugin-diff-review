"""pytest: collect the plugin root as a plain directory.

The root holds the plugin's __init__.py, but a checkout directory such as
``diff-review`` is not a valid package name, so pytest cannot import it as a
package. The tests load the plugin themselves (tests/helpers.py).
"""

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent


class _PlainRoot:
    # Registered as a global plugin: a conftest's own hooks are not consulted
    # for the directory that contains it.
    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if path == _ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    config.pluginmanager.register(_PlainRoot(), "diff-review-plain-root")
