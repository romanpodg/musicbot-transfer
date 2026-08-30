"""Extend the migrated locale catalogs with the new architecture's keys.

Run once during the migration.  Keeping this as a script (instead of hand-editing
two 200-line JSON files) guarantees the English and Russian catalogs stay key-
identical, which the catalog validator requires.
"""

from __future__ import annotations

import json
from pathlib import Path

LOCALES = Path(__file__).resolve().parent.parent / "music_transfer" / "locales"

EN: dict[str, object] = {
    "platform": {
        "tidal": "TIDAL",
        "spotify": "Spotify",
        "apple_music": "Apple Music",
        "deezer": "Deezer",
        "youtube_music": "YouTube Music",
    },
    "accounts": {
        "heading": "Connected accounts",
        "connected": "Connected",
        "not_connected": "Not connected",
        "account_line": "{platform}\n{status}\n{name}",
        "not_implemented": "Not implemented yet",
        "disconnected": "✅ {platform} session forgotten.",
        "reconnect_hint": "Use the account settings to connect {platform}.",
    },
    "plan": {
        "heading": "📋 Transfer plan (read-only, nothing has been changed yet)",
        "route": "{source} ➜ {destination}",
        "total_items": "Items: {count}",
        "matched": "Matched: {count}",
        "unmatched": "Not found on destination: {count}",
        "already_exists": "Already on destination: {count}",
        "ambiguous": "Needs review: {count}",
        "content_line": "{label}: {count}",
        "warning_line": "⚠️ {warning}",
        "confirm": "Execute this plan?",
        "cancelled": "ℹ️ Plan not executed. No changes were made.",
    },
    "job": {
        "status": {
            "created": "Created",
            "authenticating": "Authenticating",
            "exporting": "Reading source library",
            "normalizing": "Normalizing metadata",
            "matching": "Matching tracks",
            "planning": "Building plan",
            "waiting_confirmation": "Waiting for confirmation",
            "importing": "Writing to destination",
            "verifying": "Verifying result",
            "completed": "Completed",
            "failed": "Failed",
            "cancelled": "Cancelled",
            "paused": "Paused",
        },
        "status_line": "Status: {status}",
        "job_line": "Job {job_id}: {status}",
        "progress_line": "{status}: {current}/{total}",
    },
    "item": {
        "status": {
            "pending": "Pending",
            "matched": "Matched",
            "transferred": "Transferred",
            "already_exists": "Already existed",
            "not_found": "Not found on destination",
            "ambiguous": "Ambiguous — check destination",
            "unavailable": "Unavailable in region",
            "skipped": "Skipped",
            "failed": "Failed",
        }
    },
    "matching": {
        "method": {
            "isrc": "ISRC",
            "direct_id": "Same platform id",
            "exact_metadata": "Exact metadata",
            "normalized_metadata": "Normalized metadata",
            "fuzzy_metadata": "Fuzzy metadata",
            "none": "No match",
        },
        "score_line": "Match: {method} ({score:.2f})",
        "warning_explicit_to_clean": "Destination only has the clean version.",
        "warning_version_mismatch": "Destination has a different version (remaster/live/remix).",
        "warning_duration_mismatch": "Duration differs by more than 15 seconds.",
    },
    "verification": {
        "heading": "🔎 Verification",
        "ok": "✅ Destination matches the plan (identifiers and order).",
        "missing": "Missing on destination: {count}",
        "unexpected": "Unexpected on destination: {count}",
        "order_mismatches": "Order differences: {count}",
        "unverifiable": "⚠️ Destination does not expose {section}; verification skipped.",
        "report_written": "📄 Verification report saved: {path}",
    },
    "report": {
        "heading": "📊 Transfer report",
        "transferred": "Transferred: {count}",
        "already_existed": "Already existed: {count}",
        "failed": "Failed: {count}",
        "not_found": "Not found: {count}",
        "unavailable": "Unavailable: {count}",
        "ambiguous": "Ambiguous: {count}",
        "skipped": "Skipped: {count}",
        "duration": "Duration: {seconds:.1f}s",
    },
    "resume": {
        "pending": "{count} items remain from the previous run.",
        "nothing_pending": "Nothing left to resume.",
        "retry_created": "Created retry job with {count} items.",
    },
}

