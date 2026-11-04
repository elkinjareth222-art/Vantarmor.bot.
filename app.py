import asyncio
import os
import random
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
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


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.casefold())
    return "".join(char for char in text if unicodedata.category(char) != "Mn")


def duration_seconds(text: str) -> Optional[int]:
    """Accept 1m, 1h, 1d, 1mon, 1year, and Spanish equivalents."""
    value = normalize(text.strip())
    match = re.fullmatch(
        r"(\d+)\s*(m|min|mins|minuto|minutos|h|hr|hrs|hora|horas|d|dia|dias|mon|month|mes|meses|year|yr|ano|anos)",
        value,
    )
    if not match or int(match.group(1)) < 1:
        return None
    count, unit = int(match.group(1)), match.group(2)
    if unit in ("m", "min", "mins", "minuto", "minutos"):
        return count * 60
    if unit in ("h", "hr", "hrs", "hora", "horas"):
        return count * 3600
    if unit in ("d", "dia", "dias"):
        return count * 86400
    if unit in ("mon", "month", "mes", "meses"):
        return count * 30 * 86400
    return count * 365 * 86400


def duration_label(seconds: int) -> str:
    if seconds % (365 * 86400) == 0:
        return f"{seconds // (365 * 86400)} año(s)"
    if seconds % (30 * 86400) == 0:
        return f"{seconds // (30 * 86400)} mes(es)"
    if seconds % 86400 == 0:
        return f"{seconds // 86400} día(s)"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} hora(s)"
    return f"{seconds // 60} minuto(s)"


def elapsed_label(seconds: float) -> str:
    seconds = max(0, int(seconds))
    parts = []
    for name, amount in (("día", 86400), ("hora", 3600), ("minuto", 60), ("segundo", 1)):
        value, seconds = divmod(seconds, amount)
        if value:
            parts.append(f"{value} {name if value == 1 else name + 's'}")
    return ", ".join(parts) or "0 segundos"


async def audit(guild, title, description, color=discord.Color.orange()):
    channel = guild.get_channel(audit_channels.get(guild.id, 0))
    if channel is None:
        channel = discord.utils.get(guild.text_channels, name="auditoria-logs")
        if channel:
            audit_channels[guild.id] = channel.id
    if channel is None:
        return
    embed = discord.Embed(title=title, description=(description or "Sin detalles")[:4000], color=color, timestamp=datetime.now(timezone.utc))
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException) as error:
        print(f"No pude escribir en auditoria-logs: {error}")


async def audit_executor(guild, action, target_id):
    try:
        async for entry in guild.audit_logs(limit=20, action=action):
            if getattr(entry.target, "id", None) == target_id and (datetime.now(timezone.utc) - entry.created_at).total_seconds() <= 30:
                return entry.user, entry.reason
    except (discord.Forbidden, discord.HTTPException):
        pass
    return None, None


async def timeout_member(guild, actor, target, seconds):
    allowed_roles = {"aislamiento", "silenciador"}
    if not isinstance(actor, discord.Member) or not any(normalize(role.name) in allowed_roles for role in actor.roles):
        return "Solo miembros con el rol `Aislamiento` o `Silenciador` pueden usar este comando."
    if seconds is None or seconds < 60:
        return "Duración no válida. Usa `12m`, `1h`, `1d`, `1mon` o `1year` (mínimo 1m)."
    if seconds > 28 * 86400:
        return "Discord limita el aislamiento a 28 días. Usa una duración de `28d` o menos."
    if target.bot or target.id in (guild.owner_id, actor.id):
        return "No puedes aislar a bots, al propietario ni a ti mismo."
    if actor.id != guild.owner_id and target.top_role >= actor.top_role:
        return "No puedes aislar a alguien con un rol igual o superior al tuyo."
    bot_member = guild.me
    if bot_member is None or not bot_member.guild_permissions.moderate_members:
        return "Al bot le falta el permiso Moderar miembros."
    if target.top_role >= bot_member.top_role:
        return "El rol del miembro está por encima o al mismo nivel que el rol del bot."
    try:
        until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        await target.timeout(until, reason=f"Aislamiento aplicado por {actor}")
        return f"🔇 {target.mention} quedó aislado durante **{duration_label(seconds)}**."
    except discord.Forbidden:
        return "Discord rechazó el aislamiento. Revisa Moderar miembros y la jerarquía de roles."
    except discord.HTTPException:
        return "No pude aplicar el aislamiento por un error de Discord."


