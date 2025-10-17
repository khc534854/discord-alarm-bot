import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
import aiosqlite



# --- 환경 ---
load_dotenv(override=True)
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Seoul"))

UTC = ZoneInfo("UTC")   # ✅ 이 한 줄 추가

INTENTS = discord.Intents.default()
INTENTS.message_content = True  # /commands는 없어도 되지만, 예비용으로 켭니다.

class AlarmBot(discord.Client):
    def __init__(self):
        super().__init__(intents=INTENTS)
        self.tree = app_commands.CommandTree(self)
        self.db_path = os.getenv("DB_PATH", "alarms.sqlite3")

    async def setup_hook(self):
        # 슬래시 커맨드 동기화
        await self.tree.sync()
        # DB 초기화
        await self._init_db()
        # 백그라운드 태스크 시작
        self.check_alarms.start()

    async def _init_db(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS alarms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    channel_id INTEGER,
                    user_id INTEGER,
                    run_at TEXT,          -- ISO string (UTC)
                    message TEXT,
                    sent INTEGER DEFAULT 0
                );
            """)
            await db.commit()

    @tasks.loop(seconds=60)
    async def check_alarms(self):
        # 권장 방식: 타임존 인식 UTC now
        now_utc = datetime.now(UTC)
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("""
                SELECT id, guild_id, channel_id, user_id, run_at, message
                FROM alarms
                WHERE sent = 0
            """) as cursor:
                rows = await cursor.fetchall()

            to_send = []
            for rid, guild_id, channel_id, user_id, run_at, message in rows:
                run_dt = datetime.fromisoformat(run_at)
                # run_at이 tz 미포함으로 저장된 예외 상황 대비
                if run_dt.tzinfo is None:
                    run_dt = run_dt.replace(tzinfo=UTC)

                if run_dt <= now_utc:
                    to_send.append((rid, guild_id, channel_id, user_id, message))

            for rid, guild_id, channel_id, user_id, message in to_send:
                channel = self.get_channel(channel_id)
                if channel:
                    try:
                        await channel.send(f"<@{user_id}> 알람: {message}")
                    except Exception as e:
                        print(f"Send failed (alarm {rid}): {e}")

                await db.execute("UPDATE alarms SET sent = 1 WHERE id = ?", (rid,))
            if to_send:
                await db.commit()

    @check_alarms.before_loop
    async def before_check(self):
        await self.wait_until_ready()


client = AlarmBot()

# ---- /ping : 상태 확인 ----
@client.tree.command(name="ping", description="봇 응답 지연(ms)을 표시합니다.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(client.latency*1000)}ms", ephemeral=True)

# ---- /alarm_in : 지금부터 n분 뒤 알람 등록 ----
@client.tree.command(name="alarm_in", description="지금부터 N분 뒤 알람을 등록합니다.")
@app_commands.describe(minutes="몇 분 뒤?", message="알람 메시지")
async def alarm_in(interaction: discord.Interaction, minutes: int, message: str):
    if minutes <= 0:
        await interaction.response.send_message("분(minute)은 1 이상이어야 합니다.", ephemeral=True)
        return

    run_local = datetime.now(TZ) + timedelta(minutes=minutes)
    run_utc = run_local.astimezone(ZoneInfo("UTC"))

    async with aiosqlite.connect(client.db_path) as db:
        await db.execute("""
            INSERT INTO alarms (guild_id, channel_id, user_id, run_at, message, sent)
            VALUES (?, ?, ?, ?, ?, 0)
        """, (
            interaction.guild_id,
            interaction.channel_id,
            interaction.user.id,
            run_utc.isoformat(),
            message
        ))
        await db.commit()

    await interaction.response.send_message(
        f"알람 등록 완료: **{run_local.strftime('%Y-%m-%d %H:%M:%S')} (Asia/Seoul)** 에 알림을 보냅니다.",
        ephemeral=True
    )

# ---- /alarm_at : yyyy-mm-dd HH:MM 로 특정 시각 알람 ----
@client.tree.command(name="alarm_at", description="특정 시각(Asia/Seoul)에 알람을 등록합니다. 예: 2025-10-17 15:30")
@app_commands.describe(when="예: 2025-10-17 15:30", message="알람 메시지")
async def alarm_at(interaction: discord.Interaction, when: str, message: str):
    try:
        # 입력은 서울 시간 기준
        run_local = datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except ValueError:
        await interaction.response.send_message("형식 오류. 예: `2025-10-17 15:30`", ephemeral=True)
        return

    if run_local <= datetime.now(TZ):
        await interaction.response.send_message("과거 시각은 등록할 수 없습니다.", ephemeral=True)
        return

    run_utc = run_local.astimezone(ZoneInfo("UTC"))
    async with aiosqlite.connect(client.db_path) as db:
        await db.execute("""
            INSERT INTO alarms (guild_id, channel_id, user_id, run_at, message, sent)
            VALUES (?, ?, ?, ?, ?, 0)
        """, (
            interaction.guild_id,
            interaction.channel_id,
            interaction.user.id,
            run_utc.isoformat(),
            message
        ))
        await db.commit()

    await interaction.response.send_message(
        f"알람 등록 완료: **{run_local.strftime('%Y-%m-%d %H:%M:%S')} (Asia/Seoul)** 에 알림을 보냅니다.",
        ephemeral=True
    )

# ---- /alarms : 내 알람 목록 ----
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

# ---- /alarm_cancel : 알람 ID로 취소 ----
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

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("환경변수 DISCORD_TOKEN 이 설정되지 않았습니다.")
    client.run(TOKEN)