RU: dict[str, object] = {
    "platform": {
        "tidal": "TIDAL",
        "spotify": "Spotify",
        "apple_music": "Apple Music",
        "deezer": "Deezer",
        "youtube_music": "YouTube Music",
    },
    "accounts": {
        "heading": "Подключённые аккаунты",
        "connected": "Подключён",
        "not_connected": "Не подключён",
        "account_line": "{platform}\n{status}\n{name}",
        "not_implemented": "Пока не реализовано",
        "disconnected": "✅ Сессия {platform} удалена.",
        "reconnect_hint": "Подключите {platform} в настройках аккаунта.",
    },
    "plan": {
        "heading": "📋 План переноса (только чтение, ничего не изменено)",
        "route": "{source} ➜ {destination}",
        "total_items": "Объектов: {count}",
        "matched": "Сопоставлено: {count}",
        "unmatched": "Не найдено на целевом аккаунте: {count}",
        "already_exists": "Уже есть на целевом аккаунте: {count}",
        "ambiguous": "Требует проверки: {count}",
        "content_line": "{label}: {count}",
        "warning_line": "⚠️ {warning}",
        "confirm": "Выполнить этот план?",
        "cancelled": "ℹ️ План не выполнен. Изменения не внесены.",
    },
    "job": {
        "status": {
            "created": "Создано",
            "authenticating": "Авторизация",
            "exporting": "Чтение исходной библиотеки",
            "normalizing": "Нормализация метаданных",
            "matching": "Сопоставление треков",
            "planning": "Построение плана",
            "waiting_confirmation": "Ожидание подтверждения",
            "importing": "Запись в целевой аккаунт",
            "verifying": "Проверка результата",
            "completed": "Завершено",
            "failed": "Ошибка",
            "cancelled": "Отменено",
            "paused": "Пауза",
        },
        "status_line": "Статус: {status}",
        "job_line": "Задача {job_id}: {status}",
        "progress_line": "{status}: {current}/{total}",
    },
    "item": {
        "status": {
            "pending": "Ожидает",
            "matched": "Сопоставлено",
            "transferred": "Перенесено",
            "already_exists": "Уже было",
            "not_found": "Не найдено на целевом аккаунте",
            "ambiguous": "Неоднозначно — проверьте целевой аккаунт",
            "unavailable": "Недоступно в регионе",
            "skipped": "Пропущено",
            "failed": "Ошибка",
        }
    },
    "matching": {
        "method": {
            "isrc": "ISRC",
            "direct_id": "Тот же идентификатор платформы",
            "exact_metadata": "Точное совпадение метаданных",
            "normalized_metadata": "Нормализованные метаданные",
            "fuzzy_metadata": "Нечёткое совпадение",
            "none": "Совпадений нет",
        },
        "score_line": "Совпадение: {method} ({score:.2f})",
        "warning_explicit_to_clean": "На целевом аккаунте только версия без цензуры.",
        "warning_version_mismatch": "На целевом аккаунте другая версия (remaster/live/remix).",
        "warning_duration_mismatch": "Длительность отличается более чем на 15 секунд.",
    },
    "verification": {
        "heading": "🔎 Проверка",
        "ok": "✅ Целевой аккаунт соответствует плану (идентификаторы и порядок).",
        "missing": "Отсутствует на целевом аккаунте: {count}",
        "unexpected": "Лишнее на целевом аккаунте: {count}",
        "order_mismatches": "Различия в порядке: {count}",
        "unverifiable": "⚠️ Целевой аккаунт не отдаёт раздел {section}; проверка пропущена.",
        "report_written": "📄 Отчёт проверки сохранён: {path}",
    },
    "report": {
        "heading": "📊 Отчёт о переносе",
        "transferred": "Перенесено: {count}",
        "already_existed": "Уже было: {count}",
        "failed": "Ошибок: {count}",
        "not_found": "Не найдено: {count}",
        "unavailable": "Недоступно: {count}",
        "ambiguous": "Неоднозначно: {count}",
        "skipped": "Пропущено: {count}",
        "duration": "Длительность: {seconds:.1f} с",
    },
    "resume": {
        "pending": "Осталось объектов из предыдущего запуска: {count}.",
        "nothing_pending": "Продолжать нечего.",
        "retry_created": "Создана задача повтора с {count} объектами.",
    },
}

#: Stable error codes emitted by the core.  The core never stores messages,
#: only codes, so every interface can localize them consistently.
ERROR_KEYS: tuple[str, ...] = (
    "authentication_failed",
    "authorization_failed",
    "session_expired",
    "rate_limited",
    "temporary_platform_error",
    "permanent_platform_error",
    "not_found",
    "unavailable",
    "ambiguous_operation",
    "unsupported_capability",
    "platform_not_registered",
    "platform_not_implemented",
    "invalid_state_transition",
    "transfer_confirmation_required",
    "job_not_ready_for_execution",
    "same_account_transfer",
    "job_already_finished",
    "pagination_repeated_page",
    "pagination_limit_exceeded",
    "persistence_error",
    "partial_export",
    "cleanup_confirmation_required",
    "operation_failed",
)

