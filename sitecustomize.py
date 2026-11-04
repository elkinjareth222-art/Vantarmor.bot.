"""Load optional command modules when Railway starts the bot with `python app.py`.

Python imports sitecustomize automatically during interpreter startup when this
file is on the application path. This hook lets the existing app register the
social commands before its normal on_ready command sync, without duplicating or
replacing the main bot implementation.
"""

import sys

import discord

_original_client_run = discord.Client.run


def _run_with_social_commands(self, *args, **kwargs):
    # Client.run is called at the bottom of app.py, after its client and command
    # tree have been constructed. Read that module's globals and register the
    # extra commands/listener before connecting to Discord and syncing commands.
    caller_globals = sys._getframe(1).f_globals
    command_tree = caller_globals.get("tree")
    if command_tree is not None and not getattr(self, "_social_commands_installed", False):
        from social_commands import install

        install(self, command_tree)
        self._social_commands_installed = True
    return _original_client_run(self, *args, **kwargs)


discord.Client.run = _run_with_social_commands
