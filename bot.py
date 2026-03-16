import os
import asyncio
import threading
import discord
from discord.ext import commands
from flask import Flask

TOKEN = os.getenv("DISCORD_TOKEN")
TICKET_CHANNEL_ID = int(os.getenv("TICKET_CHANNEL_ID", "0"))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

app = Flask(__name__)


@app.get("/")
def health():
    return "Bot is running", 200


def run_web():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))


CATEGORY_CONFIG = {
    "Armor": {
        "suboptions": {
            "Cloth": "Tailoring",
            "Leather/Mail": "Leatherworking",
            "Plate": "Blacksmithing",
            "Head/Wrist/Boots (single stat armor)": "Engineering",
        }
    },
    "Weapons": {
        "suboptions": {
            "Swords/Axes/Maces/Daggers/Polearms": "Blacksmithing",
            "Bows/Staves/Offhands": "Inscription",
            "Guns": "Engineering",
        }
    },
    "Consumables": {
        "suboptions": {
            "Flasks/Potions": "Alchemy",
            "Treatise": "Inscription",
            "Gems": "Jewelcrafting",
        }
    },
    "Enchants": {
        "role": "Enchanting"
    }
}


def can_close_thread(user: discord.Member, requester_id: int) -> bool:
    return user.id == requester_id or user.guild_permissions.administrator


async def find_existing_thread_for_user(parent_channel: discord.TextChannel, user_id: int):
    for thread in parent_channel.threads:
        try:
            members = [member async for member in thread.fetch_members()]
            if any(member.id == user_id for member in members):
                return thread
        except Exception:
            continue
    return None


async def auto_close_thread_after_24_hours(thread: discord.Thread):
    await asyncio.sleep(86400)

    try:
        await thread.delete()
    except discord.NotFound:
        pass
    except discord.Forbidden:
        print("Missing permission to delete thread after 24 hours.")
    except Exception as e:
        print(f"Error auto-closing thread after 24 hours: {e}")


class AbortCraftView(discord.ui.View):
    def __init__(self, requester_id: int):
        super().__init__(timeout=None)
        self.requester_id = requester_id

    @discord.ui.button(label="Abort Crafting Request", style=discord.ButtonStyle.red, emoji="✖️")
    async def abort(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "This button can only be used inside a ticket thread.",
                ephemeral=True
            )
            return

        if not isinstance(interaction.user, discord.Member) or not can_close_thread(interaction.user, self.requester_id):
            await interaction.response.send_message(
                "Only the thread creator or an admin can abort this request.",
                ephemeral=True
            )
            return

        await interaction.response.send_message("Closing thread...", ephemeral=True)
        await interaction.channel.delete()


class CloseNowView(discord.ui.View):
    def __init__(self, requester_id: int):
        super().__init__(timeout=None)
        self.requester_id = requester_id

    @discord.ui.button(label="Close Now", style=discord.ButtonStyle.red, emoji="🗑️")
    async def close_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not can_close_thread(interaction.user, self.requester_id):
            await interaction.response.send_message(
                "Only the thread creator or an admin can close this.",
                ephemeral=True
            )
            return

        await interaction.response.send_message("Closing thread...", ephemeral=True)
        await interaction.channel.delete()


