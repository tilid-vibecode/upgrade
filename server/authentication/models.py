"""Compatibility imports for legacy code paths.

The prototype currently relies on Django's built-in ``auth.User`` model.
Some older modules still import ``User`` from ``authentication.models``; this
module keeps those imports working until the dedicated auth layer is revived.
"""

from django.contrib.auth.models import User  # noqa: F401
