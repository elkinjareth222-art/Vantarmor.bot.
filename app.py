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

# Intents necesarios para:
# - detectar mensajes normales
# - detectar miembros nuevos
# - interpretar ?afk, ?avatar, ?ban, ?kick
intents = discord.Intents.default()
intents.guild_messages = True
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

afk_users = {}
active_number_games = {}
autoroles = {}
invite_cache = {}
invite_counts = {}


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


async def activar_afk(guild_id, user, motivo):
    clave = (guild_id, user.id)
    afk_users[clave] = {
        "inicio": time.time(),
        "motivo": motivo or "Sin motivo",
    }


async def moderar_miembro(guild, actor, miembro, accion):
    permiso = "ban_members" if accion == "ban" else "kick_members"

    if not getattr(actor.guild_permissions, permiso, False):
        return "No tienes permiso para hacer eso."

    bot_member = guild.me
    if bot_member is None or not getattr(bot_member.guild_permissions, permiso, False):
        return "Al bot le falta el permiso necesario."

    if miembro.id == guild.owner_id:
        return "No se puede moderar al propietario del servidor."

    if miembro.id == actor.id:
        return "No puedes usar este comando contigo mismo."

    if actor.id != guild.owner_id and miembro.top_role >= actor.top_role:
        return "No puedes moderar a alguien con un rol igual o superior al tuyo."

    if miembro.top_role >= bot_member.top_role:
        return "El rol del miembro está por encima o al mismo nivel que el rol del bot."

    try:
        if accion == "ban":
            await guild.ban(miembro, reason=f"Acción solicitada por {actor}")
            return f"🔨 {miembro.mention} fue baneado."
        else:
            await guild.kick(miembro, reason=f"Acción solicitada por {actor}")
            return f"👢 {miembro.mention} fue expulsado."
    except discord.Forbidden:
        return "Discord rechazó la acción. Revisa los permisos y la posición de los roles."
    except discord.HTTPException:
        return "No pude completar la acción por un error de Discord."


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
        "• `/avatar usuario` — muestra avatar y banner del usuario\n"
        "• `/sorteo ganadores duracion premio` — crea un sorteo\n"
        "• `/cookie` — juego de la galleta\n"
        "• `/número numero` — número secreto entre 1 y 200\n"
        "• `/autorole rol` — asigna un rol a nuevos miembros\n"
        "• `/ban usuario` — banea al miembro\n"
        "• `/kick usuario` — expulsa al miembro\n"
        "• `/invite [usuario]` — cuenta invitaciones\n"
        "• `/anuncio mensaje` — publica un anuncio"
    )


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


@tree.command(name="avatar", description="Muestra el avatar y banner de un miembro", guild=guild)
@app_commands.describe(usuario="Miembro cuyo avatar y banner quieres ver")
async def avatar_slash(
    interaction: discord.Interaction,
    usuario: discord.Member,
):
    await enviar_avatar(interaction, usuario)


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
        self.lock = asyncio.Lock()

    def crear_embed(self, finalizado=False, ganadores=None):
        embed = discord.Embed(
            title="🎉 Sorteo",
            description=f"**Premio:** {self.premio}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Ganadores", value=str(self.cantidad_ganadores), inline=True)
        embed.add_field(name="Participantes", value=str(len(self.participantes)), inline=True)

        if finalizado:
            if ganadores:
                embed.add_field(
                    name="Resultado",
                    value=" ".join(f"<@{user_id}>" for user_id in ganadores),
                    inline=False,
                )
            else:
                embed.add_field(name="Resultado", value="No hubo participantes.", inline=False)
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
        async with self.lock:
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
    duracion="Ejemplo: 10 minutos, 2 horas, 3 días o 1 mes",
    premio="Premio opcional del sorteo",
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
            "Duración no válida. Usa, por ejemplo: `10 minutos`, `2 horas`, `3 días` o `1 mes`.",
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


