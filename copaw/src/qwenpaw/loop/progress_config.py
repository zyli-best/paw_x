"""Internal settings for native ReAct progress reminders."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressReminderConfig:
    enabled: bool = True
    interval_seconds: int = 300

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("Progress reminder enabled must be a boolean")
        value = self.interval_seconds
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(
                "Progress reminder interval_seconds must be a positive integer"
            )
