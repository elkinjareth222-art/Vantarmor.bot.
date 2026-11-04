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

# NUEVO: Cargar .env para pruebas locales
from dotenv import load_dotenv
load_dotenv()

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
# ACTUALIZADO: Agregamos ticket_panel
CONFIG_DEFAULTS = {
    "ticket_role": {}, "ticket_category": {}, "ticket_log": {}, "audit_log": {},
    "autorole": {}, "blocked_words": {}, "antispam": {}, "antilink": {}, "ticket_panel": {}
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

# ✅ NUEVA FUNCIÓN AGREGADA (arregla el crash AFK)
def elapsed_label(seconds: float) -> str:
    sec = int(seconds)
    if sec >= 86400:
        return f"{sec // 86400} día(s)"
    elif sec >= 3600:
        return f"{sec // 3600} hora(s)"
    elif sec >= 60:
        return f"{sec // 60} minuto(s)"
    return f"{sec} segundo(s)"


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
        await interaction.response.send_message
    
