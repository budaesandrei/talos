"""Enables ``python -m talos`` as an entrypoint, equivalent to the
``talos`` console script declared in pyproject.toml."""

from talos.cli import app

if __name__ == "__main__":
    app()
