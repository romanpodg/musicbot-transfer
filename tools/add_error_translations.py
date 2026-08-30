"""One-shot: add translations for error codes the coverage scan discovered.

Run from the repository root::

    python tools/add_error_translations.py

Every code below was found by ``tests/unit/test_localization.py`` walking the
source with ``ast``: it is raised somewhere but had no catalog entry, so it
would have reached a user as a raw code.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "music_transfer" / "locales"

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "account_id_missing": "⚠️ Internal error: an account record has no identifier.",
        "account_missing": "⚠️ The service did not return an account for this session.",
        "account_platform_id_missing": "⚠️ Internal error: an account record has no platform identifier.",
        "album_source_id_missing": "⚠️ Internal error: an album arrived without a source identifier.",
        "artist_source_id_missing": "⚠️ Internal error: an artist arrived without a source identifier.",
        "authorization_error": "⚠️ The service refused to authorize this action.",
        "capability_unknown": "⚠️ Internal error: unknown capability {capability}.",
        "capability_unsupported": "⚠️ This platform does not support the requested operation ({capability}).",
        "destination_identifier_missing": "⚠️ The item could not be matched on the destination, so it was not written.",
        "favorite_category_unsupported": "⚠️ This library section is not supported by the service.",
        "favorites_unavailable": "⚠️ The service could not return this favourites list.",
        "folder_creation_failed": "⚠️ The folder could not be created.",
        "item_unavailable": "⚠️ The item is not available for this account or region.",
        "oauth_login_failed": "⚠️ Sign-in did not complete. Please connect the account again.",
        "oauth_url_missing": "⚠️ The service did not return a sign-in link. Please try again.",
        "playlist_creation_failed": "⚠️ The playlist could not be created.",
        "playlist_creation_unconfirmed": "⚠️ The playlist may have been created, but the service did not confirm it. It was recorded for reconciliation.",
        "playlist_deletion_failed": "⚠️ The playlist could not be deleted.",
        "playlist_item_not_added": "⚠️ The track could not be added to the playlist.",
        "playlist_resume_mismatch": "⚠️ The playlist on the destination does not match the plan. The item was left for reconciliation.",
        "playlist_source_id_missing": "⚠️ Internal error: a playlist arrived without a source identifier.",
        "provider_id_missing": "⚠️ The service response is missing an identifier. Nothing was changed.",
        "provider_mutation_failed": "⚠️ The service did not complete the write.",
        "provider_mutation_rejected": "⚠️ The service rejected the write.",
        "rate_limited_write_unconfirmed": "⚠️ The rate limit was reached and the result is unknown. It was recorded for reconciliation, not retried blindly.",
        "read_only_adapter_write_blocked": "⚠️ Internal error: a write was attempted while only reading was allowed ({capability}).",
        "session_initialization_failed": "⚠️ The session could not be started. Please connect the account again.",
        "session_not_valid": "⚠️ The saved session is no longer valid. Please connect the account again.",
        "snapshot_account_missing": "⚠️ Internal error: a library snapshot arrived without an account.",
        "track_source_id_missing": "⚠️ Internal error: a track arrived without a source identifier.",
    },
    "ru": {
        "account_id_missing": "⚠️ Внутренняя ошибка: у записи аккаунта нет идентификатора.",
        "account_missing": "⚠️ Сервис не вернул аккаунт для этой сессии.",
        "account_platform_id_missing": "⚠️ Внутренняя ошибка: у записи аккаунта нет идентификатора платформы.",
        "album_source_id_missing": "⚠️ Внутренняя ошибка: у альбома нет идентификатора в источнике.",
        "artist_source_id_missing": "⚠️ Внутренняя ошибка: у исполнителя нет идентификатора в источнике.",
        "authorization_error": "⚠️ Сервис отказал в доступе к этому действию.",
        "capability_unknown": "⚠️ Внутренняя ошибка: неизвестная возможность {capability}.",
        "capability_unsupported": "⚠️ Платформа не поддерживает запрошенную операцию ({capability}).",
        "destination_identifier_missing": "⚠️ Объект не удалось сопоставить на целевом сервисе, поэтому он не записан.",
        "favorite_category_unsupported": "⚠️ Этот раздел библиотеки не поддерживается сервисом.",
        "favorites_unavailable": "⚠️ Сервис не смог вернуть этот список избранного.",
        "folder_creation_failed": "⚠️ Не удалось создать папку.",
        "item_unavailable": "⚠️ Объект недоступен для этого аккаунта или региона.",
        "oauth_login_failed": "⚠️ Вход не завершён. Подключите аккаунт заново.",
        "oauth_url_missing": "⚠️ Сервис не вернул ссылку для входа. Повторите попытку.",
        "playlist_creation_failed": "⚠️ Не удалось создать плейлист.",
        "playlist_creation_unconfirmed": "⚠️ Плейлист, возможно, создан, но сервис это не подтвердил. Операция записана для сверки.",
        "playlist_deletion_failed": "⚠️ Не удалось удалить плейлист.",
        "playlist_item_not_added": "⚠️ Не удалось добавить трек в плейлист.",
        "playlist_resume_mismatch": "⚠️ Плейлист на целевом сервисе не совпадает с планом. Объект оставлен для сверки.",
        "playlist_source_id_missing": "⚠️ Внутренняя ошибка: у плейлиста нет идентификатора в источнике.",
        "provider_id_missing": "⚠️ В ответе сервиса отсутствует идентификатор. Ничего не изменено.",
        "provider_mutation_failed": "⚠️ Сервис не завершил запись.",
        "provider_mutation_rejected": "⚠️ Сервис отклонил запись.",
        "rate_limited_write_unconfirmed": "⚠️ Достигнут лимит запросов, результат неизвестен. Операция записана для сверки, а не повторяется вслепую.",
        "read_only_adapter_write_blocked": "⚠️ Внутренняя ошибка: попытка записи при разрешённом только чтении ({capability}).",
        "session_initialization_failed": "⚠️ Не удалось начать сессию. Подключите аккаунт заново.",
        "session_not_valid": "⚠️ Сохранённая сессия больше недействительна. Подключите аккаунт заново.",
        "snapshot_account_missing": "⚠️ Внутренняя ошибка: снимок библиотеки пришёл без аккаунта.",
        "track_source_id_missing": "⚠️ Внутренняя ошибка: у трека нет идентификатора в источнике.",
    },
}


def main() -> None:
    for language, additions in MESSAGES.items():
        path = ROOT / language / "messages.json"
        catalog = json.loads(path.read_text(encoding="utf-8"))
        errors = catalog.setdefault("errors", {})
        for code, message in additions.items():
            errors.setdefault(code, message)
        catalog["errors"] = dict(sorted(errors.items()))
        path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"{language}: {len(catalog['errors'])} error messages")


if __name__ == "__main__":
    main()
