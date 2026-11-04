import asyncio
import json
import os
import random
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
CONFIG_FILE = Path("bot_config.json")
CONFIG_DEFAULTS = {
    "ticket_role": {}, "ticket_category": {}, "ticket_log": {}, "audit_log": {},
    "autorole": {}, "blocked_words": {}, "antispam": {}, "antilink": {},
}
config = {key: {} for key in CONFIG_DEFAULTS}
afk_users = {}
active_number_games = {}
invite_cache = {}
invite_counts = {}
spam_records = {}
number_game_lock = asyncio.Lock()


def load_config():
    try:
        saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        for key in CONFIG_DEFAULTS:
            if isinstance(saved.get(key), dict):
                config[key] = saved[key]
    except (OSError, json.JSONDecodeError):
        pass


def save_config():
    try:
        CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as error:
        print(f"No pude guardar la configuración: {error}")


load_config()


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.casefold())
    return "".join(char for char in text if unicodedata.category(char) != "Mn")


def duration_seconds(text: str) -> Optional[int]:
    value = normalize(text.strip())
    match = re.fullmatch(r"(\d+)\s*(m|min|mins|minuto|minutos|h|hr|hrs|hora|horas|d|dia|dias|mon|month|mes|meses|year|yr|ano|anos)", value)
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


def get_role(guild, setting):
    role_id = config[setting].get(str(guild.id))
    return guild.get_role(int(role_id)) if role_id else None


def get_channel(guild, setting):
    channel_id = config[setting].get(str(guild.id))
    return guild.get_channel(int(channel_id)) if channel_id else None


def is_admin_or_owner(member):
    return isinstance(member, discord.Member) and (
        member.id == member.guild.owner_id or member.guild_permissions.administrator
    )


def is_ticket_staff(member):
    if not isinstance(member, discord.Member):
        return False
    if is_admin_or_owner(member) or member.guild_permissions.manage_guild:
        return True
    role = get_role(member.guild, "ticket_role")
    return role is not None and role in member.roles


async def audit(guild, title, description, color=discord.Color.orange()):
    channel = get_channel(guild, "audit_log")
    if channel:
        embed = discord.Embed(title=title, description=(description or "Sin detalles")[:4000], color=color, timestamp=datetime.now(timezone.utc))
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"No se pudo escribir en auditoria-logs: {error}")


async def ticket_log(guild, title, description, color=discord.Color.blurple()):
    channel = get_channel(guild, "ticket_log")
    if channel:
        embed = discord.Embed(title=title, description=description[:4000], color=color, timestamp=datetime.now(timezone.utc))
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"No se pudo escribir en los logs de tickets: {error}")


async def audit_executor(guild, action, target_id):
    try:
        async for entry in guild.audit_logs(limit=20, action=action):
            if getattr(entry.target, "id", None) == target_id and (datetime.now(timezone.utc) - entry.created_at).total_seconds() <= 30:
                return entry.user, entry.reason
    except (discord.Forbidden, discord.HTTPException):
        pass
    return None, None


