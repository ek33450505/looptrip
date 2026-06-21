"""Enable ``python -m looptrip ...`` by delegating to the CLI entry point."""

from .cli import main

raise SystemExit(main())
