import asyncio
import os
import random
import re
import time
import unicodedata
from typing import Optional

import discord
from discord import app_commands


TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1559455132821753856

if not TOKEN:
    raise RuntimeError("Falta la variable DISCORD_TOKEN en Railway.")

guild = discord.Object(id=GUILD_ID)

# Para ?afk y para detectar menciones/mensajes, activa también
# Message Content Intent en Discord Developer Portal > Bot.
intents = discord.Intents.default()
intents.guild_messages = True
intents.message_content = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Los AFK y sorteos se guardan en memoria: se reinician si se reinicia el bot.
afk_users = {}


def formato_duracion(segundos):
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
            plural = nombre if valor == 1 else nombre + "s"
            resultado.append(f"{valor} {plural}")

    return ", ".join(resultado) if resultado else "0 segundos"


def analizar_duracion(texto):
    """Acepta, por ejemplo: 10 minutos, 2 horas, 3 días o 1 mes."""
    texto = unicodedata.normalize("NFD", texto.strip().lower())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")

    coincidencia = re.fullmatch(
        r"(\d+)\s*(meses?|minutos?|mins?|m|horas?|hrs?|h|dias?|d|segundos?|segs?|s)",
        texto,
    )
    if not coincidencia:
        return None

    cantidad = int(coincidencia.group(1))
    unidad = coincidencia.group(2)

    if cantidad < 1:
        return None
    if unidad.startswith("mes"):
        return cantidad * 30 * 24 * 60 * 60
    if unidad.startswith("d"):
        return cantidad * 24 * 60 * 60
    if unidad.startswith("h"):
        return cantidad * 60 * 60
    if unidad.startswith("m"):
        return cantidad * 60
    return cantidad


@tree.command(name="ping", description="Comprueba si el bot responde", guild=guild)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("¡Pong! ✅")


@tree.command(name="hola", description="Saluda al usuario", guild=guild)
async def hola(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"¡Hola, {interaction.user.mention}! 👋",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="ayuda", description="Muestra los comandos disponibles", guild=guild)
async def ayuda(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**Comandos disponibles**\n"
        "• `/ping` — comprueba el bot\n"
        "• `/hola` — saluda\n"
        "• `/afk` — activa tu estado AFK\n"
        "• `/avatar usuario` — muestra avatar y banner\n"
        "• `/sorteo` — crea un sorteo\n"
        "• `/anuncio mensaje` — publica un anuncio"
    )


async def activar_afk(guild_id, user, motivo):
    clave = (guild_id, user.id)
    afk_users[clave] = {
        "inicio": time.time(),
        "motivo": motivo or "Sin motivo",
    }


@tree.command(name="afk", description="Activa tu estado AFK", guild=guild)
@app_commands.describe(motivo="Motivo opcional, por ejemplo: durmiendo")
async def afk_slash(
    interaction: discord.Interaction,
    motivo: Optional[str] = None,
):
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "Este comando solo se puede usar en un servidor.",
            ephemeral=True,
        )
        return

    motivo_final = motivo.strip() if motivo and motivo.strip() else "Sin motivo"
    await activar_afk(interaction.guild_id, interaction.user, motivo_final)

    await interaction.response.send_message(
        f"{interaction.user.mention} está en modo AFK. Motivo: {motivo_final}",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="avatar", description="Muestra el avatar y banner de un miembro", guild=guild)
@app_commands.describe(usuario="Miembro cuyo avatar y banner quieres ver")
async def avatar_slash(
    interaction: discord.Interaction,
    usuario: discord.Member,
):
    await enviar_avatar(interaction, usuario)


async def enviar_avatar(interaction, usuario):
    try:
        usuario_completo = await client.fetch_user(usuario.id)
    except discord.HTTPException:
        usuario_completo = usuario

    embed = discord.Embed(
        title=f"Avatar y banner de {usuario.display_name}",
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=usuario.display_avatar.url)

    banner = getattr(usuario_completo, "banner", None)
    if banner:
        embed.set_image(url=banner.url)
    else:
        embed.description = "Este usuario no tiene un banner configurado."

    await interaction.response.send_message(embed=embed)


