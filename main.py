import discord
from discord.ext import commands
from discord.ui import View, Button
from discord import app_commands
import asyncio
import os
import re
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Tuple, List
from openai import OpenAI
from dotenv import load_dotenv
import unicodedata

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ====== 設定 ======
SUPPORT_ROLE_ID = 1398724601256874014
LOG_CHANNEL_ID = 1402874246786711572
GUILD_ID = 1398607685158440991  # サーバーID

# 公開防止のためリポジトリ直下に置かない
VC_LOG_FILE = "logs/vc_logs.json"
TICKET_LABELS_FILE = "logs/ticket_labels.json"  # ← 追加: 永続ビュー用
JST = timezone(timedelta(hours=9))

# ====== Bot初期化 ======
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True   # VC監視に必須
intents.members = True        # Member 引数に推奨
bot = commands.Bot(command_prefix="!", intents=intents)

# 参加中状態の一時保持: { (guild_id, user_id): (channel_id, joined_at_utc) }
vc_start_times: Dict[Tuple[int, int], Tuple[int, datetime]] = {}

# ====== ユーティリティ（ファイル系） ======
def ensure_log_dir():
    os.makedirs(os.path.dirname(VC_LOG_FILE), exist_ok=True)
    os.makedirs(os.path.dirname(TICKET_LABELS_FILE), exist_ok=True)

def load_json_safe(path: str, default):
    ensure_log_dir()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def save_json_safe(path: str, data):
    ensure_log_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_vc_logs():
    return load_json_safe(VC_LOG_FILE, {})

def save_vc_logs(data):
    save_json_safe(VC_LOG_FILE, data)

def load_ticket_labels() -> List[str]:
    data = load_json_safe(TICKET_LABELS_FILE, {"labels": []})
    labels = data.get("labels", [])
    # 既定ラベルを最低1つは保持
    if "問い合わせ" not in labels:
        labels.append("問い合わせ")
        save_json_safe(TICKET_LABELS_FILE, {"labels": labels})
    return labels

def add_ticket_label(label: str):
    data = load_json_safe(TICKET_LABELS_FILE, {"labels": []})
    labels = data.get("labels", [])
    if label not in labels:
        labels.append(label)
        save_json_safe(TICKET_LABELS_FILE, {"labels": labels})

# ====== ユーティリティ（VCログ） ======
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

# ====== チャンネル名スラグ化 ======
_channel_name_allowed = re.compile(r"[a-z0-9\-ぁ-んァ-ヴー一-龯]+")

def slugify_label(label: str) -> str:
    """
    Discordのチャンネル名は小文字・英数字・ハイフン推奨。
    日本語は可だが、記号や絵文字は除去。空白はハイフンに。
    """
    # 全角英数を半角へ
    norm = unicodedata.normalize("NFKC", label.strip())
    # 空白→ハイフン
    norm = re.sub(r"\s+", "-", norm)
    # 許容文字以外は削除（日本語はざっくり許容）
    parts = re.findall(_channel_name_allowed, norm.lower())
    s = "-".join(parts) if parts else "ticket"
    # ハイフン重複の圧縮
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "ticket"

# ====== 起動時処理 ======
@bot.event
async def setup_hook():
    # 既知のすべてのラベルで永続Viewを再登録
    for lb in load_ticket_labels():
        bot.add_view(TicketView(lb))  # timeout=None の永続ビュー
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

# ====== チケット作成ボタンのView（ラベル対応） ======
class TicketView(View):
    """
    生成時に ticket_label を受け取り、その内容を custom_id に埋め込んだボタンを1つ追加。
    再起動後も TICKET_LABELS_FILE に保存された label ごとに add_view して永続化。
    """
    def __init__(self, ticket_label: str):
        super().__init__(timeout=None)
        self.ticket_label = ticket_label  # 元ラベル（表示用）
        self.slug = slugify_label(ticket_label)  # チャンネル名用

        btn = Button(
            label=f"📉 {self.ticket_label} チケットを作成",
            style=discord.ButtonStyle.green,
            custom_id=f"create_ticket::{self.slug}"  # 永続識別子
        )

        async def _callback(interaction: discord.Interaction):
            await self.create_ticket(interaction)

        btn.callback = _callback
        self.add_item(btn)

    async def create_ticket(self, interaction: discord.Interaction):
        guild = interaction.guild
        author = interaction.user
        current_channel = interaction.channel
        category = current_channel.category

        if category is None:
            await interaction.response.send_message("⚠️ このチャンネルはカテゴリーに属していません。", ephemeral=True)
            return

        base_name = f"{category.name}-{self.slug}"
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
            topic=f"{author.name} の「{self.ticket_label}」チケット"
        )

        await interaction.response.send_message(f"✅ チケットを作成しました: {channel.mention}", ephemeral=True)
        await channel.send(
            f"{author.mention} 「**{self.ticket_label}**」に関する問い合わせ内容を送信してください。担当者が対応します。",
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

# ====== /ticketa コマンド（ラベル引数付き） ======
@bot.tree.command(
    name="ticketa",
    description="問い合わせ用チケット作成ボタンを送信します（label で名称変更可）",
    guild=discord.Object(id=GUILD_ID)
)
@app_commands.describe(
    label="ボタン/チケット名の中央部分（既定: 問い合わせ） 例: 採用, 営業, サポート"
)
async def ticketa(interaction: discord.Interaction, label: Optional[str] = "問い合わせ"):
    if not any(role.id == SUPPORT_ROLE_ID for role in interaction.user.roles):
        await interaction.response.send_message("❌ このコマンドを使用する権限がありません。", ephemeral=True)
        return

    # 永続化用に記録しておく（再起動後の persistent view 再登録に使用）
    add_ticket_label(label)

    view = TicketView(label)
    await interaction.response.send_message(
        f"以下のボタンから「**{label}**」チケットを作成できます。",
        view=view
    )

# ====== /voicetime コマンド（柔軟日付対応：既存） ======
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
