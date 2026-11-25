"""Compatibility entry point for Kodi and existing imports."""

import sys

from yawsp_app.router import router

__all__ = ["router"]


if __name__ == "__main__":
    router(sys.argv[2][1:])
