import random
import re

import discord
from discord import app_commands

GUILD_ID = 1559455132821753856
GUILD = discord.Object(id=GUILD_ID)
KISS_GIFS = [
    "https://media.tenor.com/8e0ZPq3dH0AAAAAC/anime-kiss.gif",
    "https://media.tenor.com/4x9Y8wP2d7AAAAAC/kiss-anime.gif",
]
SLAP_GIFS = [
    "https://media.tenor.com/7s2xJj5m9XAAAAAC/anime-slap.gif",
    "https://media.tenor.com/3qZx8Kq2v2AAAAAC/slap-anime.gif",
    "https://media.tenor.com/2x7pHjQkXwAAAAAC/anime-slap.gif",
]


def _allowed_users(*user_ids: int) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        users=[discord.Object(id=user_id) for user_id in user_ids],
        roles=False,
        everyone=False,
    )


class KissView(discord.ui.View):
    def __init__(self, requester_id: int, target_id: int):
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.target_id = target_id
        self.decided = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "Solo la persona invitada puede responder a esta solicitud.", ephemeral=True
            )
            return False
        if self.decided:
            await interaction.response.send_message("Esta solicitud ya fue respondida.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="💋 Coincidir", style=discord.ButtonStyle.danger, custom_id="social_kiss:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.decided = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"💋 {interaction.user.mention} aceptó el beso de <@{self.requester_id}>.",
            embed=discord.Embed().set_image(url=random.choice(KISS_GIFS)),
            view=self,
            allowed_mentions=_allowed_users(self.requester_id, self.target_id),
        )

    @discord.ui.button(label="🤦🏻 Rechazar", style=discord.ButtonStyle.success, custom_id="social_kiss:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.decided = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"🤦🏻 {interaction.user.mention} rechazó el beso de <@{self.requester_id}>.",
            embed=discord.Embed().set_image(url=random.choice(SLAP_GIFS)),
            view=self,
            allowed_mentions=_allowed_users(self.requester_id, self.target_id),
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if getattr(self, "message", None):
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


class SlapView(discord.ui.View):
    def __init__(self, requester_id: int, target_id: int):
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.target_id = target_id
        self.used = False

    @discord.ui.button(label="😡 Devolver", style=discord.ButtonStyle.danger, custom_id="social_slap:return")
    async def return_slap(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "Solo la persona que recibió la bofetada puede devolverla.", ephemeral=True
            )
            return
        if self.used:
            await interaction.response.send_message("Ya devolviste la bofetada.", ephemeral=True)
            return
        self.used = True
        button.disabled = True
        await interaction.response.edit_message(
            content=f"😡 {interaction.user.mention} devolvió la bofetada a <@{self.requester_id}>.",
            embed=discord.Embed().set_image(url=random.choice(SLAP_GIFS)),
            view=self,
            allowed_mentions=_allowed_users(self.requester_id, self.target_id),
        )


async def send_kiss(channel, actor, target):
    if target.bot or target.id == actor.id:
        await channel.send("Elige a otro miembro del servidor; no puedes enviarte el beso a ti mismo.")
        return
    view = KissView(actor.id, target.id)
    message = await channel.send(
        f"💋 {actor.mention} quiere darle un beso a {target.mention}. {target.mention}, ¿aceptas?",
        embed=discord.Embed().set_image(url=random.choice(KISS_GIFS)),
        view=view,
        allowed_mentions=_allowed_users(actor.id, target.id),
    )
    view.message = message


async def send_slap(channel, actor, target):
    if target.bot or target.id == actor.id:
        await channel.send("Elige a otro miembro del servidor; no puedes abofetearte a ti mismo.")
        return
    await channel.send(
        f"🖐️ {actor.mention} abofeteó a {target.mention}.",
        embed=discord.Embed().set_image(url=random.choice(SLAP_GIFS)),
        view=SlapView(actor.id, target.id),
        allowed_mentions=_allowed_users(actor.id, target.id),
    )


def install(client: discord.Client, tree: app_commands.CommandTree):
    """Register the commands and reply-based prefix aliases exactly once."""
    if getattr(client, "_social_commands_installed", False):
        return

    @tree.command(name="kiss", description="Envía una solicitud de beso", guild=GUILD)
    @app_commands.describe(usuario="Persona a quien enviar la solicitud")
    async def kiss_command(interaction: discord.Interaction, usuario: discord.Member):
        if interaction.channel is None:
            await interaction.response.send_message("No encontré este canal.", ephemeral=True)
            return
        await interaction.response.defer()
        await send_kiss(interaction.channel, interaction.user, usuario)

    @tree.command(name="slap", description="Abofetea a un miembro de forma divertida", guild=GUILD)
    @app_commands.describe(usuario="Miembro a quien abofetear")
    async def slap_command(interaction: discord.Interaction, usuario: discord.Member):
        if interaction.channel is None:
            await interaction.response.send_message("No encontré este canal.", ephemeral=True)
            return
        await interaction.response.defer()
        await send_slap(interaction.channel, interaction.user, usuario)

    async def on_message(message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        match = re.fullmatch(r"\?(kiss|slap)\s*", message.content.strip(), re.IGNORECASE)
        if not match:
            return
        reference = message.reference
        if not reference or not reference.message_id:
            await message.channel.send(
                f"Responde al mensaje de alguien para usar `?{match.group(1).lower()}`."
            )
            return
        try:
            replied = reference.resolved
            if replied is None:
                channel = message.guild.get_channel(reference.channel_id)
                if channel is None:
                    channel = await client.fetch_channel(reference.channel_id)
                replied = await channel.fetch_message(reference.message_id)
            target = message.guild.get_member(replied.author.id)
            if target is None:
                target = await message.guild.fetch_member(replied.author.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await message.channel.send("No pude encontrar al miembro del mensaje respondido.")
            return
        if match.group(1).lower() == "kiss":
            await send_kiss(message.channel, message.author, target)
        else:
            await send_slap(message.channel, message.author, target)

    client.add_listener(on_message, "on_message")
    client._social_commands_installed = True
