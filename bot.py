import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
import aiosqlite

# ── 환경 로드 ────────────────────────────────────────────────────────────────
load_dotenv(override=True)
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Seoul"))
UTC = ZoneInfo("UTC")  # Python 3.13 호환

INTENTS = discord.Intents.default()
INTENTS.message_content = True  # 메시지 콘텐츠 인텐트(필요 시)

# ── 클라이언트 ──────────────────────────────────────────────────────────────
class AlarmBot(discord.Client):
    def __init__(self):
        super().__init__(intents=INTENTS)
        self.tree = app_commands.CommandTree(self)
        self.db_path = os.getenv("DB_PATH", "alarms.sqlite3")

    async def setup_hook(self):
        await self.tree.sync()
        await self._init_db()
        self.check_alarms.start()

    async def _init_db(self):
        async with aiosqlite.connect(self.db_path) as db:
            # 일회성 알람
            await db.execute("""
                CREATE TABLE IF NOT EXISTS alarms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    channel_id INTEGER,
                    user_id INTEGER,
                    run_at TEXT,          -- ISO8601(UTC)
                    message TEXT,
                    sent INTEGER DEFAULT 0
                );
            """)
            # 반복 알람(매일 HH:MM)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS recurring_alarms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    channel_id INTEGER,
                    user_id INTEGER,
                    at_hour INTEGER,            -- 0~23
                    at_minute INTEGER,          -- 0~59
                    message TEXT,
                    enabled INTEGER DEFAULT 1,
                    last_sent_local_date TEXT   -- 'YYYY-MM-DD' (TZ 기준)
                );
            """)
            # 마이그레이션: @everyone 사용 여부 컬럼
            try:
                await db.execute("ALTER TABLE recurring_alarms ADD COLUMN ping_everyone INTEGER DEFAULT 0;")
            except Exception:
                pass
            await db.commit()

    @tasks.loop(seconds=60)
    async def check_alarms(self):
        """일회성 + 반복 알람 전송 루프(60초 간격)."""
        now_utc = datetime.now(UTC)
        now_local = now_utc.astimezone(TZ)
        today_str = now_local.strftime("%Y-%m-%d")

        async with aiosqlite.connect(self.db_path) as db:
            # ── 일회성 알람 처리 ─────────────────────────────────────────────
            async with db.execute("""
                SELECT id, guild_id, channel_id, user_id, run_at, message
                FROM alarms
                WHERE sent = 0
            """) as cursor:
                rows = await cursor.fetchall()

            to_send_once = []
            for rid, guild_id, channel_id, user_id, run_at, message in rows:
                run_dt = datetime.fromisoformat(run_at)
                if run_dt.tzinfo is None:
                    run_dt = run_dt.replace(tzinfo=UTC)
                if run_dt <= now_utc:
                    to_send_once.append((rid, guild_id, channel_id, user_id, message))

            for rid, guild_id, channel_id, user_id, message in to_send_once:
                channel = self.get_channel(channel_id)
                if channel:
                    try:
                        await channel.send(f"<@{user_id}> 알람: {message}")
                    except Exception as e:
                        print(f"Send failed (alarm {rid}): {e}")
                await db.execute("UPDATE alarms SET sent = 1 WHERE id = ?", (rid,))
            if to_send_once:
                await db.commit()

            # ── 반복 알람(매일 HH:MM) 처리 ──────────────────────────────────
            async with db.execute("""
                SELECT id, guild_id, channel_id, user_id, at_hour, at_minute, message,
                       enabled, COALESCE(last_sent_local_date, ''), COALESCE(ping_everyone, 0)
                FROM recurring_alarms
                WHERE enabled = 1
            """) as rc:
                rrows = await rc.fetchall()

            for rid, guild_id, channel_id, user_id, at_hour, at_minute, message, enabled, last_sent, ping_everyone in rrows:
                target_local = now_local.replace(hour=at_hour, minute=at_minute, second=0, microsecond=0)
                # 오늘 아직 발송 안 했고, 목표 시각이 지났으면 발송
                if last_sent != today_str and now_local >= target_local:
                    channel = self.get_channel(channel_id)
                    if channel:
                        try:
                            if int(ping_everyone) == 1:
                                text = f"@everyone 알람(매일 {at_hour:02d}:{at_minute:02d}) : {message}"
                                allowed = discord.AllowedMentions(everyone=True, users=False, roles=False)
                            else:
                                text = f"<@{user_id}> 알람(매일 {at_hour:02d}:{at_minute:02d}) : {message}"
                                allowed = discord.AllowedMentions(everyone=False, users=True, roles=False)
                            await channel.send(text, allowed_mentions=allowed)
                        except Exception as e:
                            print(f"Recurring send failed (id {rid}): {e}")
                    await db.execute("""
                        UPDATE recurring_alarms
                        SET last_sent_local_date = ?
                        WHERE id = ?
                    """, (today_str, rid))

            await db.commit()

    @check_alarms.before_loop
    async def before_check(self):
        await self.wait_until_ready()

# ── 인스턴스 ────────────────────────────────────────────────────────────────
client = AlarmBot()

# ── 명령어: 상태 ────────────────────────────────────────────────────────────
@client.tree.command(name="ping", description="봇 응답 지연(ms)을 표시합니다.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(client.latency * 1000)}ms", ephemeral=True)

# ── 명령어: 일회성 알람(분 뒤) ─────────────────────────────────────────────
@client.tree.command(name="alarm_in", description="지금부터 N분 뒤 알람을 등록합니다.")
@app_commands.describe(minutes="몇 분 뒤?", message="알람 메시지")
async def alarm_in(interaction: discord.Interaction, minutes: int, message: str):
    if minutes <= 0:
        await interaction.response.send_message("분(minute)은 1 이상이어야 합니다.", ephemeral=True)
        return

    run_local = datetime.now(TZ) + timedelta(minutes=minutes)
    run_utc = run_local.astimezone(UTC)

    async with aiosqlite.connect(client.db_path) as db:
        await db.execute("""
            INSERT INTO alarms (guild_id, channel_id, user_id, run_at, message, sent)
            VALUES (?, ?, ?, ?, ?, 0)
        """, (interaction.guild_id, interaction.channel_id, interaction.user.id, run_utc.isoformat(), message))
        await db.commit()

    await interaction.response.send_message(
        f"알람 등록 완료: **{run_local.strftime('%Y-%m-%d %H:%M:%S')} (Asia/Seoul)** 에 알림을 보냅니다.",
        ephemeral=True
    )

# ── 명령어: 일회성 알람(특정 시각) ─────────────────────────────────────────
@client.tree.command(name="alarm_at", description="특정 시각(Asia/Seoul)에 알람을 등록합니다. 예: 2025-10-17 15:30")
@app_commands.describe(when="예: 2025-10-17 15:30", message="알람 메시지")
async def alarm_at(interaction: discord.Interaction, when: str, message: str):
    try:
        run_local = datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except ValueError:
        await interaction.response.send_message("형식 오류. 예: `2025-10-17 15:30`", ephemeral=True)
        return

    if run_local <= datetime.now(TZ):
        await interaction.response.send_message("과거 시각은 등록할 수 없습니다.", ephemeral=True)
        return

    run_utc = run_local.astimezone(UTC)
    async with aiosqlite.connect(client.db_path) as db:
        await db.execute("""
            INSERT INTO alarms (guild_id, channel_id, user_id, run_at, message, sent)
            VALUES (?, ?, ?, ?, ?, 0)
        """, (interaction.guild_id, interaction.channel_id, interaction.user.id, run_utc.isoformat(), message))
        await db.commit()

    await interaction.response.send_message(
        f"알람 등록 완료: **{run_local.strftime('%Y-%m-%d %H:%M:%S')} (Asia/Seoul)** 에 알림을 보냅니다.",
        ephemeral=True
    )

# ── 명령어: 일회성 알람 목록 ───────────────────────────────────────────────
@client.tree.command(name="alarms", description="내가 등록한 대기 중 알람을 조회합니다.")
async def alarms(interaction: discord.Interaction):
    async with aiosqlite.connect(client.db_path) as db:
        async with db.execute("""
            SELECT id, run_at, message FROM alarms
            WHERE sent = 0 AND user_id = ? AND guild_id = ?
            ORDER BY run_at ASC
        """, (interaction.user.id, interaction.guild_id)) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await interaction.response.send_message("대기 중 알람이 없습니다.", ephemeral=True)
        return

    lines = []
    for rid, run_at, message in rows:
        run_dt = datetime.fromisoformat(run_at)
        if run_dt.tzinfo is None:
            run_dt = run_dt.replace(tzinfo=UTC)
        run_local = run_dt.astimezone(TZ)
        lines.append(f"`#{rid}`  {run_local.strftime('%Y-%m-%d %H:%M:%S')}  - {message}")

    await interaction.response.send_message("**등록된 알람**\n" + "\n".join(lines), ephemeral=True)

# ── 명령어: 일회성 알람 취소 ───────────────────────────────────────────────
@client.tree.command(name="alarm_cancel", description="알람 ID로 취소합니다. /alarms로 ID 확인")
@app_commands.describe(alarm_id="취소할 알람의 ID")
async def alarm_cancel(interaction: discord.Interaction, alarm_id: int):
    async with aiosqlite.connect(client.db_path) as db:
        await db.execute("""
            DELETE FROM alarms
            WHERE id = ? AND user_id = ? AND guild_id = ? AND sent = 0
        """, (alarm_id, interaction.user.id, interaction.guild_id))
        changes = db.total_changes
        await db.commit()

    if changes == 0:
        await interaction.response.send_message("해당 ID의 대기 중 알람이 없습니다.", ephemeral=True)
    else:
        await interaction.response.send_message(f"알람 #{alarm_id} 취소 완료.", ephemeral=True)

# ── 명령어: 매일 20:00(개인 멘션) ─────────────────────────────────────────
@client.tree.command(name="alarm_daily20", description="매일 20:00(Asia/Seoul) 알람을 현재 채널에 등록합니다.")
@app_commands.describe(message="알람 메시지")
async def alarm_daily20(interaction: discord.Interaction, message: str):
    async with aiosqlite.connect(client.db_path) as db:
        await db.execute("""
            INSERT INTO recurring_alarms
                (guild_id, channel_id, user_id, at_hour, at_minute, message, enabled, last_sent_local_date, ping_everyone)
            VALUES (?, ?, ?, 20, 0, ?, 1, NULL, 0)
        """, (interaction.guild_id, interaction.channel_id, interaction.user.id, message))
        await db.commit()
    await interaction.response.send_message("매일 **20:00 (Asia/Seoul)** 알람을 등록했습니다.", ephemeral=True)

# ── 명령어: 매일 20:00(@everyone) ─────────────────────────────────────────
@client.tree.command(
    name="alarm_daily20_everyone",
    description="매일 20:00(Asia/Seoul) @everyone 알람을 현재 채널에 등록합니다."
)
@app_commands.describe(message="알람 메시지")
async def alarm_daily20_everyone(interaction: discord.Interaction, message: str):
    perms = interaction.channel.permissions_for(interaction.guild.me) if interaction.guild and interaction.channel else None
    if perms is not None and not perms.mention_everyone:
        await interaction.response.send_message("이 채널에서 봇에게 `@everyone` 언급 권한이 없습니다.", ephemeral=True)
        return

    async with aiosqlite.connect(client.db_path) as db:
        await db.execute("""
            INSERT INTO recurring_alarms
                (guild_id, channel_id, user_id, at_hour, at_minute, message, enabled, last_sent_local_date, ping_everyone)
            VALUES (?, ?, ?, 20, 0, ?, 1, NULL, 1)
        """, (interaction.guild_id, interaction.channel_id, interaction.user.id, message))
        await db.commit()
    await interaction.response.send_message("매일 **20:00** `@everyone` 알람을 등록했습니다.", ephemeral=True)

# ── 명령어: 매일 N시 M분 @everyone(커스텀 시각) ───────────────────────────
@client.tree.command(
    name="alarm_daily_everyone",
    description="매일 지정한 시각(Asia/Seoul)에 @everyone 알람을 등록합니다. 예: /alarm_daily_everyone time:21:30 message:테스트"
)
@app_commands.describe(time="시각 (HH:MM 형식, 예: 21:30)", message="알람 메시지")
async def alarm_daily_everyone(interaction: discord.Interaction, time: str, message: str):
    perms = interaction.channel.permissions_for(interaction.guild.me) if interaction.guild and interaction.channel else None
    if perms is not None and not perms.mention_everyone:
        await interaction.response.send_message("이 채널에서 봇에게 `@everyone` 언급 권한이 없습니다.", ephemeral=True)
        return

    try:
        hour, minute = map(int, time.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        await interaction.response.send_message("시간 형식이 올바르지 않습니다. 예: 08:30 또는 23:00", ephemeral=True)
        return

    async with aiosqlite.connect(client.db_path) as db:
        await db.execute("""
            INSERT INTO recurring_alarms
                (guild_id, channel_id, user_id, at_hour, at_minute, message, enabled, last_sent_local_date, ping_everyone)
            VALUES (?, ?, ?, ?, ?, ?, 1, NULL, 1)
        """, (interaction.guild_id, interaction.channel_id, interaction.user.id, hour, minute, message))
        await db.commit()

    await interaction.response.send_message(
        f"매일 **{hour:02d}:{minute:02d} (Asia/Seoul)** `@everyone` 알람을 등록했습니다.",
        ephemeral=True
    )

# ── 명령어: 매일 알람 해제(20:00 고정) ────────────────────────────────────
@client.tree.command(name="alarm_daily20_cancel", description="매일 20:00(Asia/Seoul) 알람을 해제합니다.")
async def alarm_daily20_cancel(interaction: discord.Interaction):
    async with aiosqlite.connect(client.db_path) as db:
        await db.execute("""
            UPDATE recurring_alarms
            SET enabled = 0
            WHERE guild_id = ? AND channel_id = ? AND user_id = ? AND at_hour = 20 AND at_minute = 0 AND enabled = 1
        """, (interaction.guild_id, interaction.channel_id, interaction.user.id))
        changes = db.total_changes
        await db.commit()

    if changes == 0:
        await interaction.response.send_message("해제할 매일 20:00 알람이 없습니다.", ephemeral=True)
    else:
        await interaction.response.send_message("매일 20:00 알람을 해제했습니다.", ephemeral=True)

# ── 명령어: 매일 알람 목록 ────────────────────────────────────────────────
@client.tree.command(name="alarm_daily_list", description="내가 등록한 매일 알람을 확인합니다.")
async def alarm_daily_list(interaction: discord.Interaction):
    async with aiosqlite.connect(client.db_path) as db:
        async with db.execute("""
            SELECT id, at_hour, at_minute, message, enabled,
                   COALESCE(last_sent_local_date,''), COALESCE(ping_everyone,0)
            FROM recurring_alarms
            WHERE guild_id = ? AND user_id = ?
            ORDER BY at_hour, at_minute, id
        """, (interaction.guild_id, interaction.user.id)) as c:
            rows = await c.fetchall()

    if not rows:
        await interaction.response.send_message("등록된 매일 알람이 없습니다.", ephemeral=True)
        return

    lines = []
    for rid, h, m, msg, enabled, last_sent, ping_everyone in rows:
        status = "ON" if enabled else "OFF"
        tag = "@everyone" if int(ping_everyone) == 1 else ""
        lines.append(f"`#{rid}` {h:02d}:{m:02d} [{status}] {tag} - {msg} (last_sent={last_sent or '-'})")

    await interaction.response.send_message("**매일 알람 목록**\n" + "\n".join(lines), ephemeral=True)

# ── 실행 ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("환경변수 DISCORD_TOKEN 이 설정되지 않았습니다.")
    client.run(TOKEN)
