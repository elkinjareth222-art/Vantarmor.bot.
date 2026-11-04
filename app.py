import asyncio
import os
import random
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands


TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1559455132821753856

if not TOKEN:
    raise RuntimeError("Falta la variable DISCORD_TOKEN en Railway.")

GUILD = discord.Object(id=GUILD_ID)

intents = discord.Intents.default()
intents.guild_messages = True
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

afk_users = {}
autoroles = {}
invite_cache = {}
invite_counts = {}
active_number_games = {}
blocked_words = {}
audit_channels = {}
number_game_lock = asyncio.Lock()


def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFD", texto.casefold())
    return "".join(c for c in texto if unicodedata.category(c) != "Mn")


def formato_duracion(segundos: float) -> str:
    segundos = max(0, int(segundos))
    unidades = [("día", 86400), ("hora", 3600), ("minuto", 60), ("segundo", 1)]
    partes = []
    for nombre, cantidad in unidades:
        valor, segundos = divmod(segundos, cantidad)
        if valor:
            partes.append(f"{valor} {nombre if valor == 1 else nombre + 's'}")
    return ", ".join(partes) if partes else "0 segundos"


def analizar_duracion(texto: str) -> Optional[int]:
    texto = normalizar(texto.strip())
    match = re.fullmatch(r"(\d+)\s*(meses?|minutos?|mins?|m|horas?|hrs?|h|dias?|d|segundos?|segs?|s)", texto)
    if not match or int(match.group(1)) < 1:
        return None
    cantidad, unidad = int(match.group(1)), match.group(2)
    if unidad.startswith("mes"):
        return cantidad * 30 * 86400
    if unidad.startswith("d"):
        return cantidad * 86400
    if unidad.startswith("h"):
        return cantidad * 3600
    if unidad.startswith("m"):
        return cantidad * 60
    return cantidad


async def activar_afk(guild_id, user, motivo):
    afk_users[(guild_id, user.id)] = {"inicio": time.time(), "motivo": motivo or "Sin motivo"}


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
        await guild.kick(miembro, reason=f"Acción solicitada por {actor}")
        return f"👢 {miembro.mention} fue expulsado."
    except discord.Forbidden:
        return "Discord rechazó la acción. Revisa permisos y jerarquía de roles."
    except discord.HTTPException:
        return "No pude completar la acción por un error de Discord."


async def cambiar_bloqueo_canal(channel, actor, bloquear):
    guild = channel.guild
    bot_member = guild.me
    if not actor.guild_permissions.manage_channels:
        return "Necesitas el permiso Administrar canales."
    if bot_member is None or not bot_member.guild_permissions.manage_channels:
        return "Al bot le falta el permiso Administrar canales."
    if not isinstance(channel, discord.TextChannel):
        return "Este comando solo funciona en canales de texto."

    overwrite = channel.overwrites_for(guild.default_role)
    overwrite.send_messages = False if bloquear else None

    try:
        if overwrite.is_empty():
            await channel.set_permissions(guild.default_role, overwrite=None)
        else:
            await channel.set_permissions(
                guild.default_role,
                overwrite=overwrite,
                reason=f"Canal {'bloqueado' if bloquear else 'desbloqueado'} por {actor}",
            )
    except discord.Forbidden:
        return "No pude cambiar los permisos del canal. Revisa los permisos del bot."
    except discord.HTTPException:
        return "Discord devolvió un error al cambiar los permisos del canal."

    return f"🔒 {channel.mention} quedó bloqueado." if bloquear else f"🔓 {channel.mention} quedó desbloqueado."


async def enviar_auditoria(guild, titulo, descripcion, color=discord.Color.orange()):
    channel_id = audit_channels.get(guild.id)
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel is None:
        channel = discord.utils.get(guild.text_channels, name="auditoria-logs")
        if channel:
            audit_channels[guild.id] = channel.id
    if channel is None:
        return

    embed = discord.Embed(
        title=titulo,
        description=(descripcion or "Sin detalles")[:4000],
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException) as error:
        print(f"No pude escribir en el canal de auditoría: {error}")


async def obtener_ejecutor(guild, action, target_id):
    try:
        async for entry in guild.audit_logs(limit=20, action=action):
            if getattr(entry.target, "id", None) == target_id:
                return entry.user, entry.reason
    except (discord.Forbidden, discord.HTTPException):
        pass
    return None, None


