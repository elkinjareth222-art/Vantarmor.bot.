"""Railway entry point. Register all optional slash commands before app syncs."""

import runpy
import sys

import discord

_original_run = discord.Client.run


def _run_registering_commands(self, *args, **kwargs):
    app_globals = sys._getframe(1).f_globals
    command_tree = app_globals.get("tree")
    if command_tree is not None and not getattr(self, "_social_commands_installed", False):
        from social_commands import install

        install(self, command_tree)
    return _original_run(self, *args, **kwargs)


discord.Client.run = _run_registering_commands
runpy.run_path("app.py", run_name="__main__")