class SorteoView(discord.ui.View):
    def __init__(self, premio, cantidad_ganadores, duracion):
        super().__init__(timeout=None)
        self.premio = premio
        self.cantidad_ganadores = cantidad_ganadores
        self.duracion = duracion
        self.participantes = set()
        self.mensaje = None
        self.canal = None
        self.finalizado = False

    def crear_embed(self, finalizado=False, ganadores=None):
        embed = discord.Embed(
            title="🎉 Sorteo",
            description=f"**Premio:** {self.premio}",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Ganadores",
            value=str(self.cantidad_ganadores),
            inline=True,
        )
        embed.add_field(
            name="Participantes",
            value=str(len(self.participantes)),
            inline=True,
        )

        if finalizado:
            if ganadores:
                embed.add_field(
                    name="Resultado",
                    value=" ".join(f"<@{user_id}>" for user_id in ganadores),
                    inline=False,
                )
            else:
                embed.add_field(
                    name="Resultado",
                    value="No hubo participantes.",
                    inline=False,
                )
        else:
            embed.add_field(
                name="Cómo participar",
                value="Pulsa **🎁 Participar**. Pulsa otra vez para salir.",
                inline=False,
            )

        return embed

    @discord.ui.button(
        label="🎁 Participar",
        style=discord.ButtonStyle.primary,
        custom_id="sorteo:participar",
    )
    async def participar(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if self.finalizado:
            await interaction.response.send_message(
                "Este sorteo ya terminó.",
                ephemeral=True,
            )
            return

        user_id = interaction.user.id
        if user_id in self.participantes:
            self.participantes.remove(user_id)
            respuesta = "Saliste del sorteo."
        else:
            self.participantes.add(user_id)
            respuesta = "¡Ya estás participando!"

        await interaction.response.send_message(respuesta, ephemeral=True)

        if self.mensaje:
            await self.mensaje.edit(embed=self.crear_embed(), view=self)

    async def terminar(self):
        await asyncio.sleep(self.duracion)
        self.finalizado = True

        ganadores = []
        if self.participantes:
            ganadores = random.sample(
                list(self.participantes),
                k=min(self.cantidad_ganadores, len(self.participantes)),
            )

        for elemento in self.children:
            elemento.disabled = True

        if self.mensaje:
            await self.mensaje.edit(
                embed=self.crear_embed(finalizado=True, ganadores=ganadores),
                view=self,
            )

        if ganadores and self.canal:
            menciones = " ".join(f"<@{user_id}>" for user_id in ganadores)
            await self.canal.send(
                f"🎉 ¡Sorteo terminado! Ganador(es) de **{self.premio}**: {menciones}",
                allowed_mentions=discord.AllowedMentions(users=True),
            )


@tree.command(name="sorteo", description="Crea un sorteo en este canal", guild=guild)
@app_commands.describe(
    ganadores="Cantidad de ganadores, entre 1 y 10",
    duracion="Por ejemplo: 10 minutos, 2 horas, 3 días o 1 mes",
    premio="Premio del sorteo; opcional",
)
async def sorteo(
    interaction: discord.Interaction,
    ganadores: app_commands.Range[int, 1, 10],
    duracion: str,
    premio: Optional[str] = None,
):
    segundos = analizar_duracion(duracion)
    if segundos is None:
        await interaction.response.send_message(
            "Duración no válida. Usa, por ejemplo: `10 minutos`, `2 horas`, "
            "`3 días` o `1 mes`.",
            ephemeral=True,
        )
        return

    if interaction.channel is None:
        await interaction.response.send_message(
            "No pude encontrar este canal.",
            ephemeral=True,
        )
        return

    premio_final = premio.strip() if premio and premio.strip() else "Premio sorpresa"
    vista = SorteoView(premio_final, ganadores, segundos)
    vista.canal = interaction.channel

    await interaction.response.send_message(
        embed=vista.crear_embed(),
        view=vista,
    )
    vista.mensaje = await interaction.original_response()
    asyncio.create_task(vista.terminar())


@tree.command(name="anuncio", description="Envía un anuncio importante", guild=guild)
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(mensaje="Texto obligatorio del anuncio")
async def anuncio(interaction: discord.Interaction, mensaje: str):
    mensaje = mensaje.strip()
    if not mensaje:
        await interaction.response.send_message(
            "Debes escribir el mensaje del anuncio.",
            ephemeral=True,
        )
        return

    texto = f"📢 **ANUNCIO IMPORTANTE**\n{mensaje}"
    if len(texto) > 2000:
        await interaction.response.send_message(
            "El anuncio supera el límite de 2000 caracteres.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        texto,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    contenido = message.content.strip()
    partes = contenido.split(maxsplit=1)
    comando = partes[0].lower() if partes else ""

    # Comando de texto: ?afk o ?afk durmiendo
    if comando == "?afk":
        motivo = partes[1].strip() if len(partes) > 1 else "Sin motivo"
        await activar_afk(message.guild.id, message.author, motivo)
        await message.channel.send(
            f"{message.author.mention} está en modo AFK. Motivo: {motivo}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    # Si vuelve a escribir, termina su estado AFK.
    clave_autor = (message.guild.id, message.author.id)
    estado = afk_users.pop(clave_autor, None)

    if estado:
        tiempo = formato_duracion(time.time() - estado["inicio"])
        await message.channel.send(
            f"{message.author.mention} salió de su estado AFK "
            f"y llevaba {tiempo}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ?avatar muestra el autor del mensaje al que se respondió;
    # si no es una respuesta, muestra al usuario que escribió ?avatar.
    if comando == "?avatar":
        usuario_objetivo = message.author
        referencia = message.reference

        if referencia and referencia.message_id:
            try:
                mensaje_referido = referencia.resolved
                if mensaje_referido is None:
                    mensaje_referido = await message.channel.fetch_message(
                        referencia.message_id
                    )
                usuario_objetivo = mensaje_referido.author
            except (discord.HTTPException, discord.NotFound):
                pass

        try:
            usuario_completo = await client.fetch_user(usuario_objetivo.id)
        except discord.HTTPException:
            usuario_completo = usuario_objetivo

        embed = discord.Embed(
            title=f"Avatar y banner de {usuario_objetivo.display_name}",
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=usuario_objetivo.display_avatar.url)

        banner = getattr(usuario_completo, "banner", None)
        if banner:
            embed.set_image(url=banner.url)
        else:
            embed.description = "Este usuario no tiene un banner configurado."

        await message.channel.send(embed=embed)
        return

    # Avisa si mencionan a alguien que está AFK.
    usuarios_avisados = set()
    for usuario in message.mentions:
        clave_usuario = (message.guild.id, usuario.id)
        estado_afk = afk_users.get(clave_usuario)

        if estado_afk and usuario.id not in usuarios_avisados:
            usuarios_avisados.add(usuario.id)
            await message.channel.send(
                f"{usuario.mention} está en modo AFK. "
                f"Motivo: {estado_afk['motivo']}",
                allowed_mentions=discord.AllowedMentions.none(),
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