async def crear_embed_avatar(usuario):
    try:
        usuario_completo = await client.fetch_user(usuario.id)
    except discord.HTTPException:
        usuario_completo = usuario

    embed = discord.Embed(title=f"Avatar y banner de {usuario}", color=discord.Color.blurple())
    embed.set_thumbnail(url=usuario.display_avatar.url)

    banner = getattr(usuario_completo, "banner", None)
    if banner:
        embed.set_image(url=banner.url)
    else:
        embed.description = "Este usuario no tiene un banner configurado."

    return embed


@tree.command(name="ping", description="Comprueba si el bot responde", guild=GUILD)
async def ping(interaction):
    await interaction.response.send_message("¡Pong! ✅")


@tree.command(name="hola", description="Saluda al usuario", guild=GUILD)
async def hola(interaction):
    await interaction.response.send_message(
        f"¡Hola, {interaction.user.mention}! 👋",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="ayuda", description="Muestra los comandos disponibles", guild=GUILD)
async def ayuda(interaction):
    await interaction.response.send_message(
        "`/infoserver`, `/userinfo usuario`, `/setlog`, `/juego button mensaje`, `/lock`, `/unlock`, "
        "`/bloqueo_palabra`, `/desbloqueo_palabra`, `/purge`, `/afk`, `/avatar`, `/sorteo`, `/cookie`, "
        "`/número`, `/autorole`, `/ban`, `/kick`, `/invite`, `/anuncio`. Prefijos: `?afk`, `?avatar`, "
        "`?ban`, `?kick`, `?lock`, `?unlock`, `?purge 16`.",
        ephemeral=True,
    )


