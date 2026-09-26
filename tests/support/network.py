"""Fixture processing opens no network connection; the in-process storage catalog needs none."""


def refuse(*args, **kwargs):
    """Stand in for socket connect and create_connection by refusing every connection."""
    raise AssertionError("fixture processing attempted a network connection")
