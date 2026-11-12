"""Settings for explicitly constructed failure-recovery gates."""

from dataclasses import dataclass


@dataclass(frozen=True)
class FailureLoopConfig:
    """Enable model review and bound its optional advice to ReAct."""

    enabled: bool = False
    check_interval: int = 5
    max_hint_tokens: int = 512

    def __post_init__(self) -> None:
        if (
            isinstance(self.check_interval, bool)
            or not isinstance(self.check_interval, int)
            or self.check_interval < 1
        ):
            raise ValueError(
                "Failure-loop check interval must be a positive number of rounds"
            )
        if (
            isinstance(self.max_hint_tokens, bool)
            or not isinstance(self.max_hint_tokens, int)
            or self.max_hint_tokens < 1
        ):
            raise ValueError(
                "Failure-loop hint budget must be a positive integer"
            )
