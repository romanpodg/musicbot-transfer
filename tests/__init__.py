"""Test suite for music-transfer.

Layout::

    tests/unit         fast, no-network tests for the new architecture
    tests/integration  tests that exercise two layers together
    tests/legacy       the original tidal_manager suite, preserved verbatim
    tests/support.py   shared builders and a stateful fake platform adapter

Run everything from the repository root::

    python -m unittest discover -s tests -t .

``-t .`` is required: without it unittest makes ``tests`` the top-level
directory and the ``tests.support`` import fails.
"""
