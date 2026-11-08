"""Hermes Ark — encrypted, versioned backups of your whole Hermes.

Registers:
  * ``hermes ark …``  terminal command (init, status, snap, list, diff)
  * ``/ark …``        in-session command on every surface (CLI, TUI, Desktop, Telegram, Discord…)
  * ``tool_execution`` middleware: checkpoint before memory/skill writes + health guard after
  * ``on_session_end`` hook: periodic full snapshot in the background, daily retention pruning
  * ``ark_checkpoint`` tool: the agent saves a snapshot before risky changes to Hermes itself

The storage engine lives in ``arkcore`` and has no Hermes imports, so it is unit-tested on its own.
"""

from __future__ import annotations

import logging

from . import auto, cli, settings

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    settings.bind(ctx)
    ctx.register_cli_command(
        name="ark",
        help="Encrypted, versioned backups of your Hermes (init, snap, list, diff)",
        setup_fn=cli.setup_parser,
        handler_fn=cli.run_cli,
        description="Hermes Ark: deduplicated, encrypted snapshots of memory, SOUL, skills, config, cron, sessions and profiles.",
    )
    ctx.register_command(
        "ark",
        handler=cli.handle_slash,
        description="Backups: status, snap, list, diff, show, restore, prune",
        args_hint="[status|snap|list|diff|show|restore|prune]",
    )
    ctx.register_middleware("tool_execution", auto.tool_execution)
    if hasattr(ctx, "on_unload"):                  # stop the watchdog when Hermes unloads the plugin
        ctx.on_unload(auto.stop_watchdog)
    ctx.register_hook("on_session_end", auto.on_session_end)
    ctx.register_tool(
        name="ark_checkpoint", toolset="ark", schema=auto.CHECKPOINT_SCHEMA, handler=auto.checkpoint_tool,
        check_fn=auto.checkpoint_available, emoji="🛡",
        description="Save a restorable snapshot of Hermes (memory, skills, config...) before risky changes.",
    )