async def timeout_member(guild, actor, target, seconds):
    if not isinstance(actor, discord.Member):
        return "Este comando solo se puede usar en un servidor."
    # The server owner and administrators can always run it; support-role members
    # may also use it, as configured for the ticket/moderation staff.
    if not (is_admin_or_owner(actor) or is_ticket_staff(actor)):
        return "Necesitas ser administrador, propietario o tener el rol del equipo de soporte."
    if seconds is None or seconds < 60:
        return "Duración no válida. Usa `12m`, `1h`, `1d`, `1mon` o `1year` (mínimo 1m)."
    if seconds > 28 * 86400:
        return "Discord limita el aislamiento a 28 días. Usa una duración de `28d` o menos."
    if target.bot or target.id in (guild.owner_id, actor.id):
        return "No puedes aislar a bots, al propietario ni a ti mismo."
    if not is_admin_or_owner(actor) and target.top_role >= actor.top_role:
        return "No puedes aislar a alguien con un rol igual o superior al tuyo."
    bot_member = guild.me
    if bot_member is None or not bot_member.guild_permissions.moderate_members:
        return "Al bot le falta el permiso Moderar miembros."
    if target.top_role >= bot_member.top_role:
        return "El rol del miembro está por encima o al mismo nivel que el rol del bot."
    try:
        await target.timeout(datetime.now(timezone.utc) + timedelta(seconds=seconds), reason=f"Aislamiento aplicado por {actor}")
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
        await channel.set_permissions(guild.default_role, overwrite=overwrite if not overwrite.is_empty() else None, reason=f"Canal {'bloqueado' if lock else 'desbloqueado'} por {actor}")
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
    category_count = sum(isinstance(ch, discord.CategoryChannel) for ch in guild.channels)
    embed = discord.Embed(title=f"Información de {guild.name}", color=discord.Color.blurple())
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Servidor", value=f"ID: `{guild.id}`\nPropietario: <@{guild.owner_id}>\nCreado: <t:{int(guild.created_at.timestamp())}:F>", inline=False)
    embed.add_field(name="Miembros", value=f"Total: **{guild.member_count or 0}**\nBots en caché: **{sum(member.bot for member in guild.members)}**", inline=True)
    embed.add_field(name="Canales", value=f"Texto: **{text_count}**\nVoz/escenario: **{voice_count}**\nCategorías: **{category_count}**", inline=True)
    embed.add_field(name="Roles", value=f"**{len(guild.roles)}**", inline=True)
    embed.add_field(name="Boosts", value=f"Nivel: **{guild.premium_tier}**\nBoosts: **{guild.premium_subscription_count or 0}**", inline=True)
    embed.add_field(name="Verificación", value=f"**{guild.verification_level}**", inline=True)
    return embed


