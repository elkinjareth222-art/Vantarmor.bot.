"""Reliable Railway entry point that injects social command registration into app.py."""

import ast
from pathlib import Path


APP_PATH = Path(__file__).with_name("app.py")
source = APP_PATH.read_text(encoding="utf-8")
lines = source.splitlines(keepends=True)

# Register in the app's own module namespace immediately before its existing
# client.run(TOKEN), so its regular on_ready tree.sync() publishes /kiss and
# /slap along with all existing application commands.
run_line = None
for index, line in enumerate(lines):
    if line.strip() == "client.run(TOKEN)":
        run_line = index
if run_line is None:
    raise RuntimeError("No se encontró client.run(TOKEN) en app.py; no se inicia para evitar perder comandos.")

injection = "\nfrom social_commands import install as _install_social_commands\n_install_social_commands(client, tree)\n\n"
lines.insert(run_line, injection)
patched_source = "".join(lines)
# Catch syntax errors before connecting the bot to Discord.
ast.parse(patched_source, filename=str(APP_PATH))
namespace = {"__name__": "__main__", "__file__": str(APP_PATH)}
exec(compile(patched_source, str(APP_PATH), "exec"), namespace)
