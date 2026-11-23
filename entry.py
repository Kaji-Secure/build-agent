"""The entry point Nuitka compiles: it imports the package, so that its relative imports work (a package's own __main__.py compiled alone has no parent)."""

from kaji_agent.__main__ import main

main()
