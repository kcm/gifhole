"""gifhole: a local GIF library."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Read from installed metadata rather than duplicated here, so pyproject
    # stays the single source of truth and the two cannot drift.
    __version__ = version("gifhole")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0.0.0+source"

__all__ = ["__version__"]
