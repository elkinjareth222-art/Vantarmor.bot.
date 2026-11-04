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
    await interaction.response.send_message(f"¡Hola, {interaction.user.mention}! 👋", allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="ayuda", description="Muestra los comandos disponibles", guild=GUILD)
async def ayuda(interaction):
    await interaction.response.send_message(
        "`/ping`, `/hola`, `/afk`, `/avatar`, `/sorteo`, `/cookie`, `/número`, `/autorole`, "
        "`/ban`, `/kick`, `/invite`, `/anuncio`, `/lock`, `/unlock`, `/bloqueo_palabra`, "
        "`/desbloqueo_palabra`, `/purge`. Prefijos: `?afk`, `?avatar`, `?ban`, `?kick`, `?lock`, `?unlock`, `?purge 16`.",
        ephemeral=True,
    )


@tree.command(name="afk", description="Activa tu estado AFK", guild=GUILD)
@app_commands.describe(motivo="Motivo opcional")
async def afk_slash(interaction, motivo: Optional[str] = None):
    if interaction.guild_id is None:
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    motivo = motivo.strip() if motivo and motivo.strip() else "Sin motivo"
    await activar_afk(interaction.guild_id, interaction.user, motivo)
    await interaction.response.send_message(f"{interaction.user.mention} está AFK. Motivo: {motivo}", allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="avatar", description="Muestra avatar y banner de un miembro", guild=GUILD)
@app_commands.describe(usuario="Miembro cuyo avatar y banner quieres ver")
async def avatar_slash(interaction, usuario: discord.Member):
    await interaction.response.send_message(embed=await crear_embed_avatar(usuario))


class SorteoView(discord.ui.View):
    def __init__(self, premio, cantidad_ganadores, duracion):
        super().__init__(timeout=None)
        self.premio, self.cantidad_ganadores, self.duracion = premio, cantidad_ganadores, duracion
        self.participantes, self.mensaje, self.canal = set(), None, None
        self.finalizado = False
        self.lock = asyncio.Lock()

    def crear_embed(self, finalizado=False, ganadores=None):
        embed = discord.Embed(title="🎉 Sorteo", description=f"**Premio:** {self.premio}", color=discord.Color.blurple())
        embed.add_field(name="Ganadores", value=str(self.cantidad_ganadores), inline=True)
        embed.add_field(name="Participantes", value=str(len(self.participantes)), inline=True)
        if finalizado:
            embed.add_field(name="Resultado", value=" ".join(f"<@{u}>" for u in ganadores) if ganadores else "No hubo participantes.", inline=False)
        else:
            embed.add_field(name="Cómo participar", value="Pulsa **🎁 Participar**; vuelve a pulsar para salir.", inline=False)
        return embed

    @discord.ui.button(label="🎁 Participar", style=discord.ButtonStyle.primary, custom_id="sorteo:participar")
    async def participar(self, interaction, button):
        async with self.lock:
            if self.finalizado:
                await interaction.response.send_message("Este sorteo ya terminó.", ephemeral=True)
                return
            if interaction.user.id in self.participantes:
                self.participantes.remove(interaction.user.id)
                respuesta = "Saliste del sorteo."
            else:
                self.participantes.add(interaction.user.id)
                respuesta = "¡Ya estás participando!"
            await interaction.response.send_message(respuesta, ephemeral=True)
            if self.mensaje:
                await self.mensaje.edit(embed=self.crear_embed(), view=self)

    async def terminar(self):
        await asyncio.sleep(self.duracion)
        async with self.lock:
            self.finalizado = True
            ganadores = random.sample(list(self.participantes), k=min(self.cantidad_ganadores, len(self.participantes))) if self.participantes else []
            for item in self.children:
                item.disabled = True
            if self.mensaje:
                await self.mensaje.edit(embed=self.crear_embed(True, ganadores), view=self)
            if ganadores and self.canal:
                await self.canal.send(f"🎉 Ganador(es) de **{self.premio}**: " + " ".join(f"<@{u}>" for u in ganadores), allowed_mentions=discord.AllowedMentions(users=True))


@tree.command(name="sorteo", description="Crea un sorteo en este canal", guild=GUILD)
@app_commands.describe(ganadores="Cantidad de ganadores (1 a 10)", duracion="Ejemplo: 10 minutos, 2 horas, 3 días o 1 mes", premio="Premio opcional")
async def sorteo(interaction, ganadores: app_commands.Range[int, 1, 10], duracion: str, premio: Optional[str] = None):
    segundos = analizar_duracion(duracion)
    if segundos is None:
        await interaction.response.send_message("Duración no válida. Usa `10 minutos`, `2 horas`, `3 días` o `1 mes`.", ephemeral=True)
        return
    if interaction.channel is None:
        await interaction.response.send_message("No encontré este canal.", ephemeral=True)
        return
    view = SorteoView(premio.strip() if premio and premio.strip() else "Premio sorpresa", ganadores, segundos)
    view.canal = interaction.channel
    await interaction.response.send_message(embed=view.crear_embed(), view=view)
    view.mensaje = await interaction.original_response()
    asyncio.create_task(view.terminar())


