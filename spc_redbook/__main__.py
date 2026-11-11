"""Allow ``python -m spc_redbook``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
