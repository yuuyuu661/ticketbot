import discord
from discord.ext import commands
from discord.ui import View, Button
from discord import app_commands
import asyncio
import os
import re
from datetime import datetime

# ==========================
# 設定（必要に応じて書き換え）
# ==========================
SUPPORT_ROLE_ID = 1398724601256874014  # サポートロールのID
LOG_CHANNEL_ID = 1402874246786711572  # 対応履歴チャンネルのID

# ==========================
# Bot初期化
# ==========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await bot.tree.sync(guild=discord.Object(id=1398607685158440991))  # ← これ重要
    print(f"✅ Bot connected as {bot.user}")

# ==========================
# チケット作成ボタンのView
# ==========================
class TicketView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📩 チケットを作成", style=discord.ButtonStyle.green, custom_id="create_ticket")
    async def create_ticket(self, interaction: discord.Interaction, button: Button):
        guild = interaction.guild
        author = interaction.user
        current_channel = interaction.channel
        category = current_channel.category

        if category is None:
            await interaction.response.send_message("⚠️ このチャンネルはカテゴリーに属していません。", ephemeral=True)
            return

        # チャンネル名生成
        base_name = f"{category.name}-問い合わせ"
        existing = [ch for ch in category.text_channels if ch.name.startswith(base_name)]
        count = len(existing) + 1
        channel_name = f"{base_name}-{count}"

        # パーミッション設定
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            author: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.get_role(SUPPORT_ROLE_ID): discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        # チャンネル作成
        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"{author.name} の問い合わせチケット"
        )

        await interaction.response.send_message(f"✅ チケットを作成しました: {channel.mention}", ephemeral=True)

        await channel.send(
            f"{author.mention} 問い合わせしたい内容を送信してください、担当者が対応します。",
            view=CloseTicketView(author)
        )

# ==========================
# チケット終了ボタンのView
# ==========================
class CloseTicketView(View):
    def __init__(self, user):
        super().__init__(timeout=None)
        self.user = user

    @discord.ui.button(label="✅ 問い合わせ終了", style=discord.ButtonStyle.red, custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: Button):
        channel = interaction.channel
        await interaction.response.send_message("🗑 5秒後にチャンネルを削除します。ログを送信中...", ephemeral=True)

        # ログ収集
        messages = [msg async for msg in channel.history(limit=None, oldest_first=True)]
        safe_name = re.sub(r'[\\/:*?"<>|]', '_', self.user.name)
        filename = f"{safe_name}_log.txt"

        with open(filename, "w", encoding="utf-8") as f:
            for msg in messages:
                timestamp = msg.created_at.strftime("%Y/%m/%d %H:%M")
                f.write(f"[{timestamp}] {msg.author.display_name}: {msg.content}\n")

        # ログ送信
        log_channel = interaction.client.get_channel(LOG_CHANNEL_ID)
        embed = discord.Embed(
            title="📩 問い合わせチケットログ",
            description=f"{self.user.mention} の問い合わせチャンネルが終了しました。",
            color=discord.Color.blue(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="チャンネル名", value=channel.name, inline=False)
        embed.add_field(name="メッセージ数", value=str(len(messages)), inline=False)

        await log_channel.send(embed=embed, file=discord.File(filename))

        # 5秒後にチャンネル削除
        await asyncio.sleep(5)
        await channel.delete()

# ==========================
# /ticket コマンド
# ==========================
@bot.tree.command(
    name="ticketa",
    description="問い合わせ用チケット作成ボタンを送信します",
    guild=discord.Object(id=1398607685158440991)  # ← サーバーID指定
)
async def ticketa(interaction: discord.Interaction):
    await interaction.response.send_message(
        "質問や問い合わせ、サービスのご利用は下記のチケット作成ボタンをクリックしてください。",
        view=TicketView()
    )

# ==========================
# Bot起動
# ==========================
bot.run(os.environ["DISCORD_TOKEN"])


