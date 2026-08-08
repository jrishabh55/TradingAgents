"""Single source of truth for the helper's version.

Bump this when shipping a new build. The portal API serves it from
``/api/helper/version`` (the deployed API tree defines "latest"), and a running
helper compares its own copy against that to offer an update.
"""
__version__ = "0.1.0"
