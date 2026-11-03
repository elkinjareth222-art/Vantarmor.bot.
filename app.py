import os

import discord
from discord import app_commands


TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")

if not TOKEN:
    raise RuntimeError("Falta la variable DISCORD_TOKEN en Railway.")

if not GUILD_ID or not GUILD_ID.isdigit():
    raise RuntimeError("Falta DISCORD_GUILD_ID o no es un ID válido en Railway.")

guild = discord.Object(id=int(GUILD_ID))
client = discord.Client(intents=discord.Intents.default())
tree = app_commands.CommandTree(client)


@tree.command(name="ping", description="Comprueba si el bot responde", guild=guild)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("¡Pong! Bressh.Bot está activo ✅")


@tree.command(name="hola", description="Saluda al usuario", guild=guild)
async def hola(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"¡Hola, {interaction.user.mention}! 👋"
    )


@tree.command(name="ayuda", description="Muestra los comandos disponibles", guild=guild)
async def ayuda(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**Comandos de Bressh.Bot**\n"
        "• `/ping` — comprueba si el bot responde\n"
        "• `/hola` — saluda\n"
        "• `/ayuda` — muestra esta lista"
    )


@client.event
async def on_ready():
    print(f"Conectado como {client.user}")

    try:
        sincronizados = await tree.sync(guild=guild)
        print(f"Comandos sincronizados: {len(sincronizados)}")
    except Exception as error:
        print(f"Error al sincronizar comandos: {error}")


client.run(TOKEN)
