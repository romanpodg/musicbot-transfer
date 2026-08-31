"""Connection state for music-service accounts.

This service answers the question every interface eventually asks:

    TIDAL
    Connected
    roman

    Spotify
    Not connected

It deliberately knows nothing about Telegram, chat ids, or keyboards.  It also
never sees a token: authentication is delegated to platform authenticators, and
only an ``auth_reference`` (a pointer into the OS keyring) is persisted.

Platforms without an adapter are reported as *not implemented* rather than
faked, so a "Connect Spotify" button can be rendered greyed out with an honest
reason instead of appearing to work.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ...core.domain import Account, AccountProfile
from ...core.enums import Platform
from ...core.errors import UnsupportedCapabilityError
from ...core.ports import AccountRepository, MusicPlatformAdapter
from ...platforms.registry import PlatformRegistry, default_registry
from ..dto import AccountStatus

_LOGGER = logging.getLogger("music_transfer.app.accounts")

#: Text emitted during an interactive device-flow login (a URL, a hint...).
MessageCallback = Callable[[str], None]


class AccountService:
    """Connect, list, and forget music-service accounts."""

    def __init__(
        self,
        accounts: AccountRepository,
        *,
        registry: PlatformRegistry | None = None,
        authenticators: dict[Platform, Any] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._accounts = accounts
        self._registry = registry or default_registry()
        self._authenticators: dict[Platform, Any] = dict(authenticators or {})
        self._logger = logger or _LOGGER

    # -- discovery ---------------------------------------------------------

    def supported_platforms(self) -> tuple[Platform, ...]:
        """Return platforms that have a working adapter registered."""

        return tuple(self._registry.registered())

    def is_implemented(self, platform: Platform) -> bool:
        """Return whether an adapter exists for ``platform``.

        A platform that is merely *planned* is reported as not implemented, so
        interfaces never offer a button that cannot work.
        """

        return self._registry.capabilities_for(platform) is not None

    def capabilities_for(self, platform: Platform) -> Any:
        """Return the capability declaration, raising when unavailable.

        Raises:
            UnsupportedCapabilityError: If the platform has no adapter.
        """

        capabilities = self._registry.capabilities_for(platform)
        if capabilities is None:
            raise UnsupportedCapabilityError(
                "platform_not_implemented", capability=f"platform:{platform.value}"
            )
        return capabilities

    # -- connection --------------------------------------------------------

    def connect(
        self,
        platform: Platform,
        role: str = "source",
        *,
        emit: MessageCallback | None = None,
        owner_user_id: str | None = None,
        **kwargs: Any,
    ) -> Account:
        """Authenticate against ``platform`` and persist the account record.

        Args:
            platform: The service to connect.
            role: A platform-specific slot (``source``/``destination`` for
                TIDAL).  Multiple accounts on the same platform are allowed,
                which is what makes ``TIDAL A -> TIDAL B`` possible.
            emit: Optional sink for interactive messages (the OAuth URL).
            owner_user_id: Opaque interface-level owner (a Telegram user id
                later).  Stored, never interpreted.

        Returns:
            The persisted :class:`Account`.  It carries an ``auth_reference``
            only - never a token.

        Raises:
            UnsupportedCapabilityError: If the platform has no adapter.
            AuthenticationError: If login did not complete.
        """

        self._require_implemented(platform)
        authenticator = self._authenticators.get(platform)
        if authenticator is None:
            raise UnsupportedCapabilityError(
                "platform_authenticator_missing",
                capability=f"platform:{platform.value}",
            )
        client = authenticator.connect(role, emit or (lambda message: None), **kwargs)
        adapter = self._wrap(platform, client)
        profile = adapter.get_profile()
        account = self._upsert(platform, profile, role, owner_user_id)
        self._logger.info(
            "event=account_connected platform=%s role=%s account_id=%s",
            platform.value,
            role,
            account.id,
        )
        return account

    def adapter_for(self, account: Account, role: str = "source") -> MusicPlatformAdapter:
        """Return a live adapter for an already-connected account.

        Raises:
            AuthenticationError: If no session exists for the account.
        """

        self._require_implemented(account.platform)
        authenticator = self._authenticators.get(account.platform)
        if authenticator is None:
            raise UnsupportedCapabilityError(
                "platform_authenticator_missing",
                capability=f"platform:{account.platform.value}",
            )
        client = authenticator.connect(role, lambda message: None)
        return self._wrap(account.platform, client)

    def forget(self, platform: Platform, role: str = "source") -> bool:
        """Drop the stored session for a platform/role pair.

        Returns:
            ``True`` when something was removed, ``False`` when there was
            nothing to forget.  Local account rows are kept so historical jobs
            stay readable; only the credential is deleted.
        """

        authenticator = self._authenticators.get(platform)
        if authenticator is None:
            return False
        authenticator.forget(role)
        self._logger.info(
            "event=account_forgotten platform=%s role=%s", platform.value, role
        )
        return True

    def disconnect(self, account: Account, role: str = "source") -> Account:
        """Remove an account row and its credential."""

        self.forget(account.platform, role)
        self._accounts.remove(account.id)
        self._logger.info(
            "event=account_removed platform=%s account_id=%s",
            account.platform.value,
            account.id,
        )
        return account

    # -- listing -----------------------------------------------------------

    def list_accounts(
        self, owner_user_id: str | None = None
    ) -> tuple[Account, ...]:
        """Return known accounts, optionally filtered by interface owner."""

        return tuple(self._accounts.list_all(owner_user_id))

    def statuses(
        self,
        platforms: tuple[Platform, ...] | None = None,
        *,
        owner_user_id: str | None = None,
    ) -> tuple[AccountStatus, ...]:
        """Build the rows an account screen renders.

        Every requested platform appears exactly once, connected or not.  A
        platform with no adapter is reported with ``implemented=False`` so the
        interface can say "not implemented yet" instead of "not connected".
        """

        wanted = platforms or tuple(Platform)
        known = {account.platform: account for account in self.list_accounts(owner_user_id)}
        rows: list[AccountStatus] = []
        for platform in wanted:
            account = known.get(platform)
            implemented = self.is_implemented(platform)
            note = None if implemented else "platform_not_implemented"
            rows.append(
                AccountStatus(
                    platform=platform,
                    connected=account is not None,
                    display_name=account.display_name if account else None,
                    platform_account_id=(
                        account.platform_account_id if account else None
                    ),
                    account_id=account.id if account else None,
                    implemented=implemented,
                    note=note,
                )
            )
        return tuple(rows)

    # -- internals ---------------------------------------------------------

    def _require_implemented(self, platform: Platform) -> None:
        """Raise when no adapter is registered for ``platform``."""

        if not self.is_implemented(platform):
            raise UnsupportedCapabilityError(
                "platform_not_implemented",
                capability=f"platform:{platform.value}",
            )

    def _wrap(self, platform: Platform, client: Any) -> MusicPlatformAdapter:
        """Wrap an authenticated client in its platform adapter."""

        return self._registry.create(platform, client)

    def _upsert(
        self,
        platform: Platform,
        profile: AccountProfile,
        role: str,
        owner_user_id: str | None,
    ) -> Account:
        """Persist an account row, reusing it when the identity is unchanged."""

        existing = self._accounts.find(platform, profile.account_id)
        if existing is not None:
            if owner_user_id and existing.owner_user_id != owner_user_id:
                updated = Account(
                    id=existing.id,
                    platform=existing.platform,
                    platform_account_id=existing.platform_account_id,
                    display_name=profile.display_name or existing.display_name,
                    owner_user_id=owner_user_id,
                    auth_reference=existing.auth_reference,
                    metadata={**existing.metadata, "role": role},
                )
                return self._accounts.update(updated)
            return existing
        account = Account.create(
            platform,
            profile.account_id,
            profile.display_name,
            owner_user_id=owner_user_id,
            auth_reference=f"keyring:{platform.value}:{role}",
            metadata={"role": role},
        )
        self._accounts.add(account)
        return account


def describe_status(status: AccountStatus) -> str:
    """Render one account row as plain text.

    Kept here (not in an interface) so the CLI and any future bot show the same
    three lines for the same state.
    """

    state = "Connected" if status.connected else "Not connected"
    if not status.implemented:
        state = "Not implemented yet"
    name = status.display_name or status.platform_account_id or "—"
    return f"{status.platform.value}\n{state}\n{name}"


__all__ = ["AccountService", "MessageCallback", "describe_status"]