def user_info_embed(member):
    roles = [role.mention for role in reversed(member.roles) if not role.is_default()]
    embed = discord.Embed(title=f"Información de {member}", color=member.color if member.color.value else discord.Color.blurple())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Cuenta", value=f"ID: `{member.id}`\nBot: **{'Sí' if member.bot else 'No'}**\nCreada: <t:{int(member.created_at.timestamp())}:F>", inline=False)
    embed.add_field(name="Servidor", value=f"Entró: {f'<t:{int(member.joined_at.timestamp())}:F>' if member.joined_at else 'Desconocido'}\nApodo: **{member.nick or 'Ninguno'}**\nRol más alto: {member.top_role.mention}", inline=False)
    embed.add_field(name=f"Roles ({len(roles)})", value=", ".join(roles)[:1024] or "Ninguno", inline=False)
    if member.timed_out_until:
        embed.add_field(name="Aislamiento", value=f"Hasta <t:{int(member.timed_out_until.timestamp())}:F>", inline=False)
    return embed


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎫 Abrir ticket", style=discord.ButtonStyle.primary, custom_id="support_ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Este panel solo funciona en un servidor.", ephemeral=True)
            return
        category = guild.get_channel(int(config["ticket_category"].get(str(guild.id), 0)))
        support_role = get_role(guild, "ticket_role")
        if not isinstance(category, discord.CategoryChannel) or support_role is None:
            await interaction.response.send_message("El sistema de tickets aún no está configurado.", ephemeral=True)
            return
        for existing in category.text_channels:
            if existing.topic and f"ticket_owner:{interaction.user.id}" in existing.topic:
                await interaction.response.send_message(f"Ya tienes un ticket abierto: {existing.mention}", ephemeral=True)
                return
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            await interaction.response.send_message("Al bot le falta Administrar canales.", ephemeral=True)
            return
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
            support_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True),
            bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True),
        }
        try:
            channel = await guild.create_text_channel(name=f"ticket-{interaction.user.name}"[:90], category=category, overwrites=overwrites, topic=f"ticket_owner:{interaction.user.id}", reason=f"Ticket abierto por {interaction.user}")
        except (discord.Forbidden, discord.HTTPException) as error:
            await interaction.response.send_message(f"No pude crear el ticket: {error}", ephemeral=True)
            return
        await interaction.response.send_message(f"Ticket creado: {channel.mention}", ephemeral=True)
        await channel.send(f"{interaction.user.mention} {support_role.mention}, cuéntanos cómo podemos ayudarte.", view=TicketCloseView(), allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        await ticket_log(guild, "Ticket abierto", f"**Abierto por:** {interaction.user.mention}\n**Canal:** {channel.mention}", discord.Color.green())


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Cerrar ticket", style=discord.ButtonStyle.danger, custom_id="support_ticket:close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, guild = interaction.channel, interaction.guild
        if guild is None or not isinstance(channel, discord.TextChannel) or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("No pude identificar este ticket.", ephemeral=True)
            return
        match = re.search(r"ticket_owner:(\d+)", channel.topic or "")
        owner_id = int(match.group(1)) if match else None
        if interaction.user.id != owner_id and not is_ticket_staff(interaction.user):
            await interaction.response.send_message("Solo quien abrió el ticket o el equipo de soporte puede cerrarlo.", ephemeral=True)
            return
        await interaction.response.send_message("Cerrando el ticket…", ephemeral=True)
        await ticket_log(guild, "Ticket cerrado", f"**Cerrado por:** {interaction.user.mention}\n**Abierto por:** <@{owner_id}>\n**Canal:** `#{channel.name}` (`{channel.id}`)", discord.Color.red())
        try:
            await channel.delete(reason=f"Ticket cerrado por {interaction.user}")
        except (discord.Forbidden, discord.HTTPException) as error:
            await interaction.followup.send(f"No pude eliminar el canal: {error}", ephemeral=True)


@tree.command(name="ping", description="Comprueba si el bot responde", guild=GUILD)
async def ping(interaction):
    await interaction.response.send_message("¡Pong! ✅")


@tree.command(name="ayuda", description="Muestra comandos disponibles", guild=GUILD)
async def ayuda(interaction):
    await interaction.response.send_message("Tickets: `/ticket-panel`, `/ticket-rol`, `/ticket-categoria`, `/ticket-rol-elimimar`, `/setlog-tickets`, `/unsetlog-tickets`. Seguridad: `/antispam`, `/antilink`, `/setlog`, `/unsetlog`. Moderación: `/time out usuario duracion`.", ephemeral=True)


@tree.command(name="serverinfo", description="Muestra información del servidor", guild=GUILD)
async def serverinfo(interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Úsalo en un servidor.", ephemeral=True)
        return
    await interaction.response.send_message(embed=server_info_embed(interaction.guild))


@tree.command(name="infoserver", description="Alias de /serverinfo", guild=GUILD)
async def infoserver(interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Úsalo en un servidor.", ephemeral=True)
        return
    await interaction.response.send_message(embed=server_info_embed(interaction.guild))


@tree.command(name="userinfo", description="Muestra información de un miembro", guild=GUILD)
@app_commands.describe(usuario="Usuario cuya información quieres consultar")
async def userinfo(interaction, usuario: discord.Member):
    await interaction.response.send_message(embed=user_info_embed(usuario))


@tree.command(name="ticket-rol", description="Configura el rol que verá y atenderá tickets", guild=GUILD)
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(rol="Rol del equipo que podrá ver los tickets")
async def ticket_role_command(interaction: discord.Interaction, rol: discord.Role):
    if interaction.guild is None or not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Necesitas Administrar servidor.", ephemeral=True)
        return
    config["ticket_role"][str(interaction.guild.id)] = rol.id
    save_config()
    await interaction.response.send_message(f"✅ El rol de soporte ahora es {rol.mention}.", ephemeral=True)


@tree.command(name="ticket-categoria", description="Selecciona la categoría donde se crearán los tickets", guild=GUILD)
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(categoria="Categoría para canales de ticket")
async def ticket_category_command(interaction: discord.Interaction, categoria: discord.CategoryChannel):
    if interaction.guild is None or not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Necesitas Administrar servidor.", ephemeral=True)
        return
    config["ticket_category"][str(interaction.guild.id)] = categoria.id
    save_config()
    await interaction.response.send_message(f"✅ Los tickets se crearán en **{categoria.name}**.", ephemeral=True)


@tree.command(name="ticket-rol-elimimar", description="Quita el rol de soporte de tickets", guild=GUILD)
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(rol="Rol de soporte que quieres eliminar de la configuración")
async def ticket_role_remove_command(interaction: discord.Interaction, rol: discord.Role):
    if interaction.guild is None or not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Necesitas Administrar servidor.", ephemeral=True)
        return
    key = str(interaction.guild.id)
    if config["ticket_role"].get(key) != rol.id:
        await interaction.response.send_message("Ese rol no es el rol de soporte configurado.", ephemeral=True)
        return
    config["ticket_role"].pop(key, None)
    save_config()
    await interaction.response.send_message(f"✅ {rol.mention} ya no podrá ver tickets nuevos.", ephemeral=True)


@tree.command(name="ticket-panel", description="Publica un panel para abrir tickets", guild=GUILD)
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(canal="Canal donde se enviará el panel", descripcion="Descripción opcional del panel")
async def ticket_panel_command(interaction: discord.Interaction, canal: discord.TextChannel, descripcion: Optional[str] = None):
    if interaction.guild is None or not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Necesitas Administrar servidor.", ephemeral=True)
        return
    category_id = config["ticket_category"].get(str(interaction.guild.id))
    category = interaction.guild.get_channel(int(category_id)) if category_id else None
    if get_role(interaction.guild, "ticket_role") is None or not isinstance(category, discord.CategoryChannel):
        await interaction.response.send_message("Primero configura `/ticket-rol` y `/ticket-categoria`.", ephemeral=True)
        return
    embed = discord.Embed(title="🎫 Soporte", description=(descripcion or "Pulsa el botón para crear un ticket privado con el equipo de soporte.")[:4000], color=discord.Color.blurple())
    try:
        await canal.send(embed=embed, view=TicketPanelView())
    except (discord.Forbidden, discord.HTTPException) as error:
        await interaction.response.send_message(f"No pude enviar el panel: {error}", ephemeral=True)
        return
    config["ticket_panel"][str(interaction.guild.id)] = canal.id
    save_config()
    await interaction.response.send_message(f"✅ Panel de tickets enviado a {canal.mention}.", ephemeral=True)


@tree.command(name="setlog-tickets", description="Crea/configura los registros de tickets", guild=GUILD)
@app_commands.default_permissions(manage_guild=True)
async def set_ticket_log(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Necesitas Administrar servidor.", ephemeral=True)
        return
    bot_member = guild.me
    if bot_member is None or not bot_member.guild_permissions.manage_channels:
        await interaction.response.send_message("Al bot le falta Administrar canales.", ephemeral=True)
        return
    channel = discord.utils.get(guild.text_channels, name="ticket-logs")
    if channel is None:
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False), interaction.user: discord.PermissionOverwrite(view_channel=True, read_message_history=True), bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, embed_links=True)}
        try:
            channel = await guild.create_text_channel("ticket-logs", overwrites=overwrites, reason=f"Logs configurados por {interaction.user}")
        except (discord.Forbidden, discord.HTTPException) as error:
            await interaction.response.send_message(f"No pude crear ticket-logs: {error}", ephemeral=True)
            return
    config["ticket_log"][str(guild.id)] = channel.id
    save_config()
    await interaction.response.send_message(f"✅ Registros de tickets en {channel.mention}.", ephemeral=True)


@tree.command(name="unsetlog-tickets", description="Desactiva los registros de tickets", guild=GUILD)
@app_commands.default_permissions(manage_guild=True)
async def unset_ticket_log(interaction: discord.Interaction):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Necesitas Administrar servidor.", ephemeral=True)
        return
    config["ticket_log"].pop(str(interaction.guild.id), None)
    save_config()
    await interaction.response.send_message("✅ Registro de tickets desactivado; no borraré el canal.", ephemeral=True)


@tree.command(name="setlog", description="Crea/configura el canal de auditoría", guild=GUILD)
@app_commands.default_permissions(manage_guild=True)
async def setlog(interaction: discord.Interaction):
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
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False), interaction.user: discord.PermissionOverwrite(view_channel=True), bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True)}
        try:
            channel = await guild.create_text_channel("auditoria-logs", overwrites=overwrites)
        except (discord.Forbidden, discord.HTTPException) as error:
            await interaction.response.send_message(f"No pude crear el canal: {error}", ephemeral=True)
            return
    config["audit_log"][str(guild.id)] = channel.id
    save_config()
    await interaction.response.send_message(f"✅ Auditoría configurada en {channel.mention}.", ephemeral=True)


