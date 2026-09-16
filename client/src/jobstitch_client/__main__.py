"""``python -m jobstitch_client`` and the frozen executable's entry point."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
