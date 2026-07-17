"""telegram_formatter.py — Telegram message formatting (and optional send).

Pure formatting layer: takes the JSON dicts produced by
ai_decision_engine.py / market_agent.py / news_agent.py and renders a
Telegram-ready message using Telegram's MarkdownV2 dialect. This module
does no analysis of its own beyond a small OI-based support/resistance
estimate (documented below) needed purely for display -- all real
analysis lives in the other agents.

TelegramSender is optional and only used if the caller explicitly wants
to push the formatted message to a chat; it requires a bot token and
chat id (see config.py) and is not invoked by anything else in this
codebase automatically. No trading action is taken by sending a message.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from titan_ai_trader.infrastructure.market_data.retry import retry_on_network_failure

logger = logging.getLogger(__name__)

_DECISION_EMOJI = {"BUY": "🟢", "SELL": "🔴", "WAIT": "🟡"}
_RISK_EMOJI = {"LOW": "🟢", "MEDIUM": "🟠", "HIGH": "🔴"}
_BIAS_EMOJI = {"BULLISH": "📈", "BEARISH": "📉", "NEUTRAL": "➖"}
_SENTIMENT_EMOJI = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}

# Characters MarkdownV2 requires escaping outside of code blocks/entities.
_MARKDOWN_V2_SPECIAL_CHARS = r"_*[]()~`>#+-=|{}.!"


def escape_markdown_v2(text: str) -> str:
    """Escapes text for safe inclusion in a Telegram MarkdownV2 message."""
    pattern = "([" + re.escape(_MARKDOWN_V2_SPECIAL_CHARS) + "])"
    return re.sub(pattern, r"\\\1", text)


@dataclass(frozen=True, slots=True)
class SupportResistanceLevel:
    support: Decimal | None
    resistance: Decimal | None


class SupportResistanceEstimator:
    """A minimal, OI-based support/resistance estimate for display
    purposes only: the highest-OI put strike below spot is treated as
    support, the highest-OI call strike above spot as resistance. This
    is a common, simple heuristic (not the full technical S/R module
    deferred to a later phase) and is scoped here specifically because
    the Telegram summary needs *something* concrete to show and the
    option chain data is already on hand.
    """

    @staticmethod
    def estimate(option_chain_json: dict[str, Any], spot: Decimal) -> SupportResistanceLevel:
        contracts = option_chain_json.get("contracts", [])
        puts_below = [
            c for c in contracts if c["option_type"] == "PE" and Decimal(c["strike_price"]) <= spot
        ]
        calls_above = [
            c for c in contracts if c["option_type"] == "CE" and Decimal(c["strike_price"]) >= spot
        ]
        support = (
            Decimal(max(puts_below, key=lambda c: c["open_interest"])["strike_price"])
            if puts_below
            else None
        )
        resistance = (
            Decimal(max(calls_above, key=lambda c: c["open_interest"])["strike_price"])
            if calls_above
            else None
        )
        return SupportResistanceLevel(support=support, resistance=resistance)


class TelegramFormatter:
    """Renders ai_decision_engine.py output (plus optional market/news
    context) into a Telegram MarkdownV2 message."""

    @staticmethod
    def _table(rows: list[tuple[str, str]]) -> str:
        """A simple aligned two-column table inside a code block --
        Telegram has no native Markdown table support, so a monospace
        code block is the conventional way to render one."""
        label_width = max((len(r[0]) for r in rows), default=0)
        lines = [f"{label:<{label_width}} : {value}" for label, value in rows]
        return "```\n" + "\n".join(lines) + "\n```"

    def format_decision_message(
        self,
        decision_json: dict[str, Any],
        index_report_json: dict[str, Any] | None = None,
        news_json: dict[str, Any] | None = None,
    ) -> str:
        decision = decision_json["decision"]
        symbol = decision_json["symbol"]
        emoji = _DECISION_EMOJI.get(decision, "⚪")
        risk_emoji = _RISK_EMOJI.get(decision_json["risk_level"], "⚪")
        bias_emoji = _BIAS_EMOJI.get(decision_json["market_bias"], "➖")

        lines: list[str] = []
        lines.append(f"{emoji} *TITAN AI TRADER — {escape_markdown_v2(symbol)}*")
        lines.append(f"*Decision:* {emoji} `{decision}`")
        lines.append("")

        lines.append(
            self._table(
                [
                    ("Market Score", f"{decision_json['overall_market_score']}/100"),
                    ("Market Bias", f"{bias_emoji} {decision_json['market_bias']}"),
                    ("Confidence", f"{decision_json['confidence']}%"),
                    ("Probability", f"{decision_json['probability_pct']}%"),
                    ("Risk Level", f"{risk_emoji} {decision_json['risk_level']}"),
                    ("Expected Move", str(decision_json["expected_move"])),
                ]
            )
        )
        lines.append("")

        if index_report_json is not None:
            spot = Decimal(index_report_json["spot"]["last_price"])
            sr = SupportResistanceEstimator.estimate(index_report_json["option_chain"], spot)
            lines.append("*📊 Support / Resistance*")
            lines.append(
                self._table(
                    [
                        ("Spot", str(spot)),
                        ("Support", str(sr.support) if sr.support is not None else "n/a"),
                        ("Resistance", str(sr.resistance) if sr.resistance is not None else "n/a"),
                        ("PCR", index_report_json["pcr"]["ratio"]),
                        ("Max Pain", index_report_json["max_pain"]["max_pain_strike"]),
                    ]
                )
            )
            lines.append("")

        best_strategy = decision_json.get("best_strategy")
        if best_strategy is not None:
            lines.append(f"*🎯 Best Strategy: {escape_markdown_v2(best_strategy['strategy_type'])}*")
            lines.append(
                self._table(
                    [
                        ("Direction", best_strategy["direction"]),
                        ("Entry", best_strategy["entry"]),
                        ("Stop Loss", best_strategy["stop_loss"]),
                        ("Target 1", best_strategy["target_1"]),
                        ("Target 2", best_strategy["target_2"]),
                        ("Target 3", best_strategy["target_3"]),
                        ("R:R (T2)", best_strategy["risk_reward_2"]),
                        ("Position Size", str(best_strategy["position_size"])),
                        ("Confidence", f"{best_strategy['confidence_score']}%"),
                    ]
                )
            )
            lines.append("")

        if news_json is not None:
            sentiment = news_json["overall_sentiment"]
            sentiment_emoji = _SENTIMENT_EMOJI.get(sentiment["label"], "⚪")
            lines.append("*📰 News Summary*")
            lines.append(
                f"{sentiment_emoji} {escape_markdown_v2(sentiment['label'])} "
                f"\\({sentiment['bullish_count']} bullish / {sentiment['bearish_count']} bearish / "
                f"{sentiment['neutral_count']} neutral, {sentiment['total_articles']} article\\(s\\)\\)"
            )
            for alert in news_json.get("high_impact_alerts", [])[:3]:
                lines.append(f"⚠️ {escape_markdown_v2(alert['title'])}")
            lines.append("")

        lines.append("*🧠 Reasoning*")
        for reason in decision_json["reason"]:
            lines.append(f"• {escape_markdown_v2(reason)}")
        lines.append("")

        lines.append("⚠️ *RISK WARNING*")
        lines.append(escape_markdown_v2(decision_json["disclaimer"]))
        lines.append(
            escape_markdown_v2(
                "Paper trading only. No live order is placed by this system. "
                "Markets carry risk of loss; do your own research."
            )
        )

        return "\n".join(lines)


class TelegramSender:
    """Optional: pushes a formatted message to a Telegram chat/channel via
    the Bot API. Requires BOT_TOKEN / CHANNEL_ID (see bootstrap.py, which
    reads these from the environment and constructs this class). Never
    called automatically by any other module in this codebase -- sending
    is always an explicit, opt-in action taken by the caller.
    """

    def __init__(self, bot_token: str, chat_id: str, timeout_seconds: float = 10.0) -> None:
        if not bot_token or not chat_id:
            raise ValueError("bot_token and chat_id are required to construct a TelegramSender.")
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._timeout = timeout_seconds

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self._bot_token}/{method}"
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise OSError(f"Telegram {method} returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise OSError(f"Telegram API unreachable: {exc.reason}") from exc
        return json.loads(body)

    @retry_on_network_failure(max_attempts=3)
    def send_to(
        self, chat_id: str | int, message: str, parse_mode: str | None = "MarkdownV2"
    ) -> dict[str, Any]:
        """Sends `message` to an arbitrary chat_id (not necessarily the
        configured CHANNEL_ID) -- used by TelegramCommandBot to reply to
        whichever chat a /start, /help, or /status command came from.
        `send()` below is the CHANNEL_ID-specific convenience built on
        top of this."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": message}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        result = self._post("sendMessage", payload)
        if not result.get("ok"):
            logger.error("Telegram sendMessage to %s failed: %s", chat_id, result)
        return result

    def send(self, message: str, parse_mode: str = "MarkdownV2") -> dict[str, Any]:
        """Sends `message` to the configured CHANNEL_ID. Retry/backoff is
        inherited from send_to() -- this method itself is not separately
        decorated, so a single logical send never retries twice over."""
        return self.send_to(self._chat_id, message, parse_mode)

    @retry_on_network_failure(max_attempts=3)
    def get_updates(self, offset: int | None = None, poll_timeout_seconds: int = 25) -> list[dict[str, Any]]:
        """Long-polls Telegram's getUpdates endpoint for new messages
        (used by TelegramCommandBot). A timed-out poll with no new
        messages is the NORMAL case (Telegram holds the connection open
        for up to poll_timeout_seconds and returns an empty result) and
        returns an empty list rather than being treated as a failure;
        only a genuine connection/HTTP error is retried."""
        params = {"timeout": str(poll_timeout_seconds)}
        if offset is not None:
            params["offset"] = str(offset)
        url = f"https://api.telegram.org/bot{self._bot_token}/getUpdates?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url)
        try:
            # Client-side socket timeout is padded beyond Telegram's own
            # server-side long-poll timeout so our timeout never fires
            # first and gets mistaken for a network failure.
            with urllib.request.urlopen(request, timeout=poll_timeout_seconds + 10) as response:
                body = response.read()
        except TimeoutError:
            return []
        except urllib.error.HTTPError as exc:
            raise OSError(f"Telegram getUpdates returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                return []
            raise OSError(f"Telegram API unreachable: {exc.reason}") from exc

        payload = json.loads(body)
        if not payload.get("ok"):
            raise OSError(f"Telegram getUpdates error: {payload}")
        return payload.get("result", [])

    @retry_on_network_failure(max_attempts=3)
    def get_me(self) -> dict[str, Any]:
        """Calls Telegram's getMe endpoint to confirm BOT_TOKEN is valid
        and return the bot's own identity (used by verify_admin_status
        and the startup connection test)."""
        url = f"https://api.telegram.org/bot{self._bot_token}/getMe"
        request = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise OSError(f"Telegram getMe returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise OSError(f"Telegram API unreachable: {exc.reason}") from exc
        return json.loads(body)

    @retry_on_network_failure(max_attempts=3)
    def _get_chat_member(self, user_id: int) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self._bot_token}/getChatMember"
        params = urllib.parse.urlencode({"chat_id": self._chat_id, "user_id": user_id})
        request = urllib.request.Request(f"{url}?{params}")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise OSError(f"Telegram getChatMember returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise OSError(f"Telegram API unreachable: {exc.reason}") from exc
        return json.loads(body)

    def verify_admin_status(self) -> dict[str, Any]:
        """Confirms the bot is an administrator (or creator) of CHANNEL_ID
        -- Telegram channels require this before a bot can post to them.
        Never raises: network/API failures are captured in the returned
        dict's "ok"/"error" fields so a startup check can log a clear
        pass/fail without crashing the whole process on a transient
        network hiccup during startup.
        """
        try:
            me = self.get_me()
            if not me.get("ok"):
                return {"ok": False, "is_admin": False, "error": f"getMe failed: {me}"}
            bot_id = me["result"]["id"]
            bot_username = me["result"].get("username", "unknown")

            member = self._get_chat_member(bot_id)
            if not member.get("ok"):
                return {
                    "ok": False,
                    "is_admin": False,
                    "bot_username": bot_username,
                    "error": f"getChatMember failed: {member}",
                }

            status = member["result"].get("status")
            is_admin = status in ("administrator", "creator")
            return {
                "ok": True,
                "is_admin": is_admin,
                "bot_username": bot_username,
                "status": status,
            }
        except Exception as exc:  # noqa: BLE001 - a startup check must never raise
            return {"ok": False, "is_admin": False, "error": str(exc)}

    def test_connection(self) -> dict[str, Any]:
        """One-shot startup test: confirms BOT_TOKEN is valid and the
        bot is an admin of CHANNEL_ID. Returns a summary dict suitable
        for a single clear log line; never raises."""
        admin_check = self.verify_admin_status()
        return {
            "telegram_configured": True,
            "bot_username": admin_check.get("bot_username"),
            "token_valid": admin_check.get("ok", False),
            "is_channel_admin": admin_check.get("is_admin", False),
            "detail": admin_check.get("error") or admin_check.get("status"),
        }
