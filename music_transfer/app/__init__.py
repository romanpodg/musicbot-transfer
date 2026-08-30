"""Application layer: use cases shared by every interface.

This package may depend on :mod:`music_transfer.core` and
:mod:`music_transfer.infrastructure`, never on a UI framework.  The CLI, a
future Telegram bot, and future queue workers all call the same services here,
which is what keeps business logic out of the interfaces.
"""