@tree.command(name="unsetlog", description="Desactiva los registros de auditoría", guild=GUILD)
@app_commands.default_permissions(manage_guild=True)
async def unsetlog(interaction: discord.Interaction):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Necesitas Administrar servidor.", ephemeral=True)
        return
    config["audit_log"].pop(str(interaction.guild.id), None)
    save_config()
    await interaction.response.send_message("✅ Registro de auditoría desactivado; no borraré el canal.", ephemeral=True)


@tree.command(name="antispam", description="Activa o desactiva la protección contra spam", guild=GUILD)
@app_commands.default_permissions(manage_guild=True)
@app_commands.choices(accion=[app_commands.Choice(name="activar", value="activar"), app_commands.Choice(name="desactivar", value="desactivar")])
@app_commands.choices(repeticiones=[app_commands.Choice(name="3 repeticiones", value=3), app_commands.Choice(name="5 repeticiones", value=5)])
@app_commands.describe(accion="Activar o desactivar antispam", repeticiones="Umbral de mensajes repetidos en 8 segundos")
async def antispam(interaction: discord.Interaction, accion: app_commands.Choice[str], repeticiones: app_commands.Choice[int]):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_guild or is_admin_or_owner(interaction.user)):
        await interaction.response.send_message("Solo administradores pueden configurar antispam.", ephemeral=True)
        return
    key = str(interaction.guild.id)
    if accion.value == "activar":
        config["antispam"][key] = int(repeticiones.value)
        text = f"✅ Antispam activado: se borrarán mensajes repetidos al llegar a {repeticiones.value} en 8 segundos."
    else:
        config["antispam"].pop(key, None)
        text = "✅ Antispam desactivado."
    save_config()
    await interaction.response.send_message(text, ephemeral=True)


