"""Localized, testable input and high-risk confirmation prompts."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from localization.manager import LocalizationManager

from .progress import Console


class Prompts:
    """Centralize every interactive input and confirmation decision."""

    def __init__(
        self,
        console: Console,
        input_function: Callable[[str], str] | None = None,
    ) -> None:
        self._console = console
        self._i18n: LocalizationManager = console.i18n
        self._input = input_function or input

    def choose(self, prompt_key: str, options: Mapping[str, str]) -> str:
        """Display localized choices until the user selects an advertised key."""

        self._console.message(prompt_key, style="heading")
        for value, label_key in options.items():
            self._console.message(
                "prompt.option_line", number=value, label=self._i18n.t(label_key)
            )
        while True:
            choice = self._input(self._i18n.t("prompt.answer")).strip()
            if choice in options:
                return choice
            self._console.message("prompt.invalid_choice", style="warning")

    def choose_language(self, languages: Mapping[str, str]) -> str:
        """Offer language choices using their native display names."""

        items = list(languages.items())
        self._console.message("language.choose", style="heading")
        for index, (_, name) in enumerate(items, start=1):
            self._console.message("prompt.option_line", number=index, label=name)
        while True:
            choice = self._input(self._i18n.t("prompt.answer")).strip()
            if choice.isdigit() and 1 <= int(choice) <= len(items):
                return items[int(choice) - 1][0]
            self._console.message("prompt.invalid_choice", style="warning")

    def choose_values(self, prompt_key: str, options: Mapping[str, str]) -> str:
        """Choose dynamic display values, such as locally discovered backup names."""

        self._console.message(prompt_key, style="heading")
        for value, label in options.items():
            self._console.message("prompt.option_line", number=value, label=label)
        while True:
            choice = self._input(self._i18n.t("prompt.answer")).strip()
            if choice in options:
                return choice
            self._console.message("prompt.invalid_choice", style="warning")

    def yes_no(self, prompt_key: str, **values: object) -> bool:
        """Ask a localized yes/no question and reject ambiguous responses."""

        self._console.message(prompt_key, **values)
        accepted = _answers(self._i18n.t("input.yes"))
        rejected = _answers(self._i18n.t("input.no"))
        while True:
            answer = self._input(self._i18n.t("prompt.yes_no")).strip().casefold()
            if answer in accepted:
                return True
            if answer in rejected:
                return False
            self._console.message("prompt.invalid_yes_no", style="warning")

    def confirm_mutation(self, account: str, action: str) -> bool:
        """Show the mandatory write-operation warning before any mutation."""

        self._console.message("confirmation.warning_title", style="warning")
        self._console.message("confirmation.account", account=account)
        self._console.message("confirmation.action", action=action)
        self._console.message("confirmation.mutates_account", style="warning")
        return self.yes_no("confirmation.continue")

    def confirm_deletion(self, account: str, summary: str) -> bool:
        """Require a clear yes/no decision and an exact destructive phrase."""

        self._console.message("confirmation.danger_title", style="error")
        self._console.message("confirmation.account", account=account)
        self._console.message("confirmation.deleting", summary=summary)
        if not self.yes_no("confirmation.continue"):
            return False
        self._console.message("confirmation.irreversible", style="error")
        self._console.message("confirmation.type_phrase", phrase=self._i18n.t("confirmation.delete_phrase"))
        answer = self._input(self._i18n.t("prompt.answer")).strip()
        return answer == self._i18n.t("confirmation.delete_phrase")


def _answers(value: str) -> set[str]:
    """Parse comma-separated localized response aliases."""

    return {item.strip().casefold() for item in value.split(",") if item.strip()}
