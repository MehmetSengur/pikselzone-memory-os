"""Contabo entry: install central memory discovery, then existing guards unchanged."""
from __future__ import annotations
import sys
from .profile_integration import install_native


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    sys.argv = ['hermes', *args]
    # Apply native -p override before discovery/config imports, exactly as guards do.
    import hermes_cli.main
    install_native()
    from .hermes_guards import main as guarded_main
    # Native import has already consumed global -p/--profile. Passing the
    # original args would reintroduce it after Hermes has finished that phase.
    return guarded_main(list(sys.argv[1:]))


if __name__ == '__main__':
    raise SystemExit(main())
