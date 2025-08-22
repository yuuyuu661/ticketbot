import discord
from discord.ext import commands
from discord.ui import View, Button
from discord import app_commands
import asyncio
import os
import re
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Tuple
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ====== 設定 ======
SUPPORT_ROLE_ID = 1398724601256874014
LOG_CHANNEL_ID = 1402874246786711572
GUILD_ID = 1398607685158440991  # サーバーID

# 公開防止のためリポジトリ直下に置かない
VC_LOG_FILE = "logs/vc_logs.json"
JST = timezone(timedelta(hours=9))

# ====== Bot初期化 ======
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True   # VC監視に必須
intents.members = True        # Member 引数に推奨
bot = commands.Bot(command_prefix="!", intents=intents)

# 参加中状態の一時保持: { (guild_id, user_id): (channel_id, joined_at_utc) }
vc_start_times: Dict[Tuple[int, int], Tuple[int, datetime]] = {}

# ====== ユーティリティ ======
def ensure_log_dir():
    os.makedirs(os.path.dirname(VC_LOG_FILE), exist_ok=True)

def load_vc_logs():
    ensure_log_dir()
    if os.path.exists(VC_LOG_FILE):
        try:
            with open(VC_LOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_vc_logs(data):
    ensure_log_dir()
    with open(VC_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def append_vc_log(guild_id: int, user_id: int, channel_id: int, category_id: Optional[int],
                  joined_at_utc: datetime, left_at_utc: datetime):
    data = load_vc_logs()
    gkey = str(guild_id)
    data.setdefault(gkey, [])
    data[gkey].append({
        "user_id": user_id,
        "channel_id": channel_id,
        "category_id": category_id,
        "join": joined_at_utc.replace(tzinfo=timezone.utc).isoformat(),
        "leave": left_at_utc.replace(tzinfo=timezone.utc).isoformat()
    })
    save_vc_logs(data)

def parse_jst(s: str, is_start: bool) -> datetime:
    """
    s: 'YYYY-MM-DD HH:MM' または 'YYYY-MM-DD'
    is_start: Trueなら00:00補完、Falseなら23:59補完
    """
    s = s.strip()
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d")
            if is_start:
                dt = dt.replace(hour=0, minute=0)
            else:
                dt = dt.replace(hour=23, minute=59)
        except ValueError:
            raise ValueError("日付の形式が不正です。YYYY-MM-DD または YYYY-MM-DD HH:MM で入力してください。")
    return dt.replace(tzinfo=JST)

def overlap_seconds(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> float:
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    return max(0.0, (end - start).total_seconds())

def slugify_for_channel(text: str, fallback: str = "ticket") -> str:
    """
    Discordのテキストチャンネル名向けに整形:
    - 小文字化
    - 全角→半角英数（簡易）
    - 空白/連続空白→ハイフン
    - 許可外文字除去（英数・ハイフン・アンダーバー）
    - 先頭末尾のハイフン除去
    - 空ならfallback
    - 100文字以内に制限
    """
    t = text.strip().lower()
    # 全角英数の簡易正規化（必要十分ではないが実用上OK）
    t = t.translate(str.maketrans({
        'Ａ':'a','Ｂ':'b','Ｃ':'c','Ｄ':'d','Ｅ':'e','Ｆ':'f','Ｇ':'g','Ｈ':'h','Ｉ':'i','Ｊ':'j',
        'Ｋ':'k','Ｌ':'l','Ｍ':'m','Ｎ':'n','Ｏ':'o','Ｐ':'p','Ｑ':'q','Ｒ':'r','Ｓ':'s','Ｔ':'t',
        'Ｕ':'u','Ｖ':'v','Ｗ':'w','Ｘ':'x','Ｙ':'y','Ｚ':'z',
        '０':'0','１':'1','２':'2','３':'3','４':'4','５':'5','６':'6','７':'7','８':'8','９':'9',
        '－':'-','＿':'_','　':' '
    }))
    t = re.sub(r"\s+", "-", t)                 # 空白→-
    t = re.sub(r"[^a-z0-9\-_]", "", t)         # 許可外除去
    t = t.strip("-_")
    if not t:
        t = fallback
    return t[:100]

# ====== 起動時処理 ======
@bot.event
async def setup_hook():
    # 永続View（再起動後もボタン動作）
    bot.add_view(TicketView())
    # ギルドコマンド同期（安定）
    synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"✅ Synced {len(synced)} guild commands to {GUILD_ID}: {[c.name for c in synced]}")

@bot.event
async def on_ready():
    print(f"✅ Bot connected as {bot.user}")
    # 再起動時：現在VCに居る人を起点登録
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for m in vc.members:
                if m.bot:
                    continue
                key = (guild.id, m.id)
                if key not in vc_start_times:
                    vc_start_times[key] = (vc.id, datetime.now(timezone.utc))

# ====== VCログ記録 ======
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return

    now_utc = datetime.now(timezone.utc)
    guild_id = member.guild.id
    key = (guild_id, member.id)

    # 退出 or 移動で前セッションを締める
    if before.channel and (after.channel is None or after.channel.id != before.channel.id):
        if key in vc_start_times:
            start_channel_id, joined_at_utc = vc_start_times.get(key)
            ch_id = before.channel.id if before.channel else start_channel_id
            category_id = before.channel.category.id if (before.channel and before.channel.category) else None
            append_vc_log(guild_id, member.id, ch_id, category_id, joined_at_utc, now_utc)
            vc_start_times.pop(key, None)

    # 入室 or 移動で新セッション開始
    if after.channel and (before.channel is None or after.channel.id != before.channel.id):
        vc_start_times[key] = (after.channel.id, now_utc)

# ====== モーダル（チケット名入力） ======
class NameInputModal(discord.ui.Modal, title="チケット名を入力"):
    def __init__(self, author: discord.Member, category_id: int):
        super().__init__(timeout=None)
        self.author = author
        self.category_id = category_id

        self.ticket_name = discord.ui.TextInput(
            label="チケット名（例：返品相談 / 通信トラブル / 見積もり依頼 など）",
            placeholder="例：返品相談",
            required=True,
            min_length=1,
            max_length=60
        )
        self.add_item(self.ticket_name)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        category = guild.get_channel(self.category_id)
        if not isinstance(category, discord.CategoryChannel):
            # フォールバック：現在のチャンネルのカテゴリ
            category = interaction.channel.category
            if category is None:
                await interaction.response.send_message("⚠️ カテゴリーが取得できませんでした。カテゴリ内で実行してください。", ephemeral=True)
                return

        author = self.author
        base_left = slugify_for_channel(category.name) or "category"
        custom_mid = slugify_for_channel(str(self.ticket_name.value), "ticket")

        base_name = f"{base_left}-{custom_mid}"

        # 既存チャンネル数から連番を割り当て（先頭一致）
        existing = [ch for ch in category.text_channels if ch.name.startswith(base_name)]
        count = len(existing) + 1
        channel_name = f"{base_name}-{count}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            author: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.get_role(SUPPORT_ROLE_ID): discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }

        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"{author.name} の問い合わせチケット（{self.ticket_name.value}）"
        )

        await interaction.response.send_message(f"✅ チケットを作成しました: {channel.mention}", ephemeral=True)
        await channel.send(
            f"{author.mention} 「{self.ticket_name.value}」についての問い合わせを受け付けました。内容を送信してください。担当者が対応します。",
            view=CloseTicketView(author)
        )

# ====== チケット作成ボタンのView ======
class TicketView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📉 チケットを作成", style=discord.ButtonStyle.green, custom_id="create_ticket")
    async def create_ticket(self, interaction: discord.Interaction, button: Button):
        current_channel = interaction.channel
        category = current_channel.category
        if category is None:
            await interaction.response.send_message("⚠️ このチャンネルはカテゴリーに属していません。", ephemeral=True)
            return

        # ここでモーダル表示 → 入力値でチャンネルを作成
        await interaction.response.send_modal(NameInputModal(author=interaction.user, category_id=category.id))

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
        safe_name = re.sub(r'[\\/:*?\"<>|]', '_', self.user.name)
        filename = f"{safe_name}_log.txt"

        with open(filename, "w", encoding="utf-8") as f:
            for msg in messages:
                timestamp = msg.created_at.astimezone(JST).strftime("%Y/%m/%d %H:%M")
                content = msg.content or ""
                f.write(f"[{timestamp}] {msg.author.display_name}: {content}\n")

        log_channel = interaction.client.get_channel(LOG_CHANNEL_ID)
        embed = discord.Embed(
            title="📉 問い合わせチケットログ",
            description=f"{self.user.mention} の問い合わせチャンネルが終了しました。",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
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

# ====== /voicetime コマンド（柔軟日付対応） ======
@bot.tree.command(
    name="voicetime",
    description="指定ユーザーのVC滞在時間を集計（チャンネル or カテゴリ & 期間）",
    guild=discord.Object(id=GUILD_ID)
)
@app_commands.describe(
    user="対象ユーザー",
    channel="対象のボイスチャンネル（どちらか一方を指定）",
    category="対象のカテゴリー（どちらか一方を指定）",
    start_at="開始（JST） 'YYYY-MM-DD' も可（省略時00:00）",
    end_at="終了（JST） 'YYYY-MM-DD' も可（省略時23:59）"
)
async def voicetime(
    interaction: discord.Interaction,
    user: discord.Member,
    channel: Optional[discord.VoiceChannel] = None,
    category: Optional[discord.CategoryChannel] = None,
    start_at: str = "",
    end_at: str = ""
):
    await interaction.response.defer(ephemeral=True)

    if (channel is None and category is None) or (channel and category):
        await interaction.followup.send("⚠️ `channel` または `category` の**どちらか一方**だけを指定してください。", ephemeral=True)
        return

    if not start_at or not end_at:
        await interaction.followup.send("⚠️ `start_at` と `end_at` は 'YYYY-MM-DD' または 'YYYY-MM-DD HH:MM' で指定してください（JST）。", ephemeral=True)
        return

    try:
        start_jst = parse_jst(start_at, is_start=True)
        end_jst = parse_jst(end_at, is_start=False)
    except ValueError as e:
        await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
        return

    if end_jst <= start_jst:
        await interaction.followup.send("⚠️ `end_at` は `start_at` より後の時刻にしてください。", ephemeral=True)
        return

    # 集計区間（UTC）
    range_start_utc = start_jst.astimezone(timezone.utc)
    range_end_utc = end_jst.astimezone(timezone.utc)

    data = load_vc_logs()
    gkey = str(interaction.guild_id)
    logs = data.get(gkey, [])

    target_channel_id = channel.id if channel else None
    target_category_id = category.id if category else None

    total_seconds = 0.0
    matched_count = 0
    samples = []

    # 永続ログから集計
    for item in logs:
        if int(item["user_id"]) != user.id:
            continue
        if target_channel_id is not None:
            if int(item["channel_id"]) != target_channel_id:
                continue
        else:
            if item.get("category_id") is None:
                continue
            if int(item["category_id"]) != target_category_id:
                continue

        try:
            j = datetime.fromisoformat(item["join"]).astimezone(timezone.utc)
            l = datetime.fromisoformat(item["leave"]).astimezone(timezone.utc)
        except Exception:
            continue

        sec = overlap_seconds(j, l, range_start_utc, range_end_utc)
        if sec > 0:
            matched_count += 1
            total_seconds += sec
            sj = j.astimezone(JST).strftime("%Y-%m-%d %H:%M")
            sl = l.astimezone(JST).strftime("%Y-%m-%d %H:%M")
            samples.append(f"- {sj} ～ {sl} （{int(sec//60)}分）")

    # 滞在中のケースも考慮（起点あり・終点なし）
    key = (interaction.guild_id, user.id)
    if key in vc_start_times:
        live_ch_id, live_join_utc = vc_start_times[key]
        # フィルタ適用
        if (target_channel_id is not None and live_ch_id == target_channel_id) or \
           (target_category_id is not None and (
               (ch := interaction.guild.get_channel(live_ch_id)) and ch and ch.category and ch.category.id == target_category_id
           )):
            now_utc = datetime.now(timezone.utc)
            sec = overlap_seconds(live_join_utc, now_utc, range_start_utc, range_end_utc)
            if sec > 0:
                matched_count += 1
                total_seconds += sec
                sj = live_join_utc.astimezone(JST).strftime("%Y-%m-%d %H:%M")
                sl = min(now_utc, range_end_utc).astimezone(JST).strftime("%Y-%m-%d %H:%M")
                samples.append(f"- {sj} ～ {sl} （{int(sec//60)}分）※滞在中")

    minutes = int(total_seconds // 60)
    hours = minutes // 60
    mins = minutes % 60

    target_label = channel.mention if channel else f"カテゴリ: {category.name}"
    title = f"⏱️ VC滞在時間 | {user.display_name}"
    period = f"{start_jst.strftime('%Y-%m-%d %H:%M')} ～ {end_jst.strftime('%Y-%m-%d %H:%M')}（JST）"
    summary = f"対象: {target_label}\n期間: {period}\n一致ログ: {matched_count} 件\n合計: **{minutes} 分**（約 {hours:02d}:{mins:02d}）"

    if samples:
        body = "\n".join(samples[:10])  # サンプル最大10件表示
        msg = f"**{title}**\n{summary}\n\n内訳（抜粋）:\n{body}"
    else:
        msg = f"**{title}**\n{summary}\n\n一致する滞在履歴は見つかりませんでした。"

    await interaction.followup.send(msg, ephemeral=True)

# ====== Bot起動 ======
bot.run(os.getenv("DISCORD_TOKEN"))
