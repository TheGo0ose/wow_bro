"""Exceptions shared across the package."""


class DataError(Exception):
    """Input data cannot be used at all (missing file, missing columns, bad bounds)."""