class CookieGameView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.finished = False
        self.lock = asyncio.Lock()
        self.message = None

    @discord.ui.button(
        label="🍪",
        style=discord.ButtonStyle.success,
        custom_id="cookie_game:click",
    )
    async def click_cookie(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        async with self.lock:
            if self.finished:
                await interaction.response.send_message(
                    "Alguien ya tocó la galleta y ganó.",
                    ephemeral=True,
                )
                return

            self.finished = True
            button.disabled = True

            await interaction.response.edit_message(
                content=f"🍪 {interaction.user.mention} tocó la galleta primero y ganó.",
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def on_timeout(self):
        if self.finished:
            return

        self.finished = True
        for item in self.children:
            item.disabled = True

        if self.message:
            await self.message.edit(
                content="⏰ Se acabó el tiempo: nadie tocó la galleta.",
                view=self,
            )


@tree.command(name="cookie", description="Juego de la galleta", guild=guild)
async def cookie_game(interaction: discord.Interaction):
    vista = CookieGameView()

    await interaction.response.send_message("La galleta aparece en **5**…")
    mensaje = await interaction.original_response()
    vista.message = mensaje

    for cuenta in range(4, 0, -1):
        await asyncio.sleep(1)
        await mensaje.edit(content=f"La galleta aparece en **{cuenta}**…")

    await asyncio.sleep(1)
    await mensaje.edit(
        content="🍪 ¡Apareció la galleta! ¡Tócala primero!",
        view=vista,
    )


@tree.command(
    name="número",
    description="Piensa un número para que los demás lo adivinen",
    guild=guild,
)
@app_commands.describe(numero="El número secreto, entre 1 y 200")
async def numero_game(
    interaction: discord.Interaction,
    numero: app_commands.Range[int, 1, 200],
):
    if interaction.guild_id is None or interaction.channel is None:
        await interaction.response.send_message(
            "Este comando solo se puede usar dentro de un servidor.",
            ephemeral=True,
        )
        return

    if interaction.guild_id in active_number_games:
        await interaction.response.send_message(
            "Ya hay una partida activa en este servidor. Espera a que alguien la adivine.",
            ephemeral=True,
        )
        return

    active_number_games[interaction.guild_id] = {
        "numero": numero,
        "autor_id": interaction.user.id,
    }

    await interaction.response.send_message(
        "Partida iniciada. El número elegido no se mostrará en este mensaje.",
        ephemeral=True,
    )

    await interaction.channel.send(
        f"🎲 {interaction.user.mention} está pensando en un número del 1 al 200. "
        "¡Adivínenlo antes que los demás!",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="autorole", description="Configura el rol para los nuevos miembros", guild=guild)
@app_commands.default_permissions(manage_roles=True)
@app_commands.describe(rol="Rol que recibirán los nuevos miembros")
async def autorole_command(
    interaction: discord.Interaction,
    rol: discord.Role,
):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Este comando solo se puede usar en un servidor.",
            ephemeral=True,
        )
        return

    actor = interaction.user
    bot_member = interaction.guild.me

    if not actor.guild_permissions.manage_roles:
        await interaction.response.send_message(
            "Necesitas el permiso Administrar roles.",
            ephemeral=True,
        )
        return

    if bot_member is None or not bot_member.guild_permissions.manage_roles:
        await interaction.response.send_message(
            "Al bot le falta el permiso Administrar roles.",
            ephemeral=True,
        )
        return

    if rol.is_default() or rol.managed:
        await interaction.response.send_message(
            "Ese rol no se puede asignar automáticamente.",
            ephemeral=True,
        )
        return

    if rol >= bot_member.top_role:
        await interaction.response.send_message(
            "El rol del bot debe estar por encima del rol que quieres asignar.",
            ephemeral=True,
        )
        return

    if actor.id != interaction.guild.owner_id and rol >= actor.top_role:
        await interaction.response.send_message(
            "Solo puedes configurar roles que estén por debajo de tu rol más alto.",
            ephemeral=True,
        )
        return

    autoroles[interaction.guild_id] = rol.id
    await interaction.response.send_message(
        f"✅ Los nuevos miembros recibirán el rol {rol.mention}.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="ban", description="Banea a un miembro del servidor", guild=guild)
@app_commands.default_permissions(ban_members=True)
@app_commands.describe(usuario="Miembro que quieres banear")
async def ban_command(
    interaction: discord.Interaction,
    usuario: discord.Member,
):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Este comando solo se puede usar en un servidor.",
            ephemeral=True,
        )
        return

    resultado = await moderar_miembro(
        interaction.guild,
        interaction.user,
        usuario,
        "ban",
    )
    await interaction.response.send_message(
        resultado,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="kick", description="Expulsa a un miembro del servidor", guild=guild)
@app_commands.default_permissions(kick_members=True)
@app_commands.describe(usuario="Miembro que quieres expulsar")
async def kick_command(
    interaction: discord.Interaction,
    usuario: discord.Member,
):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Este comando solo se puede usar en un servidor.",
            ephemeral=True,
        )
        return

    resultado = await moderar_miembro(
        interaction.guild,
        interaction.user,
        usuario,
        "kick",
    )
    await interaction.response.send_message(
        resultado,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="invite", description="Muestra cuántas invitaciones ha generado un miembro", guild=guild)