class CookieGameView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.finished, self.message = False, None
        self.lock = asyncio.Lock()

    @discord.ui.button(label="🍪", style=discord.ButtonStyle.success, custom_id="cookie_game:click")
    async def click_cookie(self, interaction, button):
        async with self.lock:
            if self.finished:
                await interaction.response.send_message("Alguien ya tocó la galleta y ganó.", ephemeral=True)
                return
            self.finished = True
            button.disabled = True
            await interaction.response.edit_message(content=f"🍪 {interaction.user.mention} tocó la galleta primero y ganó.", view=self, allowed_mentions=discord.AllowedMentions.none())

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
    await interaction.response.send_message("La galleta aparece en **5**…")
    message = await interaction.original_response()
    for count in range(4, 0, -1):
        await asyncio.sleep(1)
        await message.edit(content=f"La galleta aparece en **{count}**…")
    await asyncio.sleep(1)
    view = CookieGameView()
    view.message = message
    await message.edit(content="🍪 ¡Apareció la galleta! ¡Tócala primero!", view=view)


@tree.command(name="número", description="Elige un número secreto del 1 al 200", guild=GUILD)
@app_commands.describe(numero="Número secreto, entre 1 y 200")
async def numero_game(interaction, numero: app_commands.Range[int, 1, 200]):
    if interaction.guild_id is None or interaction.channel is None:
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    async with number_game_lock:
        if interaction.guild_id in active_number_games:
            await interaction.response.send_message("Ya hay una partida activa en este servidor.", ephemeral=True)
            return
        active_number_games[interaction.guild_id] = {"numero": numero, "autor_id": interaction.user.id}
    await interaction.response.send_message("Partida iniciada. El número no se mostrará aquí.", ephemeral=True)
    await interaction.channel.send(f"🎲 {interaction.user.mention} piensa en un número del 1 al 200. ¡Adivínenlo!", allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="autorole", description="Configura el rol de los nuevos miembros", guild=GUILD)
@app_commands.default_permissions(manage_roles=True)
@app_commands.describe(rol="Rol que recibirán los nuevos miembros")
async def autorole_command(interaction, rol: discord.Role):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    actor, bot_member = interaction.user, interaction.guild.me
    if not actor.guild_permissions.manage_roles or bot_member is None or not bot_member.guild_permissions.manage_roles:
        await interaction.response.send_message("Tú y el bot necesitan Administrar roles.", ephemeral=True)
        return
    if rol.is_default() or rol.managed or rol >= bot_member.top_role:
        await interaction.response.send_message("Ese rol no se puede asignar o está por encima del rol del bot.", ephemeral=True)
        return
    if actor.id != interaction.guild.owner_id and rol >= actor.top_role:
        await interaction.response.send_message("Solo puedes elegir roles por debajo de tu rol más alto.", ephemeral=True)
        return
    autoroles[interaction.guild_id] = rol.id
    await interaction.response.send_message(f"✅ Los nuevos miembros recibirán {rol.mention}.", allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="ban", description="Banea a un miembro", guild=GUILD)
@app_commands.default_permissions(ban_members=True)
@app_commands.describe(usuario="Miembro que quieres banear")
async def ban_command(interaction, usuario: discord.Member):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    await interaction.response.send_message(await moderar_miembro(interaction.guild, interaction.user, usuario, "ban"), allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="kick", description="Expulsa a un miembro", guild=GUILD)
@app_commands.default_permissions(kick_members=True)
@app_commands.describe(usuario="Miembro que quieres expulsar")
async def kick_command(interaction, usuario: discord.Member):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    await interaction.response.send_message(await moderar_miembro(interaction.guild, interaction.user, usuario, "kick"), allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="invite", description="Muestra invitaciones atribuidas a un miembro", guild=GUILD)
@app_commands.describe(usuario="Usuario opcional; vacío muestra tus invitaciones")
async def invite_command(interaction, usuario: Optional[discord.Member] = None):
    if interaction.guild_id is None:
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    target = usuario or interaction.user
    await interaction.response.send_message(f"📨 {target.mention} ha invitado a **{invite_counts.get((interaction.guild_id, target.id), 0)}** persona(s), contadas mientras el bot está activo.", allowed_mentions=discord.AllowedMentions.none())


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
    if interaction.guild_id is None or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_messages:
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
    if interaction.guild_id is None or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_messages:
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
    texto = f"📢 **ANUNCIO IMPORTANTE**\n{mensaje.strip()}"
    if not mensaje.strip() or len(texto) > 2000:
        await interaction.response.send_message("Escribe un anuncio de hasta 2000 caracteres.", ephemeral=True)
        return
    await interaction.response.send_message(texto, allowed_mentions=discord.AllowedMentions.none())


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
    try:
        current = await member.guild.invites()
    except (discord.Forbidden, discord.HTTPException) as error:
        print(f"No pude consultar invitaciones: {error}")
        return
    previous = invite_cache.get(member.guild.id, {})
    used = next((invite for invite in current if (invite.uses or 0) > previous.get(invite.code, 0)), None)
    invite_cache[member.guild.id] = {invite.code: invite.uses or 0 for invite in current}
    if used and used.inviter:
        key = (member.guild.id, used.inviter.id)
        invite_counts[key] = invite_counts.get(key, 0) + 1


