import discord
from discord.ext import commands
from discord.ui import View, Button
from discord import app_commands
import asyncio
import os
import re
from datetime import datetime
from openai import OpenAI   # ← ここだけでOK
from dotenv import load_dotenv
load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))  # ← 初期化はこっち

# ====== 設定 ======
SUPPORT_ROLE_ID = 1398724601256874014
LOG_CHANNEL_ID = 1402874246786711572
GUILD_ID = 1398607685158440991  # サーバーID
openai.api_key = os.getenv("OPENAI_API_KEY")

# ====== Bot初期化 ======
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"✅ Bot connected as {bot.user}")

# ====== チケット作成ボタンのView ======
class TicketView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📉 チケットを作成", style=discord.ButtonStyle.green, custom_id="create_ticket")
    async def create_ticket(self, interaction: discord.Interaction, button: Button):
        guild = interaction.guild
        author = interaction.user
        current_channel = interaction.channel
        category = current_channel.category

        if category is None:
            await interaction.response.send_message("⚠️ このチャンネルはカテゴリーに属していません。", ephemeral=True)
            return

        base_name = f"{category.name}-問い合わせ"
        existing = [ch for ch in category.text_channels if ch.name.startswith(base_name)]
        count = len(existing) + 1
        channel_name = f"{base_name}-{count}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            author: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.get_role(SUPPORT_ROLE_ID): discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"{author.name} の問い合わせチケット"
        )

        await interaction.response.send_message(f"✅ チケットを作成しました: {channel.mention}", ephemeral=True)
        await channel.send(
            f"{author.mention} 問い合わせしたい内容を送信してください、抽選者が対応します。",
            view=CloseTicketView(author)
        )

# ====== チケット終了ボタン ======
class CloseTicketView(View):
    def __init__(self, user):
        super().__init__(timeout=None)
        self.user = user

    @discord.ui.button(label="✅ 問い合わせ終了", style=discord.ButtonStyle.red, custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: Button):
        channel = interaction.channel
        await interaction.response.send_message("🗑 5秒後にチャンネルを削除します。ログを送信中...", ephemeral=True)

        messages = [msg async for msg in channel.history(limit=None, oldest_first=True)]
        safe_name = re.sub(r'[\\/:*?"<>|]', '_', self.user.name)
        filename = f"{safe_name}_log.txt"

        with open(filename, "w", encoding="utf-8") as f:
            for msg in messages:
                timestamp = msg.created_at.strftime("%Y/%m/%d %H:%M")
                f.write(f"[{timestamp}] {msg.author.display_name}: {msg.content}\n")

        log_channel = interaction.client.get_channel(LOG_CHANNEL_ID)
        embed = discord.Embed(
            title="📉 問い合わせチケットログ",
            description=f"{self.user.mention} の問い合わせチャンネルが終了しました。",
            color=discord.Color.blue(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="チャンネル名", value=channel.name, inline=False)
        embed.add_field(name="メッセージ数", value=str(len(messages)), inline=False)

        await log_channel.send(embed=embed, file=discord.File(filename))
        await asyncio.sleep(5)
        await channel.delete()

# ====== /ticketa コマンド ======
@bot.tree.command(
    name="ticketa",
    description="問い合わせ用チケット作成ボタンを送信します",
    guild=discord.Object(id=GUILD_ID)
)
async def ticketa(interaction: discord.Interaction):
    if not any(role.id == SUPPORT_ROLE_ID for role in interaction.user.roles):
        await interaction.response.send_message("❌ このコマンドを使用する権限がありません。", ephemeral=True)
        return

    await interaction.response.send_message(
        "質問や問い合わせ、サービスのご利用は下記のチケット作成ボタンをクリックしてください。",
        view=TicketView()
    )

# ====== /ask コマンド (ChatGPT) ======
@bot.tree.command(
    name="ask",
    description="AI (ChatGPT) に質問できます",
    guild=discord.Object(id=GUILD_ID)
)
@app_commands.describe(question="AIに聞きたいこと")
async def ask(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    try:
        res = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": question}]
        )
        answer = res.choices[0].message.content
        await interaction.followup.send(f"🧠 ChatGPTの回答:\n{answer}")
    except Exception as e:
        await interaction.followup.send("❌ エラー: " + str(e))

# ====== /image コマンド (DALL·E) ======
@bot.tree.command(
    name="image",
    description="AI画像生成を実行します",
    guild=discord.Object(id=GUILD_ID)
)
@app_commands.describe(prompt="画像にしたい内容")
async def image(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer()
    try:
        response = openai.Image.create(
            prompt=prompt,
            n=1,
            size="1024x1024"
        )
        image_url = response["data"][0]["url"]
        await interaction.followup.send(f"🎨 生成した画像:\n{image_url}")
    except Exception as e:
        await interaction.followup.send("❌ 画像生成エラー: " + str(e))

# ====== Bot起動 ======
bot.run(os.getenv("DISCORD_TOKEN"))


