from dataclasses import dataclass
import os


@dataclass
class ValidationResult:
    is_valid: bool
    missing: list[str]


@dataclass
class AppContext:
    bot_token: str
    channel_id: str


def validate_environment() -> ValidationResult:
    required = [
        "BOT_TOKEN",
        "CHANNEL_ID",
    ]

    missing = [k for k in required if not os.getenv(k)]

    return ValidationResult(
        is_valid=len(missing) == 0,
        missing=missing,
    )


def build_app_context() -> AppContext:
    return AppContext(
        bot_token=os.getenv("BOT_TOKEN", ""),
        channel_id=os.getenv("CHANNEL_ID", ""),
    )