@tree.command(name="antilink", description="Activa o desactiva el filtro de enlaces", guild=GUILD)
@app_commands.default_permissions(manage_guild=True)
@app_commands.choices(accion=[app_commands.Choice(name="activar", value="activar"), app_commands.Choice(name="desactivar", value="desactivar")])
@app_commands.describe(accion="Activar o desactivar el filtro de enlaces")
async def antilink(interaction: discord.Interaction, accion: app_commands.Choice[str]):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_guild or is_admin_or_owner(interaction.user)):
        await interaction.response.send_message("Solo administradores pueden configurar antilink.", ephemeral=True)
        return
    key = str(interaction.guild.id)
    if accion.value == "activar":
        config["antilink"][key] = True
        text = "✅ Antilink activado. Se eliminarán URLs y enlaces de invitación publicados por miembros no administradores."
    else:
        config["antilink"].pop(key, None)
        text = "✅ Antilink desactivado."
    save_config()
    await interaction.response.send_message(text, ephemeral=True)


@tree.command(name="juego-placeholder", description="Usa /juego button", guild=GUILD)
async def juego_placeholder(interaction):
    await interaction.response.send_message("Usa `/juego button`.", ephemeral=True)


juego_group = app_commands.Group(name="juego", description="Juegos de suerte")