async def moderate_member(guild, actor, target, action):
    permission = "ban_members" if action == "ban" else "kick_members"
    if not getattr(actor.guild_permissions, permission, False):
        return "No tienes permiso para hacer eso."
    bot_member = guild.me
    if bot_member is None or not getattr(bot_member.guild_permissions, permission, False):
        return "Al bot le falta el permiso necesario."
    if target.id in (guild.owner_id, actor.id):
        return "No puedes moderar al propietario ni a ti mismo."
    if actor.id != guild.owner_id and target.top_role >= actor.top_role:
        return "No puedes moderar a alguien con un rol igual o superior al tuyo."
    if target.top_role >= bot_member.top_role:
        return "El rol del miembro está por encima o al mismo nivel que el rol del bot."
    try:
        if action == "ban":
            await guild.ban(target, reason=f"Acción solicitada por {actor}")
            return f"🔨 {target.mention} fue baneado."
        await guild.kick(target, reason=f"Acción solicitada por {actor}")
        return f"👢 {target.mention} fue expulsado."
    except (discord.Forbidden, discord.HTTPException) as error:
        return f"No pude completar la acción: {error}"


async def change_channel_lock(channel, actor, lock):
    guild = channel.guild
    bot_member = guild.me
    if not actor.guild_permissions.manage_channels:
        return "Necesitas el permiso Administrar canales."
    if bot_member is None or not bot_member.guild_permissions.manage_channels:
        return "Al bot le falta el permiso Administrar canales."
    if not isinstance(channel, discord.TextChannel):
        return "Este comando solo funciona en canales de texto."
    overwrite = channel.overwrites_for(guild.default_role)
    overwrite.send_messages = False if lock else None
    try:
        if overwrite.is_empty():
            await channel.set_permissions(guild.default_role, overwrite=None)
        else:
            await channel.set_permissions(guild.default_role, overwrite=overwrite, reason=f"Canal {'bloqueado' if lock else 'desbloqueado'} por {actor}")
    except (discord.Forbidden, discord.HTTPException):
        return "No pude cambiar los permisos del canal. Revisa los permisos del bot."
    return f"🔒 {channel.mention} quedó bloqueado." if lock else f"🔓 {channel.mention} quedó desbloqueado."


async def avatar_embed(user):
    try:
        full_user = await client.fetch_user(user.id)
    except discord.HTTPException:
        full_user = user
    embed = discord.Embed(title=f"Avatar y banner de {user}", color=discord.Color.blurple())
    embed.set_thumbnail(url=user.display_avatar.url)
    if getattr(full_user, "banner", None):
        embed.set_image(url=full_user.banner.url)
    else:
        embed.description = "Este usuario no tiene un banner configurado."
    return embed