@tree.command(name="infoserver", description="Muestra información detallada del servidor", guild=GUILD)
async def infoserver(interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return

    text_channels = sum(isinstance(ch, discord.TextChannel) for ch in guild.channels)
    voice_channels = sum(isinstance(ch, (discord.VoiceChannel, discord.StageChannel)) for ch in guild.channels)
    forum_channels = sum(isinstance(ch, discord.ForumChannel) for ch in guild.channels)
    categories = sum(isinstance(ch, discord.CategoryChannel) for ch in guild.channels)

    embed = discord.Embed(
        title=f"Información de {guild.name}",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(
        name="Servidor",
        value=(
            f"ID: `{guild.id}`\n"
            f"Propietario: <@{guild.owner_id}>\n"
            f"Creado: <t:{int(guild.created_at.timestamp())}:F>"
        ),
        inline=False,
    )
    embed.add_field(
        name="Miembros",
        value=f"Total: **{guild.member_count or 0}**\nBots: **{sum(member.bot for member in guild.members)}**",
        inline=True,
    )
    embed.add_field(
        name="Canales",
        value=(
            f"Texto: **{text_channels}**\n"
            f"Voz/Escenario: **{voice_channels}**\n"
            f"Foros: **{forum_channels}**\n"
            f"Categorías: **{categories}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Roles y emojis",
        value=f"Roles: **{len(guild.roles)}**\nEmojis: **{len(guild.emojis)}**\nStickers: **{len(guild.stickers)}**",
        inline=True,
    )
    embed.add_field(
        name="Boosts",
        value=f"Nivel: **{guild.premium_tier}**\nBoosts: **{guild.premium_subscription_count or 0}**",
        inline=True,
    )
    embed.add_field(
        name="Seguridad",
        value=(
            f"Verificación: **{guild.verification_level}**\n"
            f"Filtro de contenido: **{guild.explicit_content_filter}**\n"
            f"MFA moderadores: **{'Sí' if guild.mfa_level else 'No'}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Otros",
        value=f"Idioma: **{guild.preferred_locale}**\nDescripción: **{guild.description or 'Sin descripción'}**",
        inline=False,
    )

    await interaction.response.send_message(embed=embed)


@tree.command(name="userinfo", description="Muestra información detallada de un miembro", guild=GUILD)
@app_commands.describe(usuario="Usuario cuya información quieres consultar")
async def userinfo(interaction, usuario: discord.Member):
    roles = [role.mention for role in reversed(usuario.roles) if not role.is_default()]
    permisos = []
    for clave, label in (
        ("administrator", "Administrador"),
        ("manage_guild", "Gestionar servidor"),
        ("manage_messages", "Gestionar mensajes"),
        ("kick_members", "Expulsar miembros"),
        ("ban_members", "Banear miembros"),
    ):
        if getattr(usuario.guild_permissions, clave, False):
            permisos.append(label)

    embed = discord.Embed(
        title=f"Información de {usuario}",
        color=usuario.color if usuario.color.value else discord.Color.blurple(),
    )
    embed.set_thumbnail(url=usuario.display_avatar.url)
    embed.add_field(
        name="Cuenta",
        value=(
            f"ID: `{usuario.id}`\n"
            f"Bot: **{'Sí' if usuario.bot else 'No'}**\n"
            f"Creada: <t:{int(usuario.created_at.timestamp())}:F>"
        ),
        inline=False,
    )
    embed.add_field(
        name="Servidor",
        value=(
            f"Entró: {f'<t:{int(usuario.joined_at.timestamp())}:F>' if usuario.joined_at else 'Desconocido'}\n"
            f"Apodo: **{usuario.nick or 'Ninguno'}**\n"
            f"Rol más alto: {usuario.top_role.mention}"
        ),
        inline=False,
    )
    embed.add_field(
        name=f"Roles ({len(roles)})",
        value=", ".join(roles)[:1024] or "Ninguno",
        inline=False,
    )
    embed.add_field(
        name="Permisos clave",
        value=", ".join(permisos) or "Sin permisos de moderación destacados",
        inline=False,
    )
    if usuario.timed_out_until:
        embed.add_field(
            name="Timeout",
            value=f"Hasta <t:{int(usuario.timed_out_until.timestamp())}:F>",
            inline=False,
        )

    await interaction.response.send_message(embed=embed)


@tree.command(name="setlog", description="Configura el canal de auditoría del servidor", guild=GUILD)
@app_commands.default_permissions(manage_guild=True)
async def setlog(interaction):
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Necesitas Administrar servidor para configurar la auditoría.", ephemeral=True)
        return

    bot_member = guild.me
    if bot_member is None or not bot_member.guild_permissions.manage_channels:
        await interaction.response.send_message("Al bot le falta Administrar canales.", ephemeral=True)
        return

    channel = discord.utils.get(guild.text_channels, name="auditoria-logs")
    if channel is None:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=True),
            bot_member: discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=True, embed_links=True),
        }
        try:
            channel = await guild.create_text_channel(
                "auditoria-logs",
                overwrites=overwrites,
                reason=f"Canal de auditoría configurado por {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.response.send_message("No pude crear el canal. Revisa permisos y jerarquía del bot.", ephemeral=True)
            return
        except discord.HTTPException as error:
            await interaction.response.send_message(f"Discord devolvió un error: {error}", ephemeral=True)
            return

    audit_channels[guild.id] = channel.id
    await interaction.response.send_message(f"✅ Auditoría configurada en {channel.mention}.", ephemeral=True)
    await enviar_auditoria(guild, "Auditoría activada", f"Configurada por {interaction.user.mention}.", discord.Color.green())


class SuerteView(discord.ui.View):
    def __init__(self, mensaje):
        super().__init__(timeout=120)
        self.mensaje = mensaje
        self.usado = False

    @discord.ui.button(label="🍀 Suerte", style=discord.ButtonStyle.success, custom_id="juego_suerte:button")
    async def suerte(self, interaction, button):
        if self.usado:
            await interaction.response.send_message("Este botón de suerte ya se usó.", ephemeral=True)
            return

        self.usado = True
        button.disabled = True
        resultado = random.choice(["¡Sí!", "No esta vez."])

        await interaction.response.edit_message(
            content=(
                f"🍀 {interaction.user.mention}, tu suerte dice: **{resultado}**\n"
                f"Pregunta: {self.mensaje}"
            ),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )


juego_group = app_commands.Group(name="juego", description="Juegos de suerte")


@juego_group.command(name="button", description="Crea un juego de suerte con un botón")
@app_commands.describe(mensaje="Pregunta o texto del juego")
async def juego_button(interaction, mensaje: str):
    mensaje = mensaje.strip()
    if not mensaje or len(mensaje) > 500:
        await interaction.response.send_message("Escribe un mensaje de 1 a 500 caracteres.", ephemeral=True)
        return

    view = SuerteView(mensaje)
    await interaction.response.send_message(
        f"Mira tu suerte en el botón **🍀 Suerte**. Tiene un 50% de sí y 50% de no.\nPregunta: {mensaje}",
        view=view,
        allowed_mentions=discord.AllowedMentions.none(),
    )


tree.add_command(juego_group)


@tree.command(name="afk", description="Activa tu estado AFK", guild=GUILD)
@app_commands.describe(motivo="Motivo opcional")
async def afk_slash(interaction, motivo: Optional[str] = None):
    if interaction.guild_id is None:
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    motivo_final = motivo.strip() if motivo and motivo.strip() else "Sin motivo"
    await activar_afk(interaction.guild_id, interaction.user, motivo_final)
    await interaction.response.send_message(
        f"{interaction.user.mention} está AFK. Motivo: {motivo_final}",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="avatar", description="Muestra avatar y banner de un miembro", guild=GUILD)
@app_commands.describe(usuario="Miembro cuyo avatar y banner quieres ver")
async def avatar_slash(interaction, usuario: discord.Member):
    await interaction.response.send_message(embed=await crear_embed_avatar(usuario))


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
        embed = discord.Embed(title="🎉 Sorteo", description=f"**Premio:** {self.premio}", color=discord.Color.blurple())
        embed.add_field(name="Ganadores", value=str(self.cantidad_ganadores), inline=True)
        embed.add_field(name="Participantes", value=str(len(self.participantes)), inline=True)

        if finalizado:
            if ganadores:
                embed.add_field(name="Resultado", value=" ".join(f"<@{user_id}>" for user_id in ganadores), inline=False)
            else:
                embed.add_field(name="Resultado", value="No hubo participantes.", inline=False)
        else:
            embed.add_field(name="Cómo participar", value="Pulsa **🎁 Participar**. Pulsalo otra vez para salir.", inline=False)

        return embed

    @discord.ui.button(label="🎁 Participar", style=discord.ButtonStyle.primary, custom_id="sorteo:participar")
    async def participar(self, interaction, button):
        async with self.lock:
            if self.finalizado:
                await interaction.response.send_message("Este sorteo ya terminó.", ephemeral=True)
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
            ganadores = random.sample(list(self.participantes), k=min(self.cantidad_ganadores, len(self.participantes)))

        for item in self.children:
            item.disabled = True

        if self.mensaje:
            await self.mensaje.edit(embed=self.crear_embed(finalizado=True, ganadores=ganadores), view=self)

        if ganadores and self.canal:
            await self.canal.send(
                f"🎉 ¡Sorteo terminado! Ganador(es) de **{self.premio}**: " + " ".join(f"<@{user_id}>" for user_id in ganadores),
                allowed_mentions=discord.AllowedMentions(users=True),
            )


@tree.command(name="sorteo", description="Crea un sorteo en este canal", guild=GUILD)
@app_commands.describe(
    ganadores="Cantidad de ganadores, entre 1 y 10",
    duracion="Ejemplo: 10 minutos, 2 horas, 3 días o 1 mes",
    premio="Premio opcional del sorteo",
)
async def sorteo(interaction, ganadores: app_commands.Range[int, 1, 10], duracion: str, premio: Optional[str] = None):
    segundos = analizar_duracion(duracion)
    if segundos is None:
        await interaction.response.send_message(
            "Duración no válida. Usa, por ejemplo: `10 minutos`, `2 horas`, `3 días` o `1 mes`.",
            ephemeral=True,
        )
        return

    if interaction.channel is None:
        await interaction.response.send_message("No pude encontrar este canal.", ephemeral=True)
        return

    premio_final = premio.strip() if premio and premio.strip() else "Premio sorpresa"
    vista = SorteoView(premio_final, ganadores, segundos)
    vista.canal = interaction.channel

    await interaction.response.send_message(embed=vista.crear_embed(), view=vista)
    vista.mensaje = await interaction.original_response()
    asyncio.create_task(vista.terminar())


class CookieGameView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.finished = False
        self.lock = asyncio.Lock()
        self.message = None

    @discord.ui.button(label="🍪", style=discord.ButtonStyle.success, custom_id="cookie_game:click")
    async def click_cookie(self, interaction, button):
        async with self.lock:
            if self.finished:
                await interaction.response.send_message("Alguien ya tocó la galleta y ganó.", ephemeral=True)
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
            await self.message.edit(content="⏰ Se acabó el tiempo: nadie tocó la galleta.", view=self)


@tree.command(name="cookie", description="Juego de la galleta", guild=GUILD)
async def cookie_game(interaction):
    vista = CookieGameView()
    await interaction.response.send_message("La galleta aparece en **5**…")
    mensaje = await interaction.original_response()
    vista.message = mensaje

    for cuenta in range(4, 0, -1):
        await asyncio.sleep(1)
        await mensaje.edit(content=f"La galleta aparece en **{cuenta}**…")

    await asyncio.sleep(1)
    await mensaje.edit(content="🍪 ¡Apareció la galleta! ¡Tócala primero!", view=vista)


@tree.command(name="número", description="Piensa un número para que los demás lo adivinen", guild=GUILD)
@app_commands.describe(numero="El número secreto, entre 1 y 200")
async def numero_game(interaction, numero: app_commands.Range[int, 1, 200]):
    if interaction.guild_id is None or interaction.channel is None:
        await interaction.response.send_message("Este comando solo se puede usar dentro de un servidor.", ephemeral=True)
        return

    if interaction.guild_id in active_number_games:
        await interaction.response.send_message("Ya hay una partida activa en este servidor. Espera a que alguien la adivine.", ephemeral=True)
        return

    active_number_games[interaction.guild_id] = {"numero": numero, "autor_id": interaction.user.id}
    await interaction.response.send_message("Partida iniciada. El número elegido no se mostrará en este mensaje.", ephemeral=True)
    await interaction.channel.send(
        f"🎲 {interaction.user.mention} está pensando en un número del 1 al 200. ¡Adivínenlo antes que los demás!",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="autorole", description="Configura el rol para los nuevos miembros", guild=GUILD)
@app_commands.default_permissions(manage_roles=True)
@app_commands.describe(rol="Rol que recibirán los nuevos miembros")
async def autorole_command(interaction, rol: discord.Role):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Este comando solo se puede usar en un servidor.", ephemeral=True)
        return

    actor = interaction.user
    bot_member = interaction.guild.me

    if not actor.guild_permissions.manage_roles:
        await interaction.response.send_message("Necesitas el permiso Administrar roles.", ephemeral=True)
        return

    if bot_member is None or not bot_member.guild_permissions.manage_roles:
        await interaction.response.send_message("Al bot le falta el permiso Administrar roles.", ephemeral=True)
        return

    if rol.is_default() or rol.managed:
        await interaction.response.send_message("Ese rol no se puede asignar automáticamente.", ephemeral=True)
        return

    if rol >= bot_member.top_role:
        await interaction.response.send_message("El rol del bot debe estar por encima del rol que quieres asignar.", ephemeral=True)
        return

    if actor.id != interaction.guild.owner_id and rol >= actor.top_role:
        await interaction.response.send_message("Solo puedes configurar roles que estén por debajo de tu rol más alto.", ephemeral=True)
        return

    autoroles[interaction.guild_id] = rol.id
    await interaction.response.send_message(
        f"✅ Los nuevos miembros recibirán el rol {rol.mention}.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="ban", description="Banea a un miembro del servidor", guild=GUILD)
@app_commands.default_permissions(ban_members=True)
@app_commands.describe(usuario="Miembro que quieres banear")
async def ban_command(interaction, usuario: discord.Member):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Este comando solo se puede usar en un servidor.", ephemeral=True)
        return

    resultado = await moderar_miembro(interaction.guild, interaction.user, usuario, "ban")
    await interaction.response.send_message(resultado, allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="kick", description="Expulsa a un miembro del servidor", guild=GUILD)
@app_commands.default_permissions(kick_members=True)
@app_commands.describe(usuario="Miembro que quieres expulsar")
async def kick_command(interaction, usuario: discord.Member):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Este comando solo se puede usar en un servidor.", ephemeral=True)
        return

    resultado = await moderar_miembro(interaction.guild, interaction.user, usuario, "kick")
    await interaction.response.send_message(resultado, allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="invite", description="Muestra cuántas invitaciones ha generado un miembro", guild=GUILD)
@app_commands.describe(usuario="Miembro que quieres consultar; si se omite, te consulta a ti")
async def invite_command(interaction, usuario: Optional[discord.Member] = None):
    if interaction.guild_id is None:
        await interaction.response.send_message("Este comando solo se puede usar en un servidor.", ephemeral=True)
        return

    objetivo = usuario or interaction.user
    total = invite_counts.get((interaction.guild_id, objetivo.id), 0)

    await interaction.response.send_message(
        f"📨 {objetivo.mention} ha invitado a **{total}** persona(s), contadas desde que el bot está activo.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="lock", description="Bloquea el canal actual", guild=GUILD)
@app_commands.default_permissions(manage_channels=True)
async def lock_command(interaction):
    if interaction.guild is None or interaction.channel is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Úsalo dentro de un canal del servidor.", ephemeral=True)
        return
    await interaction.response.send_message(await cambiar_bloqueo_canal(interaction.channel, interaction.user, True))


@tree.command(name="unlock", description="Desbloquea el canal actual", guild=GUILD)
@app_commands.default_permissions(manage_channels=True)
async def unlock_command(interaction):
    if interaction.guild is None or interaction.channel is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Úsalo dentro de un canal del servidor.", ephemeral=True)
        return
    await interaction.response.send_message(await cambiar_bloqueo_canal(interaction.channel, interaction.user, False))


@tree.command(name="bloqueo_palabra", description="Añade una palabra al filtro del servidor", guild=GUILD)
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(palabra="Palabra o frase que se eliminará de mensajes de miembros")
async def bloqueo_palabra(interaction, palabra: str):
    if interaction.guild_id is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Necesitas permisos de servidor para configurar el filtro.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("Necesitas Administrar mensajes para configurar el filtro.", ephemeral=True)
        return

    palabra = palabra.strip()
    if not palabra or len(palabra) > 100:
        await interaction.response.send_message("Escribe una palabra o frase de 1 a 100 caracteres.", ephemeral=True)
        return

    key = normalizar(palabra)
    words = blocked_words.setdefault(interaction.guild_id, set())
    if key in words:
        await interaction.response.send_message("Esa palabra ya está bloqueada.", ephemeral=True)
        return

    words.add(key)
    await interaction.response.send_message(f"✅ Se añadió `{palabra}` al filtro.", ephemeral=True)


@tree.command(name="desbloqueo_palabra", description="Quita una palabra del filtro", guild=GUILD)
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(palabra="Palabra o frase que quieres desbloquear")
async def desbloqueo_palabra(interaction, palabra: str):
    if interaction.guild_id is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Necesitas permisos de servidor para configurar el filtro.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("Necesitas Administrar mensajes para configurar el filtro.", ephemeral=True)
        return

    key = normalizar(palabra.strip())
    words = blocked_words.setdefault(interaction.guild_id, set())
    if key not in words:
        await interaction.response.send_message("Esa palabra no está en la lista de bloqueadas.", ephemeral=True)
        return

    words.remove(key)
    await interaction.response.send_message(f"✅ Se quitó `{palabra.strip()}` del filtro.", ephemeral=True)


async def purge_messages(channel, cantidad):
    return len(await channel.purge(limit=cantidad, bulk=False))


@tree.command(name="purge", description="Elimina de 1 a 100 mensajes recientes del canal", guild=GUILD)
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(cantidad="Cantidad de mensajes que se eliminarán (1 a 100)")
async def purge_command(interaction, cantidad: app_commands.Range[int, 1, 100]):
    if interaction.channel is None or interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Úsalo dentro de un canal del servidor.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.manage_messages or not interaction.guild.me.guild_permissions.manage_messages:
        await interaction.response.send_message("Tú y el bot necesitan Administrar mensajes.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        count = await purge_messages(interaction.channel, cantidad)
        await interaction.followup.send(f"🧹 Eliminé {count} mensaje(s).", ephemeral=True)
    except (discord.Forbidden, discord.HTTPException) as error:
        await interaction.followup.send(f"No pude eliminar los mensajes: {error}", ephemeral=True)


@tree.command(name="anuncio", description="Envía un anuncio importante", guild=GUILD)
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(mensaje="Texto obligatorio del anuncio")
async def anuncio(interaction, mensaje: str):
    mensaje = mensaje.strip()
    if not mensaje:
        await interaction.response.send_message("Debes escribir el mensaje del anuncio.", ephemeral=True)
        return

    texto = f"📢 **ANUNCIO IMPORTANTE**\n{mensaje}"
    if len(texto) > 2000:
        await interaction.response.send_message("El anuncio supera el límite de 2000 caracteres.", ephemeral=True)
        return

    await interaction.response.send_message(texto, allowed_mentions=discord.AllowedMentions.none())


@client.event
async def on_message(message):
    if message.author.bot or message.guild is None:
        return

    content = message.content.strip()
    partes = content.split(maxsplit=1)
    comando = partes[0].lower() if partes else ""

    if content.lower() in ("?lock", "?unlock"):
        if isinstance(message.author, discord.Member):
            resultado = await cambiar_bloqueo_canal(message.channel, message.author, content.lower() == "?lock")
            await message.channel.send(resultado, delete_after=8)
        return

    purge_count = None
    if comando == "?purge" and len(partes) == 2:
        try:
            purge_count = int(partes[1])
        except ValueError:
            purge_count = -1
    if purge_count is not None:
        if not isinstance(message.author, discord.Member) or not message.author.guild_permissions.manage_messages:
            await message.channel.send("Necesitas Administrar mensajes.", delete_after=8)
            return
        if not 1 <= purge_count <= 100:
            await message.channel.send("Indica una cantidad entre 1 y 100, por ejemplo `?purge 16`.", delete_after=8)
            return
        try:
            await message.delete()
            count = await purge_messages(message.channel, purge_count)
            await message.channel.send(f"🧹 Eliminé {count} mensaje(s).", delete_after=6)
        except (discord.Forbidden, discord.HTTPException) as error:
            await message.channel.send(f"No pude eliminar los mensajes: {error}", delete_after=8)
        return

    if content.lower() in ("?ban", "?kick"):
        if not isinstance(message.author, discord.Member):
            return
        referencia = message.reference
        if not referencia or not referencia.message_id:
            await message.channel.send("Responde al mensaje del miembro con `?ban` o `?kick`.")
            return

        try:
            respuesta = referencia.resolved
            if respuesta is None:
                canal_objetivo = message.guild.get_channel(referencia.channel_id) or await client.fetch_channel(referencia.channel_id)
                respuesta = await canal_objetivo.fetch_message(referencia.message_id)
            miembro_objetivo = message.guild.get_member(respuesta.author.id) or await message.guild.fetch_member(respuesta.author.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await message.channel.send("No pude encontrar al miembro del mensaje respondido.")
            return

        accion = "ban" if content.lower() == "?ban" else "kick"
        await message.channel.send(
            await moderar_miembro(message.guild, message.author, miembro_objetivo, accion),
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

    estado = afk_users.pop((message.guild.id, message.author.id), None)
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
                mensaje_referido = referencia.resolved or await message.channel.fetch_message(referencia.message_id)
                usuario_objetivo = mensaje_referido.author
            except (discord.HTTPException, discord.NotFound):
                pass

        await message.channel.send(embed=await crear_embed_avatar(usuario_objetivo))
        return

    if isinstance(message.author, discord.Member) and not message.author.guild_permissions.administrator:
        normalizado = normalizar(message.content)
        for word in blocked_words.get(message.guild.id, set()):
            if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", normalizado):
                try:
                    await message.delete()
                except (discord.Forbidden, discord.HTTPException):
                    pass
                return

    if content.isdigit():
        async with number_game_lock:
            partida = active_number_games.get(message.guild.id)
            if partida and message.author.id != partida["autor_id"] and 1 <= int(content) <= 200 and int(content) == partida["numero"]:
                active_number_games.pop(message.guild.id, None)
            else:
                partida = None
        if partida:
            autor = message.guild.get_member(partida["autor_id"])
            await message.channel.send(
                f"🎉 ¡Felicidades {message.author.mention}! {autor.mention if autor else 'El usuario'} estaba pensando el número **{partida['numero']}**.",
                allowed_mentions=discord.AllowedMentions.users(),
            )
            return

    for usuario in message.mentions:
        estado_afk = afk_users.get((message.guild.id, usuario.id))
        if estado_afk:
            await message.channel.send(
                f"{usuario.mention} está en modo AFK. Motivo: {estado_afk['motivo']}",
                allowed_mentions=discord.AllowedMentions.none(),
            )


@client.event
async def on_member_join(member):
    role_id = autoroles.get(member.guild.id)
    role = member.guild.get_role(role_id) if role_id else None
    bot_member = member.guild.me
    if role and bot_member and bot_member.guild_permissions.manage_roles and role < bot_member.top_role:
        try:
            await member.add_roles(role, reason="Rol automático para nuevo miembro")
        except (discord.Forbidden, discord.HTTPException):
            pass

    await enviar_auditoria(member.guild, "Miembro nuevo", f"**Miembro:** {member.mention} (`{member.id}`)", discord.Color.green())
    try:
        invites_actuales = await member.guild.invites()
        anteriores = invite_cache.get(member.guild.id, {})
        usada = next((invite for invite in invites_actuales if (invite.uses or 0) > anteriores.get(invite.code, 0)), None)
        invite_cache[member.guild.id] = {invite.code: invite.uses or 0 for invite in invites_actuales}
        if usada and usada.inviter:
            clave = (member.guild.id, usada.inviter.id)
            invite_counts[clave] = invite_counts.get(clave, 0) + 1
    except (discord.Forbidden, discord.HTTPException):
        pass


@client.event
async def on_message_delete(message):
    if message.guild is None or message.author.bot:
        return

    executor, _ = await obtener_ejecutor(message.guild, discord.AuditLogAction.message_delete, message.author.id)
    await enviar_auditoria(
        message.guild,
        "Mensaje eliminado",
        f"**Autor:** {message.author.mention} (`{message.author.id}`)\n**Canal:** {message.channel.mention}\n**Ejecutor:** {executor.mention if executor else 'No identificado'}\n**Contenido:** {(message.content or '(vacío)')[:1500]}",
        discord.Color.red(),
    )


@client.event
async def on_message_edit(before, after):
    if before.guild is None or before.author.bot or before.content == after.content:
        return
    await enviar_auditoria(
        before.guild,
        "Mensaje editado",
        f"**Autor:** {before.author.mention}\n**Canal:** {before.channel.mention}\n**Antes:** {(before.content or '(vacío)')[:1200]}\n**Después:** {(after.content or '(vacío)')[:1200]}",
        discord.Color.gold(),
    )


@client.event
async def on_member_ban(guild, user):
    executor, reason = await obtener_ejecutor(guild, discord.AuditLogAction.ban, user.id)
    await enviar_auditoria(
        guild,
        "Miembro baneado",
        f"**Miembro:** {user} (`{user.id}`)\n**Baneado por:** {executor.mention if executor else 'No identificado'}\n**Razón:** {reason or 'No indicada'}",
        discord.Color.red(),
    )


@client.event
async def on_member_remove(member):
    executor, reason = await obtener_ejecutor(member.guild, discord.AuditLogAction.kick, member.id)
    await enviar_auditoria(
        member.guild,
        "Miembro expulsado o salido",
        f"**Miembro:** {member.mention} (`{member.id}`)\n**Acción:** {executor.mention if executor else 'No se pudo identificar'}\n**Razón:** {reason or 'No indicada'}",
        discord.Color.orange(),
    )


@client.event
async def on_guild_channel_create(channel):
    await enviar_auditoria(channel.guild, "Canal creado", f"**Canal:** {channel.mention}\n**Tipo:** {channel.type}", discord.Color.green())


@client.event
async def on_guild_channel_delete(channel):
    await enviar_auditoria(channel.guild, "Canal eliminado", f"**Canal:** {channel.name}\n**ID:** `{channel.id}`", discord.Color.red())


@client.event
async def on_member_update(before, after):
    if before.nick != after.nick:
        await enviar_auditoria(
            after.guild,
            "Apodo actualizado",
            f"**Miembro:** {after.mention}\n**Antes:** {before.nick or 'Ninguno'}\n**Después:** {after.nick or 'Ninguno'}",
            discord.Color.gold(),
        )

    if before.roles != after.roles:
        añadidos = [role.mention for role in after.roles if role not in before.roles and not role.is_default()]
        quitados = [role.mention for role in before.roles if role not in after.roles and not role.is_default()]
        await enviar_auditoria(
            after.guild,
            "Roles actualizados",
            f"**Miembro:** {after.mention}\n**Añadidos:** {', '.join(añadidos) or 'Ninguno'}\n**Quitados:** {', '.join(quitados) or 'Ninguno'}",
            discord.Color.gold(),
        )


@client.event
async def on_ready():
    print(f"Conectado como {client.user}")
    for servidor in client.guilds:
        channel = discord.utils.get(servidor.text_channels, name="auditoria-logs")
        if channel:
            audit_channels[servidor.id] = channel.id

        try:
            invitaciones = await servidor.invites()
            invite_cache[servidor.id] = {invite.code: invite.uses or 0 for invite in invitaciones}
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"No pude cargar invitaciones de {servidor.id}: {error}")

    try:
        comandos = await tree.sync(guild=GUILD)
        print(f"Comandos sincronizados: {len(comandos)}")
    except Exception as error:
        print(f"Error al sincronizar comandos: {error}")


client.run(TOKEN)
