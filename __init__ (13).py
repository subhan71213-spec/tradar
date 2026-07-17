"""telegram_bot.py — the interactive Telegram bot (/start, /help, /status).

Everything else Telegram-related in this codebase is ONE-WAY: agents
compute analysis, main.py formats it, and TelegramSender.send() pushes
it to CHANNEL_ID. This module is the other direction -- it listens for
incoming messages/commands sent TO the bot and replies to whichever
chat sent them.

Design note on why this uses stdlib long-polling (TelegramSender's
get_updates) instead of python-telegram-bot or aiogram: this project
has been deliberately zero-third-party-dependency at runtime since
Phase 1 (see requirements.txt's comment), specifically so it stays
simple to install and deploy and so every network call could be fully
verified offline in this codebase's own test/build environment. Adding
a bot framework would reintroduce exactly the kind of packaging risk
that previously broke this project's Render deployment (see
project_final_report.md / the render.yaml history). A polling loop
against three commands does not need a framework -- it needs
`getUpdates`, a dict dispatch, and `sendMessage`, all of which
TelegramSender already provides.

Commands:
    /start  -- welcome message, confirms the bot is alive
    /help   -- lists available commands
    /status -- live status: trading mode, tracked symbols, Telegram
               admin status, and the next scheduled analysis run

This module places no orders and executes no trades -- it only ever
reads incoming text and replies with information.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from titan_ai_trader.application.services.telegram_formatter import TelegramSender

logger = logging.getLogger(__name__)

StatusProvider = Callable[[], dict[str, Any]]

_HELP_TEXT = (
    "Titan AI Trader — available commands:\n\n"
    "/start — check that the bot is running\n"
    "/help — show this message\n"
    "/status — current trading mode, tracked symbols, and next scheduled run\n\n"
    "This bot only posts analysis and alerts. It never places trades — "
    "there is no live-broker connection anywhere in this project."
)

_START_TEXT = (
    "Titan AI Trader is online.\n"
    "Send /help to see available commands, or /status for a live status report."
)


class TelegramCommandBot:
    """Long-polls Telegram for incoming messages and replies to /start,
    /help, and /status. Runs concurrently with (not instead of) the
    analysis scheduler -- see main.py, which starts both via
    asyncio.gather.
    """

    def __init__(
        self,
        sender: TelegramSender,
        status_provider: StatusProvider,
        poll_timeout_seconds: int = 20,
        bot_logger: logging.Logger | None = None,
    ) -> None:
        self._sender = sender
        self._status_provider = status_provider
        self._poll_timeout_seconds = poll_timeout_seconds
        self._logger = bot_logger or logger
        self._stop_event = asyncio.Event()
        self._handlers: dict[str, Callable[[], str]] = {
            "/start": self._handle_start,
            "/help": self._handle_help,
            "/status": self._handle_status,
        }

    def stop(self) -> None:
        self._stop_event.set()

    # -- Command handlers ------------------------------------------------#
    def _handle_start(self) -> str:
        return _START_TEXT

    def _handle_help(self) -> str:
        return _HELP_TEXT

    def _handle_status(self) -> str:
        try:
            status = self._status_provider()
        except Exception as exc:  # noqa: BLE001 - a reply must never crash the bot
            self._logger.error("status_provider failed", exc_info=True)
            return f"Status check failed: {exc}"

        lines = ["Titan AI Trader — status", ""]
        for label, value in status.items():
            lines.append(f"{label}: {value}")
        return "\n".join(lines)

    # -- Update processing ------------------------------------------------#
    def _extract_command(self, update: dict[str, Any]) -> tuple[int, str] | None:
        message = update.get("message") or update.get("channel_post")
        if not message or "text" not in message or "chat" not in message:
            return None
        text = message["text"].strip()
        if not text.startswith("/"):
            return None
        # Strips a "@BotUsername" suffix (e.g. "/status@MyBot") and any
        # trailing arguments -- only the bare command word is dispatched.
        command = text.split()[0].split("@")[0].lower()
        chat_id = message["chat"]["id"]
        return chat_id, command

    def _process_update(self, update: dict[str, Any]) -> None:
        extracted = self._extract_command(update)
        if extracted is None:
            return
        chat_id, command = extracted

        handler = self._handlers.get(command)
        if handler is None:
            self._logger.info("Ignoring unrecognized command %r from chat %s", command, chat_id)
            return

        self._logger.info("Handling %s from chat %s", command, chat_id)
        reply_text = handler()
        try:
            self._sender.send_to(chat_id, reply_text, parse_mode=None)
        except Exception:
            self._logger.error("Failed to reply to %s for chat %s", command, chat_id, exc_info=True)

    def register_commands(self) -> None:
        """Registers the command list with Telegram's own UI (the "/"
        menu next to the message box). Best-effort: a failure here does
        not stop the bot from working, it just means the menu won't show
        command descriptions until this succeeds."""
        try:
            self._sender._post(  # noqa: SLF001 - intentional reuse of the shared POST helper
                "setMyCommands",
                {
                    "commands": [
                        {"command": "start", "description": "Check that the bot is running"},
                        {"command": "help", "description": "List available commands"},
                        {"command": "status", "description": "Live status report"},
                    ]
                },
            )
        except Exception:
            self._logger.warning("Could not register bot command menu with Telegram", exc_info=True)

    async def run_forever(self) -> None:
        """Runs until stop() is called. Each iteration performs one
        (blocking, off-the-event-loop) long-poll for new messages, then
        dispatches each one. A failed poll backs off with a short delay
        before retrying rather than busy-looping."""
        self._stop_event.clear()
        offset: int | None = None
        consecutive_failures = 0

        while not self._stop_event.is_set():
            loop = asyncio.get_running_loop()
            poll_task = asyncio.ensure_future(
                loop.run_in_executor(None, self._sender.get_updates, offset, self._poll_timeout_seconds)
            )
            stop_task = asyncio.ensure_future(self._stop_event.wait())
            done, pending = await asyncio.wait({poll_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()

            if stop_task in done:
                poll_task.cancel()
                break

            try:
                updates = poll_task.result()
                consecutive_failures = 0
            except Exception as exc:
                consecutive_failures += 1
                delay = min(2**consecutive_failures, 60)
                self._logger.warning(
                    "Telegram getUpdates failed (%s); retrying in %ds", exc, delay
                )
                await asyncio.sleep(delay)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                await loop.run_in_executor(None, self._process_update, update)