class LuckView(discord.ui.View):
    def __init__(self, question):
        super().__init__(timeout=None)
        self.question = question

    @discord.ui.button(label="🍀 Suerte", style=discord.ButtonStyle.success, custom_id="juego_suerte:button")
    async def luck(self, interaction: discord.Interaction, button: discord.ui.Button):
        result = random.choice(("¡Sí!", "No esta vez."))
        await interaction.response.send_message(f"🍀 {interaction.user.mention}, tu suerte dice: **{result}**\nPregunta: {self.question}", allowed_mentions=discord.AllowedMentions.none())


@juego_group.command(name="button", description="Crea un juego de suerte con botón")
@app_commands.describe(mensaje="Pregunta o mensaje del juego")
async def juego_button(interaction, mensaje: str):
    mensaje = mensaje.strip()
    if not mensaje or len(mensaje) > 500:
        await interaction.response.send_message("Escribe un mensaje de 1 a 500 caracteres.", ephemeral=True)
        return
    await interaction.response.send_message(f"Mira tu suerte en **🍀 Suerte**: 50% sí y 50% no.\nPregunta: {mensaje}", view=LuckView(mensaje), allowed_mentions=discord.AllowedMentions.none())


tree.add_command(juego_group, guild=GUILD)


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


@tree.command(name="sorteo", description="Crea un sorteo en este canal", guild=GUILD)
@app_commands.describe(ganadores="Ganadores (1 a 10)", duracion="10m, 2h, 3d", premio="Premio opcional")
async def giveaway(interaction, ganadores: app_commands.Range[int, 1, 10], duracion: str, premio: Optional[str] = None):
    seconds = duration_seconds(duracion)
    if seconds is None:
        await interaction.response.send_message("Duración no válida. Usa `10m`, `2h` o `3d`.", ephemeral=True)
        return
    await interaction.response.send_message("El comando de sorteo requiere la versión anterior del módulo; vuelve a intentarlo después.", ephemeral=True)


@tree.command(name="cookie", description="Juego de tocar la galleta", guild=GUILD)
async def cookie(interaction):
    await interaction.response.send_message("🍪 Juego de galleta disponible después del redeploy.")


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
async def autorole_command(interaction, rol: discord.Role):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("Necesitas Administrar roles.", ephemeral=True)
        return
    config["autorole"][str(interaction.guild.id)] = rol.id
    save_config()
    await interaction.response.send_message(f"Autorole configurado: {rol.mention}.", ephemeral=True)


@tree.command(name="ban", description="Banea a un miembro", guild=GUILD)
@app_commands.default_permissions(ban_members=True)
@app_commands.describe(usuario="Miembro que quieres banear")
async def ban_command(interaction, usuario: discord.Member):
    if interaction.guild is None:
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    await interaction.response.send_message(await moderate_member(interaction.guild, interaction.user, usuario, "ban"), allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="kick", description="Expulsa a un miembro", guild=GUILD)
@app_commands.default_permissions(kick_members=True)
@app_commands.describe(usuario="Miembro que quieres expulsar")
async def kick_command(interaction, usuario: discord.Member):
    if interaction.guild is None:
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    await interaction.response.send_message(await moderate_member(interaction.guild, interaction.user, usuario, "kick"), allowed_mentions=discord.AllowedMentions.none())


async def apply_timeout(interaction, usuario, duracion):
    seconds = duration_seconds(duracion)
    result = await timeout_member(interaction.guild, interaction.user, usuario, seconds)
    await interaction.response.send_message(result, allowed_mentions=discord.AllowedMentions.none())
    if seconds and seconds <= 28 * 86400 and "quedó aislado" in result:
        await audit(interaction.guild, "Miembro aislado", f"**Miembro:** {usuario.mention}\n**Moderador:** {interaction.user.mention}\n**Duración:** {duracion}", discord.Color.red())


