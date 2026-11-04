import os
import time

import discord
from discord import app_commands


TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("1559455132821753856")

if not TOKEN:
    raise RuntimeError("Falta la variable DISCORD_TOKEN en Railway.")

if not GUILD_ID or not GUILD_ID.isdigit():
    raise RuntimeError("Falta DISCORD_GUILD_ID o no es un ID válido.")

guild = discord.Object(id=int(GUILD_ID))

intents = discord.Intents.default()
intents.guild_messages = True
intents.message_content = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Guarda los estados AFK en memoria.
# Se borran si el bot se reinicia.
afk_users = {}


def formato_duracion(segundos):
    """Convierte segundos a días, horas, minutos y segundos."""
    segundos = max(0, int(segundos))

    unidades = [
        ("día", 86400),
        ("hora", 3600),
        ("minuto", 60),
        ("segundo", 1),
    ]

    resultado = []

    for nombre, cantidad in unidades:
        valor, segundos = divmod(segundos, cantidad)

        if valor:
            nombre_plural = nombre if valor == 1 else nombre + "s"
            resultado.append(f"{valor} {nombre_plural}")

    return ", ".join(resultado) if resultado else "0 segundos"


@tree.command(
    name="afk",
    description="Activa tu estado AFK con un motivo opcional",
    guild=guild
)
@app_commands.describe(motivo="Motivo opcional por el que estarás AFK")
async def afk(
    interaction: discord.Interaction,
    motivo: str | None = None
):
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "Este comando solo se puede usar dentro de un servidor.",
            ephemeral=True
        )
        return

    clave = (interaction.guild_id, interaction.user.id)
    motivo_final = motivo.strip() if motivo and motivo.strip() else "Sin motivo"

    afk_users[clave] = {
        "inicio": time.time(),
        "motivo": motivo_final,
        "nombre": interaction.user.display_name,
    }

    await interaction.response.send_message(
        f"{interaction.user.mention} está en modo AFK. Motivo: {motivo_final}",
        allowed_mentions=discord.AllowedMentions.none()
    )


@tree.command(
    name="anuncio",
    description="Envía un anuncio importante en este canal",
    guild=guild
)
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(mensaje="Texto del anuncio")
async def anuncio(
    interaction: discord.Interaction,
    mensaje: str
):
    mensaje = mensaje.strip()

    if not mensaje:
        await interaction.response.send_message(
            "Debes escribir el mensaje del anuncio.",
            ephemeral=True
        )
        return

    texto = f"📢 **ANUNCIO IMPORTANTE**\n{mensaje}"

    if len(texto) > 2000:
        await interaction.response.send_message(
            "El anuncio es demasiado largo. Discord permite hasta 2000 caracteres.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        texto,
        allowed_mentions=discord.AllowedMentions.none()
    )


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    # Si una persona AFK vuelve a escribir, se elimina su estado.
    clave_autor = (message.guild.id, message.author.id)
    estado = afk_users.pop(clave_autor, None)

    if estado:
        duracion = formato_duracion(time.time() - estado["inicio"])

        await message.channel.send(
            f"{message.author.mention} salió de su estado AFK "
            f"y llevaba {duracion}.",
            allowed_mentions=discord.AllowedMentions.none()
        )

    # Si mencionan a alguien que está AFK, informa el motivo.
    mencionados = []

    for usuario in message.mentions:
        clave_usuario = (message.guild.id, usuario.id)
        estado_afk = afk_users.get(clave_usuario)

        if estado_afk and usuario.id not in mencionados:
            mencionados.append(usuario.id)
            await message.channel.send(
                f"{usuario.mention} está en modo AFK. "
                f"Motivo: {estado_afk['motivo']}",
                allowed_mentions=discord.AllowedMentions.none()
            )


@client.event
async def on_ready():
    print(f"Conectado como {client.user}")

    try:
        comandos = await tree.sync(guild=guild)
        print(f"Comandos sincronizados: {len(comandos)}")
    except Exception as error:
        print(f"Error al sincronizar comandos: {error}")


client.run(TOKEN)
