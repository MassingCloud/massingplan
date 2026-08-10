"""Marks `tests` as a package so `from tests.x import y` resolves.

Two modules share `fixture_schedule` from `test_persistence`. Without this file
that import works under `python -m pytest`, which puts the working directory on
`sys.path`, and fails under plain `pytest`, which does not -- so the suite
passed locally and could not even be collected in CI.
"""
