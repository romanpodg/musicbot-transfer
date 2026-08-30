"""Interface adapters (CLI today, Telegram later).

Interfaces translate user intent into application-service calls and render
results.  They must never contain transfer, matching, or platform logic: that
lives in :mod:`music_transfer.core` and :mod:`music_transfer.app`.
"""