@app_commands.describe(usuario="Miembro que quieres consultar; si se omite, te consulta a ti")
async def invite_command(
    interaction: discord.Interaction,
    usuario: Optional[discord.Member] = None,
):
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "Este comando solo se puede usar en un servidor.",
            ephemeral=True,
        )
        return

    objetivo = usuario or interaction.user
    total = invite_counts.get((interaction.guild_id, objetivo.id), 0)

    await interaction.response.send_message(
        f"📨 {objetivo.mention} ha invitado a **{total}** persona(s), "
        "contadas desde que el bot está activo.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


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
async def on_member_join(member: discord.Member):
    guild_obj = member.guild
    role_id = autoroles.get(guild_obj.id)

    if role_id:
        role = guild_obj.get_role(role_id)
        bot_member = guild_obj.me

        if role and bot_member and bot_member.guild_permissions.manage_roles:
            try:
                if role < bot_member.top_role:
                    await member.add_roles(role, reason="Rol automático para nuevo miembro")
            except discord.Forbidden:
                print(f"No se pudo asignar el autorole en {guild_obj.id}")
            except discord.HTTPException as error:
                print(f"Error al asignar autorole: {error}")

    try:
        invitaciones_actuales = await guild_obj.invites()
    except discord.Forbidden:
        print(f"No puedo leer invitaciones de {guild_obj.id}.")
        return
    except discord.HTTPException as error:
        print(f"No pude consultar invitaciones: {error}")
        return

    anteriores = invite_cache.get(guild_obj.id, {})
    usada = None

    for invitacion in invitaciones_actuales:
        usos_actuales = invitacion.uses or 0
        usos_anteriores = anteriores.get(invitacion.code, 0)

        if usos_actuales > usos_anteriores:
            usada = invitacion
            break

    invite_cache[guild_obj.id] = {
        invitacion.code: invitacion.uses or 0
        for invitacion in invitaciones_actuales
    }

    if usada and usada.inviter:
        clave = (guild_obj.id, usada.inviter.id)
        invite_counts[clave] = invite_counts.get(clave, 0) + 1


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    contenido = message.content.strip()
    partes = contenido.split(maxsplit=1)
    comando = partes[0].lower() if partes else ""

    if contenido.lower() in ("?ban", "?kick"):
        referencia = message.reference

        if not referencia:
            await message.channel.send(
                "Usa este comando respondiendo al mensaje del miembro que quieres moderar."
            )
            return

        try:
            referencia_resuelta = referencia.resolved
            if referencia_resuelta is None:
                canal_objetivo = message.guild.get_channel(referencia.channel_id)
                if canal_objetivo is None:
                    canal_objetivo = await client.fetch_channel(referencia.channel_id)

                referencia_resuelta = await canal_objetivo.fetch_message(
                    referencia.message_id
                )

            miembro_objetivo = message.guild.get_member(
                referencia_resuelta.author.id
            )
            if miembro_objetivo is None:
                miembro_objetivo = await message.guild.fetch_member(
                    referencia_resuelta.author.id
                )

        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await message.channel.send(
                "No pude encontrar al miembro del mensaje al que respondiste."
            )
            return

        accion = "ban" if contenido.lower() == "?ban" else "kick"
        resultado = await moderar_miembro(
            message.guild,
            message.author,
            miembro_objetivo,
            accion,
        )
        await message.channel.send(
            resultado,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    if comando == "?afk":
        motivo = partes[1].strip() if len(partes) > 1 else "Sin motivo"
        await activar_afk(message.guild.id, message.author, motivo)
        await message.channel.send(
            f"{message.author.mention} está en modo AFK. Motivo: {motivo}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    clave_autor = (message.guild.id, message.author.id)
    estado = afk_users.pop(clave_autor, None)

    if estado:
        tiempo = formato_duracion(time.time() - estado["inicio"])
        await message.channel.send(
            f"{message.author.mention} salió de su estado AFK y llevaba {tiempo}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

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

    # JUEGO DE ADIVINAR EL NÚMERO
    partida = active_number_games.get(message.guild.id)
    if partida and message.content.isdigit():
        numero_adivinado = int(message.content)

        if (
            numero_adivinado >= 1
            and numero_adivinado <= 200
            and message.author.id != partida["autor_id"]
            and numero_adivinado == partida["numero"]
        ):
            autor = message.guild.get_member(partida["autor_id"])
            autor_mention = autor.mention if autor else "El usuario"

            active_number_games.pop(message.guild.id, None)

            await message.channel.send(
                f"🎉 ¡Felicidades {message.author.mention}! "
                f"{autor_mention} estaba pensando el número **{partida['numero']}**.",
                allowed_mentions=discord.AllowedMentions.users(),
            )
            return

    for usuario in message.mentions:
        clave_usuario = (message.guild.id, usuario.id)
        estado_afk = afk_users.get(clave_usuario)
        if estado_afk:
            await message.channel.send(
                f"{usuario.mention} está en modo AFK. Motivo: {estado_afk['motivo']}",
                allowed_mentions=discord.AllowedMentions.none(),
            )


@client.event
async def on_ready():
    print(f"Conectado como {client.user}")

    for servidor in client.guilds:
        try:
            invitaciones = await servidor.invites()
            invite_cache[servidor.id] = {
                invitacion.code: invitacion.uses or 0
                for invitacion in invitaciones
            }
        except discord.Forbidden:
            print(f"No puedo leer invitaciones de {servidor.id}.")
        except discord.HTTPException as error:
            print(f"Error al cargar invitaciones: {error}")

    try:
        comandos = await tree.sync(guild=guild)
        print(f"Comandos sincronizados: {len(comandos)}")
    except Exception as error:
        print(f"Error al sincronizar comandos: {error}")


client.run(TOKEN)