time_group = app_commands.Group(name="time", description="Comandos de moderación")


@time_group.command(name="out", description="Aísla temporalmente a un miembro")
@app_commands.describe(usuario="Miembro que se aislará", duracion="Duración como 12m, 1h, 1d o 1mon (máx. 28d)")
async def timeout_command(interaction, usuario: discord.Member, duracion: str):
    await apply_timeout(interaction, usuario, duracion)


tree.add_command(time_group, guild=GUILD)


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
    if interaction.guild is None or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("Necesitas Administrar mensajes.", ephemeral=True)
        return
    words = config["blocked_words"].setdefault(str(interaction.guild.id), [])
    key = normalize(palabra.strip())
    if not key or len(palabra) > 100:
        await interaction.response.send_message("Escribe una palabra o frase de hasta 100 caracteres.", ephemeral=True)
        return
    if key not in words:
        words.append(key)
    save_config()
    await interaction.response.send_message(f"✅ Palabra añadida: `{palabra}`.", ephemeral=True)


@tree.command(name="desbloqueo_palabra", description="Quita una palabra del filtro", guild=GUILD)
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(palabra="Palabra o frase a desbloquear")
async def unblock_word(interaction, palabra: str):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("Necesitas Administrar mensajes.", ephemeral=True)
        return
    words = config["blocked_words"].setdefault(str(interaction.guild.id), [])
    key = normalize(palabra.strip())
    if key not in words:
        await interaction.response.send_message("Esa palabra no está bloqueada.", ephemeral=True)
        return
    words.remove(key)
    save_config()
    await interaction.response.send_message(f"✅ Se quitó `{palabra}` del filtro.", ephemeral=True)