@client.event
async def on_invite_create(invite):
    if invite.guild:
        invite_cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses or 0


@client.event
async def on_invite_delete(invite):
    if invite.guild:
        invite_cache.setdefault(invite.guild.id, {}).pop(invite.code, None)


@client.event
async def on_message(message):
    if message.author.bot or message.guild is None:
        return
    content = message.content.strip()
    parts = content.split(maxsplit=1)
    command = parts[0].lower() if parts else ""

    if content.lower() in ("?lock", "?unlock"):
        if not isinstance(message.author, discord.Member):
            return
        result = await cambiar_bloqueo_canal(message.channel, message.author, content.lower() == "?lock")
        await message.channel.send(result, delete_after=8)
        return

    purge_count = None
    if command == "?purge" and len(parts) == 2:
        try:
            purge_count = int(parts[1])
        except ValueError:
            purge_count = -1
    elif content.lower().startswith("? purge "):
        try:
            purge_count = int(content.split(maxsplit=2)[2])
        except (ValueError, IndexError):
            purge_count = -1
    if purge_count is not None:
        if not isinstance(message.author, discord.Member) or not message.author.guild_permissions.manage_messages:
            await message.channel.send("Necesitas Administrar mensajes.", delete_after=8)
            return
        bot_member = message.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_messages:
            await message.channel.send("Al bot le falta Administrar mensajes.", delete_after=8)
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
        reference = message.reference
        if not reference or not reference.message_id:
            await message.channel.send("Responde al mensaje del miembro con `?ban` o `?kick`.")
            return
        try:
            target_message = reference.resolved
            if target_message is None:
                channel = message.guild.get_channel(reference.channel_id) or await client.fetch_channel(reference.channel_id)
                target_message = await channel.fetch_message(reference.message_id)
            target = message.guild.get_member(target_message.author.id) or await message.guild.fetch_member(target_message.author.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await message.channel.send("No pude encontrar al miembro del mensaje respondido.")
            return
        action = "ban" if content.lower() == "?ban" else "kick"
        await message.channel.send(await moderar_miembro(message.guild, message.author, target, action), allowed_mentions=discord.AllowedMentions.none())
        return

    if command == "?afk":
        reason = parts[1] if len(parts) > 1 else "Sin motivo"
        await activar_afk(message.guild.id, message.author, reason)
        await message.channel.send(f"{message.author.mention} está AFK. Motivo: {reason}", allowed_mentions=discord.AllowedMentions.none())
        return

    afk = afk_users.pop((message.guild.id, message.author.id), None)
    if afk:
        elapsed = formato_duracion(time.time() - afk["inicio"])
        await message.channel.send(f"{message.author.mention} salió de AFK y llevaba {elapsed}.", allowed_mentions=discord.AllowedMentions.none())

    if command == "?avatar":
        target = message.author
        if message.reference and message.reference.message_id:
            try:
                ref = message.reference.resolved or await message.channel.fetch_message(message.reference.message_id)
                target = ref.author
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        await message.channel.send(embed=await crear_embed_avatar(target))
        return

    # El filtro se aplica a miembros no administradores; los administradores quedan exentos.
    if isinstance(message.author, discord.Member) and not message.author.guild_permissions.administrator:
        normalized = normalizar(message.content)
        for word in blocked_words.get(message.guild.id, set()):
            if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", normalized):
                try:
                    await message.delete()
                except (discord.Forbidden, discord.HTTPException) as error:
                    print(f"No pude borrar mensaje con palabra bloqueada: {error}")
                return

    if content.isdecimal():
        async with number_game_lock:
            game = active_number_games.get(message.guild.id)
            if game and message.author.id != game["autor_id"] and 1 <= int(content) <= 200 and int(content) == game["numero"]:
                active_number_games.pop(message.guild.id, None)
            else:
                game = None
        if game:
            author = message.guild.get_member(game["autor_id"])
            await message.channel.send(f"🎉 ¡Felicidades {message.author.mention}! {author.mention if author else 'El usuario'} pensaba el número **{game['numero']}** y lo adivinaste.", allowed_mentions=discord.AllowedMentions.users())
            return

    seen = set()
    for user in message.mentions:
        state = afk_users.get((message.guild.id, user.id))
        if state and user.id not in seen:
            seen.add(user.id)
            await message.channel.send(f"{user.mention} está AFK. Motivo: {state['motivo']}", allowed_mentions=discord.AllowedMentions.none())


@client.event
async def on_ready():
    print(f"Conectado como {client.user}")
    for server in client.guilds:
        try:
            invites = await server.invites()
            invite_cache[server.id] = {invite.code: invite.uses or 0 for invite in invites}
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"No pude cargar invitaciones de {server.id}: {error}")
    try:
        commands = await tree.sync(guild=GUILD)
        print(f"Comandos sincronizados: {len(commands)}")
    except Exception as error:
        print(f"Error al sincronizar comandos: {error}")


client.run(TOKEN)