class CompleteCraftView(discord.ui.View):
    def __init__(self, requester_id: int, crafter_role: str):
        super().__init__(timeout=None)
        self.requester_id = requester_id
        self.crafter_role = crafter_role

    @discord.ui.button(label="Completed", style=discord.ButtonStyle.green, emoji="✅")
    async def complete(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = discord.utils.get(interaction.guild.roles, name=self.crafter_role)

        if role is None or role not in interaction.user.roles:
            await interaction.response.send_message(
                f"Only the {self.crafter_role} role can complete this request.",
                ephemeral=True
            )
            return

        button.disabled = True
        await interaction.response.edit_message(view=self)

        requester = interaction.guild.get_member(self.requester_id)

        if requester:
            await interaction.channel.send(
                f"{requester.mention} The crafting order is completed, this thread will automatically close in 24 hours or you can close it manually by clicking the close thread button."
            )
        else:
            await interaction.channel.send(
                "The crafting order is completed, this thread will automatically close in 24 hours or you can close it manually by clicking the close thread button."
            )

        await interaction.channel.send(view=CloseNowView(self.requester_id))


async def handle_final_request(interaction: discord.Interaction, display_label: str, role_name: str, requester_id: int):
    user = interaction.user
    thread = interaction.channel
    guild = interaction.guild

    if user.id != requester_id:
        await interaction.followup.send(
            "Only the person who opened this thread can continue this crafting request.",
            ephemeral=True
        )
        return

    await thread.edit(name=f"{display_label} - {user.name}")
    await thread.send("Please list all the things you want crafted.")

    def check(m: discord.Message):
        return m.author.id == user.id and m.channel.id == thread.id

    try:
        msg = await bot.wait_for("message", check=check, timeout=300)
    except asyncio.TimeoutError:
        await thread.send("Timed out waiting for your crafting details. Closing thread...")
        await asyncio.sleep(5)
        await thread.delete()
        return

    role = discord.utils.get(guild.roles, name=role_name)

    if role:
        await thread.send(
            f"{role.mention} {user.display_name} needs an item crafted: **{msg.content}**"
        )
    else:
        await thread.send(
            f"{user.display_name} needs an item crafted: **{msg.content}**"
        )

    await thread.send(
        "Click below when this crafting request has been completed.",
        view=CompleteCraftView(user.id, role_name)
    )


class SubcategorySelect(discord.ui.Select):
    def __init__(self, category: str, requester_id: int):
        self.category = category
        self.requester_id = requester_id
        sub = CATEGORY_CONFIG[category]["suboptions"]

        options = [discord.SelectOption(label=name) for name in sub]

        super().__init__(
            placeholder=f"Choose {category} type",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who opened this thread can use this menu.",
                ephemeral=True
            )
            return

        choice = self.values[0]
        role = CATEGORY_CONFIG[self.category]["suboptions"][choice]

        self.disabled = True
        await interaction.response.edit_message(
            content=f"Selected: **{choice}**",
            view=self.view
        )

        await handle_final_request(interaction, choice, role, self.requester_id)


class SubcategoryView(discord.ui.View):
    def __init__(self, category: str, requester_id: int):
        super().__init__(timeout=300)
        self.add_item(SubcategorySelect(category, requester_id))


class CategorySelect(discord.ui.Select):
    def __init__(self, requester_id: int):
        self.requester_id = requester_id

        options = [
            discord.SelectOption(label="Armor"),
            discord.SelectOption(label="Weapons"),
            discord.SelectOption(label="Consumables"),
            discord.SelectOption(label="Enchants"),
        ]

        super().__init__(
            placeholder="Choose what you need crafted",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who opened this thread can use this menu.",
                ephemeral=True
            )
            return

        category = self.values[0]

        self.disabled = True
        await interaction.response.edit_message(
            content=f"Selected: **{category}**",
            view=self.view
        )

        if category == "Enchants":
            await handle_final_request(interaction, "Enchants", "Enchanting", self.requester_id)
            return

        await interaction.followup.send(
            f"Please choose a {category.lower()} type:",
            view=SubcategoryView(category, self.requester_id)
        )


class CategoryView(discord.ui.View):
    def __init__(self, requester_id: int):
        super().__init__(timeout=300)
        self.add_item(CategorySelect(requester_id))


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.green, custom_id="create_ticket")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        user = interaction.user

        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "This button can only be used in a text channel.",
                ephemeral=True
            )
            return

        existing_thread = await find_existing_thread_for_user(channel, user.id)
        if existing_thread is not None:
            await interaction.response.send_message(
                f"You already have an open crafting request: {existing_thread.mention}",
                ephemeral=True
            )
            return

        thread = await channel.create_thread(
            name=f"ticket-{user.name}",
            type=discord.ChannelType.private_thread
        )

        await thread.add_user(user)

        asyncio.create_task(auto_close_thread_after_24_hours(thread))

        await thread.send(
            "If you made this thread by mistake or no longer need a craft, click below to cancel it.",
            view=AbortCraftView(user.id)
        )

        notice_embed = discord.Embed(
            title="<a:tcgold:1482787632411840512> Please help out crafters by tipping 1.5k for each craft. <a:tcgold:1482787632411840512>",
            description=(
                "Finishing reagents are very useful for helping crafters do crafts for everyone.\n"
                "These usually cost 1k-2k, and crafters usually have to buy it themselves.\n\n"
                "Thank you."
            ),
            color=discord.Color.purple()
        )

        await thread.send(embed=notice_embed)

        await thread.send(
            f"{user.display_name}, please choose the type of crafting request below:",
            view=CategoryView(user.id)
        )

        await interaction.response.send_message(
            f"Your ticket has been created: {thread.mention}",
            ephemeral=True
        )


async def send_ticket_panel():
    await bot.wait_until_ready()

    channel = bot.get_channel(TICKET_CHANNEL_ID)

    if channel is None:
        print("Ticket channel not found.")
        return

    async for message in channel.history(limit=50):
        if message.author == bot.user and message.components:
            for row in message.components:
                for component in row.children:
                    if getattr(component, "custom_id", None) == "create_ticket":
                        print("Ticket panel already exists. Skipping repost.")
                        return

    notice_embed = discord.Embed(
        title="<a:tcgold:1482787632411840512> Please help out crafters by tipping 1.5k for each craft. <a:tcgold:1482787632411840512>",
        description=(
            "Finishing reagents are very useful for helping crafters do crafts for everyone.\n"
            "These usually cost 1k-2k, and crafters usually have to buy it themselves.\n\n"
            "Thank you."
        ),
        color=discord.Color.purple()
    )

    panel_embed = discord.Embed(
        title="Crafting Requests",
        description="Click the button below to open a crafting ticket.",
        color=discord.Color.green()
    )

    await channel.send(embed=notice_embed)
    await channel.send(embed=panel_embed, view=TicketView())
    print("Ticket panel sent.")


@bot.event
async def on_ready():
    bot.add_view(TicketView())
    print(f"Logged in as {bot.user}")
    await send_ticket_panel()


if __name__ == "__main__":
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN is not set.")

    threading.Thread(target=run_web, daemon=True).start()
    bot.run(TOKEN)