@tree.command(name="purge", description="Elimina de 1 a 100 mensajes", guild=GUILD)
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(cantidad="Cantidad de mensajes, entre 1 y 100")
async def purge_command(interaction, cantidad: app_commands.Range[int, 1, 100]):
    if interaction.guild is None or interaction.channel is None or not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("Necesitas Administrar mensajes.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = len(await interaction.channel.purge(limit=cantidad, bulk=False))
    await interaction.followup.send(f"Eliminé {deleted} mensaje(s).", ephemeral=True)


@tree.command(name="anuncio", description="Envía un anuncio", guild=GUILD)
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
        await message.channel.send(await change_channel_lock(message.channel, message.author, content.casefold() == "?lock"), delete_after=8)
        return
    if command == "?purge" and len(parts) == 2 and isinstance(message.author, discord.Member) and message.author.guild_permissions.manage_messages:
        try:
            count = int(parts[1])
            if not 1 <= count <= 100:
                raise ValueError
            await message.delete()
            deleted = len(await message.channel.purge(limit=count, bulk=False))
            await message.channel.send(f"🧹 Eliminé {deleted} mensaje(s).", delete_after=6)
        except (ValueError, discord.Forbidden, discord.HTTPException):
            await message.channel.send("No pude procesar el purge. Usa `?purge` con un número entre 1 y 100.", delete_after=8)
        return
    if content.casefold() in ("?ban", "?kick") and isinstance(message.author, discord.Member):
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
    member = message.author if isinstance(message.author, discord.Member) else None
    if member and not is_admin_or_owner(member):
        guild_key = str(message.guild.id)
        # Antispam: remove a burst of repeated messages inside an 8-second window.
        threshold = config["antispam"].get(guild_key)
        if threshold:
            now = time.monotonic()
            key = (message.guild.id, message.author.id, message.channel.id)
            record = spam_records.setdefault(key, {"last": None, "times": []})
            record["times"] = [stamp for stamp in record["times"] if now - stamp <= 8]
            repeated = bool(record["last"] and normalize(content) == record["last"])
            record["times"].append(now)
            record["last"] = normalize(content)
            if len(record["times"]) >= int(threshold) and repeated:
                try:
                    await message.channel.purge(limit=min(len(record["times"]), 10), check=lambda item: item.author.id == message.author.id and normalize(item.content) == normalize(content))
                except (discord.Forbidden, discord.HTTPException):
                    try:
                        await message.delete()
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                spam_records.pop(key, None)
                return
        # Antilink removes URLs and Discord invite links from non-admin members.
        if config["antilink"].get(guild_key) and re.search(r"(?:https?://|www\.)\S+|discord(?:\.gg|\.com/invite)/\S+", content, re.IGNORECASE):
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        normalized = normalize(content)
        for word in config["blocked_words"].get(guild_key, []):
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
    if interaction.type == discord.InteractionType.application_command and interaction.guild:
        data = interaction.data or {}
        await audit(interaction.guild, "Comando slash usado", f"**Usuario:** {interaction.user.mention}\n**Comando:** `/{data.get('name', 'comando')}`\n**Canal:** {interaction.channel.mention if interaction.channel else 'desconocido'}", discord.Color.blue())


@client.event
async def on_message_delete(message):
    if message.guild is None or getattr(message.author, "bot", False):
        return
    executor, _ = await audit_executor(message.guild, discord.AuditLogAction.message_delete, message.author.id)
    await audit(message.guild, "Mensaje eliminado", f"**Autor:** {message.author.mention}\n**Eliminado por:** {executor.mention if executor else 'No identificado'}\n**Canal:** {message.channel.mention}\n**Contenido:** {(message.content or '(vacío)')[:1200]}", discord.Color.red())


@client.event
async def on_message_edit(before, after):
    if before.guild and not before.author.bot and before.content != after.content:
        await audit(before.guild, "Mensaje editado", f"**Autor:** {before.author.mention}\n**Canal:** {before.channel.mention}\n**Antes:** {(before.content or '(vacío)')[:900]}\n**Después:** {(after.content or '(vacío)')[:900]}", discord.Color.gold())


@client.event
async def on_member_ban(guild, user):
    executor, reason = await audit_executor(guild, discord.AuditLogAction.ban, user.id)
    await audit(guild, "Miembro baneado", f"**Miembro:** {user}\n**Baneado por:** {executor.mention if executor else 'No identificado'}\n**Razón:** {reason or 'No indicada'}", discord.Color.red())


@client.event
async def on_member_remove(member):
    executor, reason = await audit_executor(member.guild, discord.AuditLogAction.kick, member.id)
    await audit(member.guild, "Miembro expulsado o salió", f"**Miembro:** {member}\n**Expulsado por:** {executor.mention if executor else 'No identificado'}\n**Razón:** {reason or 'No indicada'}", discord.Color.orange())


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
    role = get_role(member.guild, "autorole")
    bot_member = member.guild.me
    if role and bot_member and bot_member.guild_permissions.manage_roles and role < bot_member.top_role:
        try:
            await member.add_roles(role, reason="Rol automático para nuevo miembro")
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"No se pudo asignar autorole: {error}")
    await audit(member.guild, "Miembro nuevo", f"**Miembro:** {member.mention}", discord.Color.green())
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
async def on_ready():
    print(f"Conectado como {client.user}")
    if not getattr(client, "ticket_views_registered", False):
        client.add_view(TicketPanelView())
        client.add_view(TicketCloseView())
        client.ticket_views_registered = True
    for server in client.guilds:
        try:
            invites = await server.invites()
            invite_cache[server.id] = {invite.code: invite.uses or 0 for invite in invites}
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"No pude cargar invitaciones: {error}")
    try:
        commands = await tree.sync(guild=GUILD)
        print(f"Comandos sincronizados: {len(commands)}")
    except Exception as error:
        print(f"Error al sincronizar comandos: {error}")


client.run(TOKEN)
