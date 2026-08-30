"""The account model shared by every interface.

An account identifies a *music-service* identity.  It deliberately has no
Telegram user id, no chat id, and no token: the interface layer owns those.
A future Telegram screen such as::

    TIDAL
    Connected
    roman

    Spotify
    Not connected

is rendered from this model plus the connection state of the account service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..enums import Platform


@dataclass(frozen=True, slots=True)
class AccountProfile:
    """A display-oriented account summary captured during an export.

    Backups and reports store profiles rather than :class:`Account` records
    because a snapshot must remain meaningful even after the account row is
    disconnected.  It carries no authentication material by construction.
    """

    account_id: str
    display_name: str | None = None
    platform: Platform | None = None

    @property
    def label(self) -> str:
        """Return a display name, falling back to the platform identifier."""

        return self.display_name or self.account_id

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "account_id": self.account_id,
            "display_name": self.display_name,
            "platform": str(self.platform) if self.platform else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AccountProfile:
        """Rebuild a profile from persisted data."""

        platform_value = value.get("platform")
        return cls(
            account_id=str(value.get("account_id", "")),
            display_name=value.get("display_name"),
            platform=Platform(str(platform_value)) if platform_value else None,
        )


@dataclass(frozen=True, slots=True)
class Account:
    """A connected (or known) music-service account.

    Attributes:
        id: Internal, stable identifier used by repositories.
        platform: Which music service this account belongs to.
        platform_account_id: The service's own identifier for the user.
        display_name: Human-readable name shown in interfaces.
        owner_user_id: Opaque interface-level owner (a Telegram user id in the
            future).  Optional so the core stays usable from the CLI.
        auth_reference: A *pointer* to stored credentials (for example a
            keyring service/username pair).  Never a token or password.
        metadata: Non-secret extras.
    """

    id: str
    platform: Platform
    platform_account_id: str
    display_name: str | None = None
    owner_user_id: str | None = None
    auth_reference: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.id):
            raise ValueError("account_id_missing")
        if not str(self.platform_account_id):
            raise ValueError("account_platform_id_missing")

    @property
    def label(self) -> str:
        """Return the display name, falling back to the platform id."""

        return self.display_name or self.platform_account_id

    @classmethod
    def create(
        cls,
        platform: Platform,
        platform_account_id: str,
        display_name: str | None = None,
        *,
        owner_user_id: str | None = None,
        auth_reference: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Account:
        """Build an account with a generated stable internal identifier.

        ``new_identifier`` is imported lazily to keep this module free of a
        circular import with :mod:`music_transfer.core.domain.transfer`.
        """

        from .transfer import new_identifier

        return cls(
            id=new_identifier("acct"),
            platform=platform,
            platform_account_id=str(platform_account_id),
            display_name=display_name,
            owner_user_id=owner_user_id,
            auth_reference=auth_reference,
            metadata=dict(metadata or {}),
        )

    def same_identity(self, other: Account | None) -> bool:
        """Return whether two accounts are literally the same remote account.

        Used to reject a meaningless ``account A -> account A`` transfer while
        still allowing ``TIDAL account A -> TIDAL account B``, which is a
        legitimate use case.
        """

        if other is None:
            return False
        return (
            self.platform == other.platform
            and self.platform_account_id == other.platform_account_id
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values.  Contains no credentials."""

        return {
            "id": self.id,
            "platform": str(self.platform),
            "platform_account_id": self.platform_account_id,
            "display_name": self.display_name,
            "owner_user_id": self.owner_user_id,
            "auth_reference": self.auth_reference,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Account:
        """Rebuild an account from persisted data."""

        return cls(
            id=str(value.get("id", "")),
            platform=Platform(str(value.get("platform"))),
            platform_account_id=str(value.get("platform_account_id", "")),
            display_name=value.get("display_name"),
            owner_user_id=value.get("owner_user_id"),
            auth_reference=value.get("auth_reference"),
            metadata=dict(value.get("metadata") or {}),
        )
