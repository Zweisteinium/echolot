"""Echolot: playlists and likes in, only the right songs in the best available quality out."""

import os
from importlib.metadata import version

__version__ = version("echolot")
COMMIT = os.environ.get("ECHOLOT_COMMIT", "")[:7]  # the commit an image was built from (Dockerfile), '' elsewhere
