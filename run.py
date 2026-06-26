#!/usr/bin/env python3
"""Entrypoint. Usage: python run.py [selftest|once|loop|status|flatten] [--live] [--config path]"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from sentinel.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