def server_info_embed(guild):
    text_count = sum(isinstance(ch, discord.TextChannel) for ch in guild.channels)
    voice_count = sum(isinstance(ch, (discord.VoiceChannel, discord.StageChannel)) for ch in guild.channels)
    forum_count = sum(isinstance(ch, discord.ForumChannel) for ch in guild.channels)
    category_count = sum(isinstance(ch, discord.CategoryChannel) for ch in guild.channels)
    embed = discord.Embed(title=f"Información de {guild.name}", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Servidor", value=f"ID: `{guild.id}`\nPropietario: <@{guild.owner_id}>\nCreado: <t:{int(guild.created_at.timestamp())}:F>", inline=False)
    embed.add_field(name="Miembros", value=f"Total: **{guild.member_count or 0}**\nBots en caché: **{sum(member.bot for member in guild.members)}**", inline=True)
    embed.add_field(name="Canales", value=f"Texto: **{text_count}**\nVoz/escenario: **{voice_count}**\nForos: **{forum_count}**\nCategorías: **{category_count}**", inline=True)
    embed.add_field(name="Roles y recursos", value=f"Roles: **{len(guild.roles)}**\nEmojis: **{len(guild.emojis)}**\nStickers: **{len(guild.stickers)}**", inline=True)
    embed.add_field(name="Boosts", value=f"Nivel: **{guild.premium_tier}**\nBoosts: **{guild.premium_subscription_count or 0}**", inline=True)
    embed.add_field(name="Seguridad", value=f"Verificación: **{guild.verification_level}**\nFiltro de contenido: **{guild.explicit_content_filter}**\nMFA moderadores: **{'Sí' if guild.mfa_level else 'No'}**", inline=True)
    embed.add_field(name="Otros", value=f"Idioma: **{guild.preferred_locale}**\nDescripción: **{guild.description or 'Sin descripción'}**", inline=False)
    return embed


def user_info_embed(member):
    roles = [role.mention for role in reversed(member.roles) if not role.is_default()]
    permissions = [label for key, label in (("administrator", "Administrador"), ("manage_guild", "Gestionar servidor"), ("manage_messages", "Gestionar mensajes"), ("kick_members", "Expulsar miembros"), ("ban_members", "Banear miembros"), ("moderate_members", "Moderar miembros")) if getattr(member.guild_permissions, key, False)]
    embed = discord.Embed(title=f"Información de {member}", color=member.color if member.color.value else discord.Color.blurple())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Cuenta", value=f"ID: `{member.id}`\nBot: **{'Sí' if member.bot else 'No'}**\nCreada: <t:{int(member.created_at.timestamp())}:F>", inline=False)
    embed.add_field(name="En este servidor", value=f"Entró: {f'<t:{int(member.joined_at.timestamp())}:F>' if member.joined_at else 'Desconocido'}\nApodo: **{member.nick or 'Ninguno'}**\nRol más alto: {member.top_role.mention}", inline=False)
    embed.add_field(name=f"Roles ({len(roles)})", value=", ".join(roles)[:1024] or "Ninguno", inline=False)
    embed.add_field(name="Permisos clave", value=", ".join(permissions) or "Sin permisos destacados", inline=False)
    if member.timed_out_until:
        embed.add_field(name="Aislamiento", value=f"Hasta <t:{int(member.timed_out_until.timestamp())}:F>", inline=False)
    return embed


@tree.command(name="ping", description="Comprueba si el bot responde", guild=GUILD)
async def ping(interaction):
    await interaction.response.send_message("¡Pong! ✅")


@tree.command(name="ayuda", description="Muestra comandos disponibles", guild=GUILD)
async def ayuda(interaction):
    await interaction.response.send_message("`/serverinfo`, `/infoserver`, `/userinfo`, `/setlog`, `/juego button`, `/time out`, `/timeout`, `/lock`, `/unlock`, `/purge`, `/afk`, `/avatar`, `/sorteo`, `/cookie`, `/número`, `/autorole`, `/ban`, `/kick`, `/invite`, `/anuncio`. Texto: `?servidor`, responde con `?info`, `?time out 12m`, `?lock`, `?unlock`, `?purge 16`.", ephemeral=True)


@tree.command(name="serverinfo", description="Muestra información del servidor", guild=GUILD)
async def serverinfo(interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    await interaction.response.send_message(embed=server_info_embed(interaction.guild))


@tree.command(name="infoserver", description="Alias de /serverinfo", guild=GUILD)
async def infoserver(interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    await interaction.response.send_message(embed=server_info_embed(interaction.guild))


@tree.command(name="userinfo", description="Muestra información de un miembro", guild=GUILD)
@app_commands.describe(usuario="Usuario cuya información quieres consultar")
async def userinfo(interaction, usuario: discord.Member):
    await interaction.response.send_message(embed=user_info_embed(usuario))


@tree.command(name="setlog", description="Crea/configura el canal privado de auditoría", guild=GUILD)
@app_commands.default_permissions(manage_guild=True)
async def setlog(interaction):
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Necesitas Administrar servidor.", ephemeral=True)
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
            channel = await guild.create_text_channel("auditoria-logs", overwrites=overwrites, reason=f"Auditoría configurada por {interaction.user}")
        except (discord.Forbidden, discord.HTTPException) as error:
            await interaction.response.send_message(f"No pude crear el canal: {error}", ephemeral=True)
            return
    audit_channels[guild.id] = channel.id
    await interaction.response.send_message(f"✅ Auditoría configurada en {channel.mention}.", ephemeral=True)
    await audit(guild, "Auditoría activada", f"Configurada por {interaction.user.mention}.", discord.Color.green())


@tree.command(name="afk", description="Activa tu estado AFK", guild=GUILD)
@app_commands.describe(motivo="Motivo opcional")
async def afk_command(interaction, motivo: Optional[str] = None):
    if interaction.guild_id is None:
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    afk_users[(interaction.guild_id, interaction.user.id)] = {"started": time.time(), "reason": motivo or "Sin motivo"}
    await interaction.response.send_message(f"{interaction.user.mention} está AFK. Motivo: {motivo or 'Sin motivo'}", allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="avatar", description="Muestra avatar y banner de un miembro", guild=GUILD)
@app_commands.describe(usuario="Miembro cuyo avatar quieres ver")
async def avatar_command(interaction, usuario: discord.Member):
    await interaction.response.send_message(embed=await avatar_embed(usuario))


class LuckView(discord.ui.View):
    def __init__(self, question):
        super().__init__(timeout=120)
        self.question = question
        self.used = False

    @discord.ui.button(label="🍀 Suerte", style=discord.ButtonStyle.success, custom_id="juego_suerte:button")
    async def luck(self, interaction, button):
        if self.used:
            await interaction.response.send_message("Este botón ya se usó.", ephemeral=True)
            return
        self.used = True
        button.disabled = True
        result = random.choice(("¡Sí!", "No esta vez."))
        await interaction.response.edit_message(content=f"🍀 {interaction.user.mention}, tu suerte dice: **{result}**\nPregunta: {self.question}", view=self, allowed_mentions=discord.AllowedMentions.none())


juego_group = app_commands.Group(name="juego", description="Juegos de suerte")


@juego_group.command(name="button", description="Crea un juego de suerte con botón")
@app_commands.describe(mensaje="Pregunta o mensaje del juego")
async def juego_button(interaction, mensaje: str):
    mensaje = mensaje.strip()
    if not mensaje or len(mensaje) > 500:
        await interaction.response.send_message("Escribe un mensaje de 1 a 500 caracteres.", ephemeral=True)
        return
    await interaction.response.send_message(f"Mira tu suerte en el botón **🍀 Suerte**: 50% sí y 50% no.\nPregunta: {mensaje}", view=LuckView(mensaje), allowed_mentions=discord.AllowedMentions.none())


tree.add_command(juego_group, guild=GUILD)


class GiveawayView(discord.ui.View):
    def __init__(self, prize, winner_count, seconds, channel):
        super().__init__(timeout=None)
        self.prize, self.winner_count, self.seconds, self.channel = prize, winner_count, seconds, channel
        self.entries, self.message, self.finished = set(), None, False
        self.lock = asyncio.Lock()

    def make_embed(self, finished=False, winners=None):
        embed = discord.Embed(title="🎉 Sorteo", description=f"**Premio:** {self.prize}", color=discord.Color.blurple())
        embed.add_field(name="Ganadores", value=str(self.winner_count), inline=True)
        embed.add_field(name="Participantes", value=str(len(self.entries)), inline=True)
        embed.add_field(name="Resultado" if finished else "Participa", value=(" ".join(f"<@{u}>" for u in winners) if winners else "Sin participantes") if finished else "Pulsa 🎁 Participar para entrar o salir.", inline=False)
        return embed

    @discord.ui.button(label="🎁 Participar", style=discord.ButtonStyle.primary, custom_id="sorteo:participar")
    async def enter(self, interaction, button):
        async with self.lock:
            if self.finished:
                await interaction.response.send_message("Este sorteo ya terminó.", ephemeral=True)
                return
            if interaction.user.id in self.entries:
                self.entries.remove(interaction.user.id)
                answer = "Saliste del sorteo."
            else:
                self.entries.add(interaction.user.id)
                answer = "¡Ya estás participando!"
            await interaction.response.send_message(answer, ephemeral=True)
            if self.message:
                await self.message.edit(embed=self.make_embed(), view=self)

    async def finish(self):
        await asyncio.sleep(self.seconds)
        async with self.lock:
            self.finished = True
            winners = random.sample(list(self.entries), min(self.winner_count, len(self.entries))) if self.entries else []
            for item in self.children:
                item.disabled = True
            if self.message:
                await self.message.edit(embed=self.make_embed(True, winners), view=self)
            if winners:
                await self.channel.send("🎉 Ganador(es): " + " ".join(f"<@{u}>" for u in winners), allowed_mentions=discord.AllowedMentions(users=True))


@tree.command(name="sorteo", description="Crea un sorteo en este canal", guild=GUILD)
@app_commands.describe(ganadores="Cantidad de ganadores (1 a 10)", duracion="Ejemplo 10m, 2h, 3d", premio="Premio opcional")
async def sorteo(interaction, ganadores: app_commands.Range[int, 1, 10], duracion: str, premio: Optional[str] = None):
    seconds = duration_seconds(duracion)
    if seconds is None or interaction.channel is None:
        await interaction.response.send_message("Duración inválida o canal no disponible. Usa `10m`, `2h`, `3d`.", ephemeral=True)
        return
    view = GiveawayView(premio.strip() if premio and premio.strip() else "Premio sorpresa", ganadores, seconds, interaction.channel)
    await interaction.response.send_message(embed=view.make_embed(), view=view)
    view.message = await interaction.original_response()
    asyncio.create_task(view.finish())


@tree.command(name="cookie", description="Juego de tocar la galleta", guild=GUILD)
async def cookie(interaction):
    view = CookieView()
    await interaction.response.send_message("🍪 ¡Toca la galleta primero!", view=view)


class CookieView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.finished = False
        self.lock = asyncio.Lock()

    @discord.ui.button(label="🍪", style=discord.ButtonStyle.success, custom_id="cookie_game:click")
    async def click(self, interaction, button):
        async with self.lock:
            if self.finished:
                await interaction.response.send_message("Alguien ya ganó.", ephemeral=True)
                return
            self.finished = True
            button.disabled = True
            await interaction.response.edit_message(content=f"🍪 {interaction.user.mention} tocó la galleta primero y ganó.", view=self, allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="número", description="Piensa un número para que los demás lo adivinen", guild=GUILD)
@app_commands.describe(numero="Número secreto, entre 1 y 200")
async def number_game(interaction, numero: app_commands.Range[int, 1, 200]):
    if interaction.guild_id is None or interaction.channel is None:
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    async with number_game_lock:
        if interaction.guild_id in active_number_games:
            await interaction.response.send_message("Ya hay una partida activa.", ephemeral=True)
            return
        active_number_games[interaction.guild_id] = {"number": numero, "author": interaction.user.id}
    await interaction.response.send_message("Partida iniciada; el número es secreto.", ephemeral=True)
    await interaction.channel.send(f"🎲 {interaction.user.mention} piensa en un número del 1 al 200. ¡Adivínenlo!", allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="autorole", description="Configura el rol de nuevos miembros", guild=GUILD)
@app_commands.default_permissions(manage_roles=True)
@app_commands.describe(rol="Rol que recibirán los nuevos miembros")
async def autorole(interaction, rol: discord.Role):
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("Necesitas Administrar roles.", ephemeral=True)
        return
    bot_member = guild.me
    if bot_member is None or not bot_member.guild_permissions.manage_roles or rol.is_default() or rol.managed or rol >= bot_member.top_role:
        await interaction.response.send_message("No puedo asignar ese rol; revisa permisos y jerarquía.", ephemeral=True)
        return
    autoroles[guild.id] = rol.id
    await interaction.response.send_message(f"✅ Autorole configurado: {rol.mention}.", allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="ban", description="Banea a un miembro", guild=GUILD)
@app_commands.default_permissions(ban_members=True)
@app_commands.describe(usuario="Miembro que quieres banear")
async def ban_command(interaction, usuario: discord.Member):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    await interaction.response.send_message(await moderate_member(interaction.guild, interaction.user, usuario, "ban"), allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="kick", description="Expulsa a un miembro", guild=GUILD)
@app_commands.default_permissions(kick_members=True)
@app_commands.describe(usuario="Miembro que quieres expulsar")
async def kick_command(interaction, usuario: discord.Member):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    await interaction.response.send_message(await moderate_member(interaction.guild, interaction.user, usuario, "kick"), allowed_mentions=discord.AllowedMentions.none())


async def apply_timeout_response(interaction, usuario, duracion):
    seconds = duration_seconds(duracion)
    result = await timeout_member(interaction.guild, interaction.user, usuario, seconds)
    await interaction.response.send_message(result, allowed_mentions=discord.AllowedMentions.none())
    if seconds and seconds <= 28 * 86400 and "quedó aislado" in result:
        await audit(interaction.guild, "Miembro aislado", f"**Miembro:** {usuario.mention}\n**Moderador:** {interaction.user.mention}\n**Duración:** {duracion}", discord.Color.red())


# Group constructor has no `guild` parameter in discord.py. Bind its guild on tree.add_command.
time_group = app_commands.Group(name="time", description="Comandos de aislamiento")


@time_group.command(name="out", description="Aísla temporalmente a un miembro")
@app_commands.describe(usuario="Miembro que se aislará", duracion="Duración como 12m, 1h, 1d, 1mon o 1year (máx. 28d)")
async def time_out_command(interaction, usuario: discord.Member, duracion: str):
    await apply_timeout_response(interaction, usuario, duracion)


tree.add_command(time_group, guild=GUILD)


@tree.command(name="timeout", description="Aísla temporalmente a un miembro", guild=GUILD)
@app_commands.describe(usuario="Miembro que se aislará", duracion="Duración como 12m, 1h o 1d (máx. 28d)")
async def timeout_command(interaction, usuario: discord.Member, duracion: str):
    await apply_timeout_response(interaction, usuario, duracion)


@tree.command(name="invite", description="Muestra las invitaciones atribuidas a un miembro", guild=GUILD)
@app_commands.describe(usuario="Miembro opcional")
async def invite_command(interaction, usuario: Optional[discord.Member] = None):
    target = usuario or interaction.user
    await interaction.response.send_message(f"📨 {target.mention} ha invitado a **{invite_counts.get((interaction.guild_id, target.id), 0)}** persona(s) desde que el bot está activo.", allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="lock", description="Bloquea el canal actual", guild=GUILD)
@app_commands.default_permissions(manage_channels=True)
async def lock_command(interaction):
    if interaction.guild and interaction.channel and isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(await change_channel_lock(interaction.channel, interaction.user, True))
    else:
        await interaction.response.send_message("Úsalo en un canal del servidor.", ephemeral=True)


@tree.command(name="unlock", description="Desbloquea el canal actual", guild=GUILD)
@app_commands.default_permissions(manage_channels=True)
async def unlock_command(interaction):
    if interaction.guild and interaction.channel and isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(await change_channel_lock(interaction.channel, interaction.user, False))
    else:
        await interaction.response.send_message("Úsalo en un canal del servidor.", ephemeral=True)


@tree.command(name="bloqueo_palabra", description="Añade una palabra al filtro", guild=GUILD)
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(palabra="Palabra o frase que se eliminará")
async def block_word(interaction, palabra: str):
    if interaction.guild_id is None or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("Necesitas Administrar mensajes.", ephemeral=True)
        return
    key = normalize(palabra.strip())
    if not key or len(palabra) > 100:
        await interaction.response.send_message("Escribe una palabra o frase de hasta 100 caracteres.", ephemeral=True)
        return
    blocked_words.setdefault(interaction.guild_id, set()).add(key)
    await interaction.response.send_message(f"✅ Se añadió `{palabra}` al filtro.", ephemeral=True)


@tree.command(name="desbloqueo_palabra", description="Quita una palabra del filtro", guild=GUILD)
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(palabra="Palabra o frase que desbloquearás")
async def unblock_word(interaction, palabra: str):
    if interaction.guild_id is None or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("Necesitas Administrar mensajes.", ephemeral=True)
        return
    words = blocked_words.setdefault(interaction.guild_id, set())
    key = normalize(palabra.strip())
    if key not in words:
        await interaction.response.send_message("Esa palabra no está bloqueada.", ephemeral=True)
        return
    words.remove(key)
    await interaction.response.send_message(f"✅ Se quitó `{palabra}` del filtro.", ephemeral=True)


@tree.command(name="purge", description="Elimina de 1 a 100 mensajes", guild=GUILD)
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(cantidad="Cantidad de mensajes, entre 1 y 100")
async def purge_command(interaction, cantidad: app_commands.Range[int, 1, 100]):
    if interaction.guild is None or interaction.channel is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Úsalo dentro de un canal del servidor.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.manage_messages or not interaction.guild.me.guild_permissions.manage_messages:
        await interaction.response.send_message("Tú y el bot necesitan Administrar mensajes.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = len(await interaction.channel.purge(limit=cantidad, bulk=False))
        await interaction.followup.send(f"🧹 Eliminé {deleted} mensaje(s).", ephemeral=True)
    except (discord.Forbidden, discord.HTTPException) as error:
        await interaction.followup.send(f"No pude eliminar mensajes: {error}", ephemeral=True)


@tree.command(name="anuncio", description="Envía un anuncio importante", guild=GUILD)
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(mensaje="Texto del anuncio")
async def anuncio(interaction, mensaje: str):
    text = f"📢 **ANUNCIO IMPORTANTE**\n{mensaje.strip()}"
    if not mensaje.strip() or len(text) > 2000:
        await interaction.response.send_message("Escribe un anuncio de hasta 2000 caracteres.", ephemeral=True)
        return
    await interaction.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())


@client.event
async def on_message(message):
    if message.author.bot or message.guild is None:
        return
    content = message.content.strip()
    parts = content.split(maxsplit=2)
    command = parts[0].lower() if parts else ""

    if content.casefold() == "?servidor":
        await message.channel.send(embed=server_info_embed(message.guild))
        return

    if content.casefold() == "?info":
        reference = message.reference
        if not reference or not reference.message_id:
            await message.channel.send("Responde al mensaje de un miembro con `?info`.")
            return
        try:
            replied = reference.resolved
            if replied is None:
                channel = message.guild.get_channel(reference.channel_id) or await client.fetch_channel(reference.channel_id)
                replied = await channel.fetch_message(reference.message_id)
            member = message.guild.get_member(replied.author.id) or await message.guild.fetch_member(replied.author.id)
            await message.channel.send(embed=user_info_embed(member))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await message.channel.send("No pude encontrar al miembro respondido.")
        return

    if len(parts) == 3 and parts[0].casefold() == "?time" and parts[1].casefold() == "out":
        if not isinstance(message.author, discord.Member):
            return
        reference = message.reference
        if not reference or not reference.message_id:
            await message.channel.send("Responde al miembro que quieres aislar: `?time out 12m`.")
            return
        try:
            replied = reference.resolved
            if replied is None:
                channel = message.guild.get_channel(reference.channel_id) or await client.fetch_channel(reference.channel_id)
                replied = await channel.fetch_message(reference.message_id)
            target = message.guild.get_member(replied.author.id) or await message.guild.fetch_member(replied.author.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await message.channel.send("No pude encontrar al miembro respondido.")
            return
        seconds = duration_seconds(parts[2])
        result = await timeout_member(message.guild, message.author, target, seconds)
        await message.channel.send(result, allowed_mentions=discord.AllowedMentions.none())
        if seconds and seconds <= 28 * 86400 and "quedó aislado" in result:
            await audit(message.guild, "Miembro aislado", f"**Miembro:** {target.mention}\n**Moderador:** {message.author.mention}\n**Duración:** {parts[2]}", discord.Color.red())
        return

    if content.casefold() in ("?lock", "?unlock") and isinstance(message.author, discord.Member):
        result = await change_channel_lock(message.channel, message.author, content.casefold() == "?lock")
        await message.channel.send(result, delete_after=8)
        return

    if command == "?purge" and len(parts) == 2:
        if not isinstance(message.author, discord.Member) or not message.author.guild_permissions.manage_messages:
            await message.channel.send("Necesitas Administrar mensajes.", delete_after=8)
            return
        try:
            count = int(parts[1])
            if not 1 <= count <= 100:
                raise ValueError
            await message.delete()
            deleted = len(await message.channel.purge(limit=count, bulk=False))
            await message.channel.send(f"🧹 Eliminé {deleted} mensaje(s).", delete_after=6)
        except ValueError:
            await message.channel.send("Indica un número entre 1 y 100, por ejemplo `?purge 16`.", delete_after=8)
        except (discord.Forbidden, discord.HTTPException) as error:
            await message.channel.send(f"No pude eliminar mensajes: {error}", delete_after=8)
        return

    if content.casefold() in ("?ban", "?kick"):
        if not isinstance(message.author, discord.Member):
            return
        reference = message.reference
        if not reference or not reference.message_id:
            await message.channel.send("Responde al mensaje con `?ban` o `?kick`.")
            return
        try:
            replied = reference.resolved
            if replied is None:
                channel = message.guild.get_channel(reference.channel_id) or await client.fetch_channel(reference.channel_id)
                replied = await channel.fetch_message(reference.message_id)
            target = message.guild.get_member(replied.author.id) or await message.guild.fetch_member(replied.author.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await message.channel.send("No pude encontrar al miembro respondido.")
            return
        action = "ban" if content.casefold() == "?ban" else "kick"
        await message.channel.send(await moderate_member(message.guild, message.author, target, action), allowed_mentions=discord.AllowedMentions.none())
        return

    if command == "?afk":
        reason = parts[1] if len(parts) > 1 else "Sin motivo"
        afk_users[(message.guild.id, message.author.id)] = {"started": time.time(), "reason": reason}
        await message.channel.send(f"{message.author.mention} está AFK. Motivo: {reason}", allowed_mentions=discord.AllowedMentions.none())
        return

    state = afk_users.pop((message.guild.id, message.author.id), None)
    if state:
        await message.channel.send(f"{message.author.mention} salió de AFK tras {elapsed_label(time.time() - state['started'])}.", allowed_mentions=discord.AllowedMentions.none())

    if command == "?avatar":
        target = message.author
        if message.reference and message.reference.message_id:
            try:
                replied = message.reference.resolved or await message.channel.fetch_message(message.reference.message_id)
                target = replied.author
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        await message.channel.send(embed=await avatar_embed(target))
        return

    if isinstance(message.author, discord.Member) and not message.author.guild_permissions.administrator:
        normalized = normalize(content)
        for word in blocked_words.get(message.guild.id, set()):
            if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", normalized):
                try:
                    await message.delete()
                except (discord.Forbidden, discord.HTTPException):
                    pass
                return

    if content.isdecimal():
        async with number_game_lock:
            game = active_number_games.get(message.guild.id)
            if game and message.author.id != game["author"] and 1 <= int(content) <= 200 and int(content) == game["number"]:
                active_number_games.pop(message.guild.id, None)
            else:
                game = None
        if game:
            author = message.guild.get_member(game["author"])
            await message.channel.send(f"🎉 ¡Felicidades {message.author.mention}! {author.mention if author else 'El usuario'} pensaba el **{game['number']}**.", allowed_mentions=discord.AllowedMentions.users())
            return

    for user in message.mentions:
        state = afk_users.get((message.guild.id, user.id))
        if state:
            await message.channel.send(f"{user.mention} está AFK. Motivo: {state['reason']}", allowed_mentions=discord.AllowedMentions.none())


@client.event
async def on_interaction(interaction):
    if interaction.type != discord.InteractionType.application_command or interaction.guild is None:
        return
    data = interaction.data or {}
    details = f"**Usuario:** {interaction.user.mention} (`{interaction.user.id}`)\n**Comando:** `/{data.get('name', 'comando')}`\n**Canal:** {interaction.channel.mention if interaction.channel else 'desconocido'}"
    await audit(interaction.guild, "Comando slash usado", details, discord.Color.blue())


@client.event
async def on_message_delete(message):
    if message.guild is None or getattr(message.author, "bot", False):
        return
    executor, _ = await audit_executor(message.guild, discord.AuditLogAction.message_delete, message.author.id)
    await audit(message.guild, "Mensaje eliminado", f"**Autor:** {message.author.mention} (`{message.author.id}`)\n**Eliminado por:** {executor.mention if executor else 'No identificado'}\n**Canal:** {message.channel.mention}\n**Contenido:** {(message.content or '(vacío)')[:1500]}", discord.Color.red())


@client.event
async def on_message_edit(before, after):
    if before.guild and not before.author.bot and before.content != after.content:
        await audit(before.guild, "Mensaje editado", f"**Autor:** {before.author.mention}\n**Canal:** {before.channel.mention}\n**Antes:** {(before.content or '(vacío)')[:900]}\n**Después:** {(after.content or '(vacío)')[:900]}", discord.Color.gold())


@client.event
async def on_member_ban(guild, user):
    executor, reason = await audit_executor(guild, discord.AuditLogAction.ban, user.id)
    await audit(guild, "Miembro baneado", f"**Miembro:** {user} (`{user.id}`)\n**Baneado por:** {executor.mention if executor else 'No identificado'}\n**Razón:** {reason or 'No indicada'}", discord.Color.red())


@client.event
async def on_member_remove(member):
    executor, reason = await audit_executor(member.guild, discord.AuditLogAction.kick, member.id)
    await audit(member.guild, "Miembro expulsado o salió", f"**Miembro:** {member} (`{member.id}`)\n**Expulsado por:** {executor.mention if executor else 'No identificado (pudo salir por su cuenta)'}\n**Razón:** {reason or 'No indicada'}", discord.Color.orange())


@client.event
async def on_member_update(before, after):
    if before.nick != after.nick:
        await audit(after.guild, "Apodo actualizado", f"**Miembro:** {after.mention}\n**Antes:** {before.nick or 'Ninguno'}\n**Después:** {after.nick or 'Ninguno'}", discord.Color.gold())
    if before.roles != after.roles:
        added = [role.mention for role in after.roles if role not in before.roles and not role.is_default()]
        removed = [role.mention for role in before.roles if role not in after.roles and not role.is_default()]
        await audit(after.guild, "Roles actualizados", f"**Miembro:** {after.mention}\n**Añadidos:** {', '.join(added) or 'Ninguno'}\n**Quitados:** {', '.join(removed) or 'Ninguno'}", discord.Color.gold())


@client.event
async def on_member_join(member):
    role_id = autoroles.get(member.guild.id)
    role = member.guild.get_role(role_id) if role_id else None
    bot_member = member.guild.me
    if role and bot_member and bot_member.guild_permissions.manage_roles and role < bot_member.top_role:
        try:
            await member.add_roles(role, reason="Rol automático para nuevo miembro")
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"No se pudo asignar autorole: {error}")
    await audit(member.guild, "Miembro nuevo", f"**Miembro:** {member.mention} (`{member.id}`)", discord.Color.green())
    try:
        invites = await member.guild.invites()
        previous = invite_cache.get(member.guild.id, {})
        used = next((invite for invite in invites if (invite.uses or 0) > previous.get(invite.code, 0)), None)
        invite_cache[member.guild.id] = {invite.code: invite.uses or 0 for invite in invites}
        if used and used.inviter:
            key = (member.guild.id, used.inviter.id)
            invite_counts[key] = invite_counts.get(key, 0) + 1
    except (discord.Forbidden, discord.HTTPException):
        pass


@client.event
async def on_guild_channel_create(channel):
    await audit(channel.guild, "Canal creado", f"**Canal:** {channel.mention}\n**Tipo:** {channel.type}", discord.Color.green())


@client.event
async def on_guild_channel_delete(channel):
    await audit(channel.guild, "Canal eliminado", f"**Canal:** {channel.name}\n**ID:** `{channel.id}`", discord.Color.red())


@client.event
async def on_ready():
    print(f"Conectado como {client.user}")
    for guild in client.guilds:
        log_channel = discord.utils.get(guild.text_channels, name="auditoria-logs")
        if log_channel:
            audit_channels[guild.id] = log_channel.id
        try:
            invites = await guild.invites()
            invite_cache[guild.id] = {invite.code: invite.uses or 0 for invite in invites}
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"No pude cargar invitaciones: {error}")
    try:
        commands = await tree.sync(guild=GUILD)
        print(f"Comandos sincronizados: {len(commands)}")
    except Exception as error:
        print(f"Error al sincronizar comandos: {error}")


client.run(TOKEN)