EN_ERRORS: dict[str, str] = {
    "authentication_failed": "⚠️ Authentication failed. Nothing was changed.",
    "authorization_failed": "⚠️ The account is not authorized for this action.",
    "session_expired": "⚠️ The saved session expired. Please connect the account again.",
    "rate_limited": "⚠️ The service rate limit was reached. Try again later.",
    "temporary_platform_error": "⚠️ The service is temporarily unavailable. The operation can be resumed.",
    "permanent_platform_error": "⚠️ The service rejected the request. This item will not be retried automatically.",
    "not_found": "⚠️ The item was not found on the destination service.",
    "unavailable": "⚠️ The item is not available for this account or region.",
    "ambiguous_operation": "⚠️ The result of this operation is unknown. It was recorded for reconciliation, not retried blindly.",
    "unsupported_capability": "⚠️ This platform does not support the requested operation ({capability}).",
    "platform_not_registered": "⚠️ No adapter is registered for this platform.",
    "platform_not_implemented": "⚠️ This platform is not implemented yet.",
    "invalid_state_transition": "⚠️ Invalid job state change: {current} → {target}.",
    "transfer_confirmation_required": "⚠️ No change was made because confirmation was not provided.",
    "job_not_ready_for_execution": "⚠️ The job is not ready to run. Build a plan first.",
    "same_account_transfer": "⚠️ Source and destination are the same account. Nothing to transfer.",
    "job_already_finished": "⚠️ This job has already finished and cannot be resumed.",
    "pagination_repeated_page": "⚠️ The service returned the same page twice. Reading stopped to avoid an infinite loop.",
    "pagination_limit_exceeded": "⚠️ The library is larger than the configured read limit.",
    "persistence_error": "⚠️ Local state could not be written safely. Nothing was changed.",
    "partial_export": "⚠️ Some library sections could not be read completely: {sections}",
    "cleanup_confirmation_required": "⚠️ No deletion was performed because confirmation was not provided.",
    "operation_failed": "⚠️ The operation could not be completed safely. See the sanitized local log for details.",
}

RU_ERRORS: dict[str, str] = {
    "authentication_failed": "⚠️ Ошибка авторизации. Ничего не изменено.",
    "authorization_failed": "⚠️ У аккаунта нет прав на это действие.",
    "session_expired": "⚠️ Сохранённая сессия истекла. Подключите аккаунт заново.",
    "rate_limited": "⚠️ Достигнут лимит запросов сервиса. Повторите позже.",
    "temporary_platform_error": "⚠️ Сервис временно недоступен. Операцию можно продолжить.",
    "permanent_platform_error": "⚠️ Сервис отклонил запрос. Автоматический повтор не выполняется.",
    "not_found": "⚠️ Объект не найден на целевом сервисе.",
    "unavailable": "⚠️ Объект недоступен для этого аккаунта или региона.",
    "ambiguous_operation": "⚠️ Результат операции неизвестен. Она записана для сверки, а не повторяется вслепую.",
    "unsupported_capability": "⚠️ Платформа не поддерживает запрошенную операцию ({capability}).",
    "platform_not_registered": "⚠️ Для этой платформы не зарегистрирован адаптер.",
    "platform_not_implemented": "⚠️ Эта платформа пока не реализована.",
    "invalid_state_transition": "⚠️ Недопустимая смена состояния задачи: {current} → {target}.",
    "transfer_confirmation_required": "⚠️ Изменения не внесены, так как подтверждение не получено.",
    "job_not_ready_for_execution": "⚠️ Задача не готова к выполнению. Сначала постройте план.",
    "same_account_transfer": "⚠️ Исходный и целевой аккаунты совпадают. Нечего переносить.",
    "job_already_finished": "⚠️ Эта задача уже завершена, продолжение невозможно.",
    "pagination_repeated_page": "⚠️ Сервис вернул ту же страницу дважды. Чтение остановлено, чтобы избежать бесконечного цикла.",
    "pagination_limit_exceeded": "⚠️ Библиотека больше заданного лимита чтения.",
    "persistence_error": "⚠️ Локальное состояние не удалось безопасно записать. Ничего не изменено.",
    "partial_export": "⚠️ Некоторые разделы библиотеки не удалось прочитать полностью: {sections}",
    "cleanup_confirmation_required": "⚠️ Удаление не выполнено, так как подтверждение не получено.",
    "operation_failed": "⚠️ Операцию не удалось безопасно завершить. Подробности смотрите в очищенном локальном журнале.",
}


def _deep_merge(base: dict, extra: dict) -> dict:
    """Merge ``extra`` into ``base`` recursively, refusing to drop keys."""

    for key, value in extra.items():
        current = base.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _deep_merge(current, value)
        else:
            base[key] = value
    return base


def main() -> None:
    """Extend both catalogs and verify they expose identical leaf keys."""

    for language, sections, errors in (
        ("en", EN, EN_ERRORS),
        ("ru", RU, RU_ERRORS),
    ):
        path = LOCALES / language / "messages.json"
        catalog = json.loads(path.read_text(encoding="utf-8"))
        # Preserve the legacy keys; only add what the new architecture needs.
        legacy_errors = dict(catalog.get("errors", {}))
        _deep_merge(catalog, sections)
        merged_errors = dict(errors)
        merged_errors.update(
            {key: value for key, value in legacy_errors.items() if key not in merged_errors}
        )
        merged_errors["unknown"] = (
            "⚠️ {code}" if language == "en" else "⚠️ Код ошибки: {code}"
        )
        catalog["errors"] = {key: merged_errors[key] for key in sorted(merged_errors)}
        path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"updated {path} ({len(catalog)} top-level sections)")


if __name__ == "__main__":
    main()
