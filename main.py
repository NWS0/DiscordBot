import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp
import asyncio
import json
import os
import sys
import shutil
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

# 1. 設定權限
intents = discord.Intents.default()
intents.message_content = True

class MusicBot(commands.Bot):
    pass

# 2. 建立指令型機器人 (改用真正的 Discord slash commands)
bot = MusicBot(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

# 3. yt-dlp 設定 (告訴它只要抓最好的純音訊就好)
# 加上 reconnect 參數，避免 YouTube 串流中途斷線時提早結束播放。
ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}
script_dir = Path(__file__).resolve().parent
local_ffmpeg = script_dir / "ffmpeg.exe"
restart_state_file = script_dir / ".restart_state.json"
dotenv_candidates = [
    script_dir / "token.env",
    script_dir / ".env",
    script_dir.parent / "token.env",
    script_dir.parent / ".env",
    script_dir.parent.parent / "token.env",
    script_dir.parent.parent / ".env",
]

def load_local_dotenv() -> None:
    found_token_file = False
    for dotenv_path in dotenv_candidates:
        if not dotenv_path.exists():
            continue

        found_token_file = True

        for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

load_local_dotenv()

ffmpeg_path = str(local_ffmpeg) if local_ffmpeg.exists() else shutil.which("ffmpeg") or os.getenv("FFMPEG_PATH")
node_runtime_path = shutil.which("node") or os.getenv("NODE_PATH")
ROLE_REMOVE_CHANNEL_ID = 1513795348206850069  # 改成目標文字頻道 ID；設為 0 代表改用環境變數
ROLE_REMOVE_ROLE_ID = 1518459075254419487  # 改成要移除的身分組 ID；設為 0 代表改用環境變數
role_remove_channel_id_raw = os.getenv("ROLE_REMOVE_CHANNEL_ID", "").strip()
role_remove_role_id_raw = os.getenv("ROLE_REMOVE_ROLE_ID", "").strip()


def parse_optional_int(value: str, *, env_name: str) -> int | None:
    if not value:
        return None

    try:
        return int(value)
    except ValueError:
        print(f"{env_name} 不是有效的數字 ID：{value!r}")
        return None


role_remove_channel_id = ROLE_REMOVE_CHANNEL_ID or parse_optional_int(role_remove_channel_id_raw, env_name="1513795348206850069")
role_remove_role_id = ROLE_REMOVE_ROLE_ID or parse_optional_int(role_remove_role_id_raw, env_name="1518459075254419487")

@dataclass
class QueueItem:
    title: str
    webpage_url: str

@dataclass
class GuildState:
    queue: deque[QueueItem] = field(default_factory=deque)
    text_channel_id: int | None = None
    current_track: QueueItem | None = None  # 【新增】記錄目前正在播放的歌曲
    is_looping: bool = False                # 【新增】記錄是否開啟單曲重播
    is_shuffling: bool = False               # 【預留】之後可擴充成隨機播放

guild_states: dict[int, GuildState] = {}
guild_locks: dict[int, asyncio.Lock] = {}

def get_guild_state(guild_id: int) -> GuildState:
    state = guild_states.get(guild_id)
    if state is None:
        state = GuildState()
        guild_states[guild_id] = state
    return state

def get_guild_lock(guild_id: int) -> asyncio.Lock:
    lock = guild_locks.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        guild_locks[guild_id] = lock
    return lock


async def purge_stale_application_commands() -> None:
    local_command_names = {command.name for command in bot.tree.get_commands(type=discord.AppCommandType.chat_input)}

    for remote_command in await bot.tree.fetch_commands():
        if remote_command.name not in local_command_names:
            try:
                await remote_command.delete()
            except Exception as error:
                print(f"刪除全域舊指令失敗：{remote_command.name} / {error}")

    for guild in bot.guilds:
        for remote_command in await bot.tree.fetch_commands(guild=guild):
            if remote_command.name not in local_command_names:
                try:
                    await remote_command.delete()
                except Exception as error:
                    print(f"刪除伺服器舊指令失敗：{guild.name} / {remote_command.name} / {error}")


async def ensure_voice_connected(guild: discord.Guild, channel: discord.VoiceChannel) -> None:
    voice_client = guild.voice_client
    try:
        if voice_client:
            if voice_client.channel != channel:
                await voice_client.move_to(channel)
        else:
            await channel.connect()
    except asyncio.TimeoutError as error:
        if guild.voice_client:
            try:
                await guild.voice_client.disconnect(force=True)
            except Exception:
                pass
        raise RuntimeError("語音連線逾時，請確認機器人有加入語音頻道與連線的權限，或稍後再試。") from error

def build_ytdl_options(*, no_playlist: bool) -> dict:
    options = {
        'format': 'bestaudio/best',
        'noplaylist': no_playlist,
        'remote_components': {'ejs:github'},
    }
    if node_runtime_path:
        options['js_runtimes'] = {'node': {'path': node_runtime_path}}
    return options

async def extract_youtube_info(url: str, *, no_playlist: bool) -> dict:
    loop = asyncio.get_running_loop()

    def _extract() -> dict:
        with yt_dlp.YoutubeDL(build_ytdl_options(no_playlist=no_playlist)) as downloader:
            return downloader.extract_info(url, download=False)

    return await loop.run_in_executor(None, _extract)

async def send_music_message(guild: discord.Guild, message: str) -> None:
    state = guild_states.get(guild.id)
    channel = guild.get_channel(state.text_channel_id) if state and state.text_channel_id else None
    if channel is None:
        channel = guild.system_channel
    if channel is None:
        channel = next((text_channel for text_channel in guild.text_channels if text_channel.permissions_for(guild.me).send_messages), None)
    if channel is not None:
        await channel.send(message)


async def remove_role_on_message(message: discord.Message) -> None:
    if message.guild is None or message.author.bot:
        return

    if role_remove_channel_id is None or role_remove_role_id is None:
        return

    if message.channel.id != role_remove_channel_id:
        return

    role = message.guild.get_role(role_remove_role_id)
    if role is None:
        print(f"找不到要移除的身分組：{role_remove_role_id}")
        return

    member = message.author if isinstance(message.author, discord.Member) else None
    if member is None:
        try:
            member = await message.guild.fetch_member(message.author.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    if role not in member.roles:
        return

    try:
        await member.remove_roles(role, reason=f"在指定頻道 {message.channel.id} 發言自動移除身分組")
        print(f"已從 {member} 移除身分組 {role.name}，因為他在頻道 {message.channel.id} 發言")
    except discord.Forbidden:
        print("機器人沒有權限移除該身分組，或角色階級不夠高。")
    except discord.HTTPException as error:
        print(f"移除身分組失敗：{error}")

async def play_next_track(guild_id: int) -> None:
    guild = bot.get_guild(guild_id)
    if guild is None:
        return

    state = guild_states.get(guild_id)
    if state is None:
        return

    voice_client = guild.voice_client
    if voice_client is None:
        state.queue.clear()
        return

    if voice_client.is_playing() or voice_client.is_paused():
        return

    async with get_guild_lock(guild_id):
        voice_client = guild.voice_client
        if voice_client is None or voice_client.is_playing() or voice_client.is_paused():
            return

        while state.queue:
            item = state.queue.popleft()
            state.current_track = item  # 【新增】記下當前正在播放的歌曲
            
            try:
                info = await extract_youtube_info(item.webpage_url, no_playlist=True)
                stream_url = info['url']
                title = info.get('title', item.title)
            except Exception as error:
                await send_music_message(guild, f"❌ 抓取音源失敗：{error}")
                continue

            def after_playback(error: Exception | None) -> None:
                if error:
                    print(f"播放錯誤：{error}")

                def schedule_next() -> None:
                    if state.is_looping and state.current_track:
                        state.queue.appendleft(QueueItem(
                            title=state.current_track.title,
                            webpage_url=state.current_track.webpage_url,
                        ))
                    else:
                        state.current_track = None

                    asyncio.create_task(play_next_track(guild_id))

                bot.loop.call_soon_threadsafe(schedule_next)

            voice_client.play(
                discord.FFmpegPCMAudio(stream_url, executable=ffmpeg_path, **ffmpeg_options),
                after=after_playback,
            )
            await send_music_message(guild, f"🎶 正在播放：**{title}**")
            return

def get_discord_token() -> str:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("找不到 DISCORD_TOKEN。請確認 token.env 或 .env 不是空的，且內容格式是 DISCORD_TOKEN=你的新 token。")
    return token

async def announce_restart_complete():
    if not restart_state_file.exists():
        return

    try:
        payload = json.loads(restart_state_file.read_text(encoding="utf-8"))
        channel_id = payload.get("channel_id")
        if channel_id is None:
            return

        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)

        await channel.send("✅ 重啟完成")
    except Exception:
        pass
    finally:
        try:
            restart_state_file.unlink()
        except FileNotFoundError:
            pass

@bot.event
async def on_ready():
    if not getattr(bot, "_slash_commands_synced", False):
        await purge_stale_application_commands()

        if bot.guilds:
            for guild in bot.guilds:
                await bot.tree.sync(guild=guild)
        await bot.tree.sync()
        bot._slash_commands_synced = True

    print(f'🎵 音樂精靈 {bot.user} 上線囉！')
    await announce_restart_complete()


@bot.event
async def on_message(message: discord.Message):
    await remove_role_on_message(message)

def build_help_text() -> str:
    return (
        "```\n"
        "༺ · ──────── 一般指令 ──────── · ༻\n"
        "/help - 顯示目前所有指令與簡單說明\n"
        "/join - 讓機器人加入你目前所在的語音頻道\n"
        "/play [url] - 播放指定 YouTube 影片並加入佇列\n"
        "/queue - 查看目前播放佇列\n"
        "/replay - 切換單曲重播模式 (開啟/關閉)\n"  
        "/leave - 讓機器人離開語音頻道\n"
        "༺ · ──────── 管理指令 ──────── · ༻\n"
        "/restart - 重啟 Discord bot\n"
        "/stop - 完全關閉 Discord bot\n"
        "```"
    )

@bot.tree.command(name="help", description="顯示目前所有指令與簡單說明")
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(build_help_text())

@bot.tree.command(name="join", description="讓機器人加入你目前所在的語音頻道")
async def join_command(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ 這個指令只能在伺服器裡使用。", ephemeral=True)
        return

    voice_state = interaction.user.voice
    if not voice_state:
        await interaction.response.send_message("❌ 你必須先加入一個語音頻道喔！", ephemeral=True)
        return

    channel = voice_state.channel
    try:
        await ensure_voice_connected(interaction.guild, channel)
    except RuntimeError as error:
        await interaction.response.send_message(f"❌ {error}", ephemeral=True)
        return

    get_guild_state(interaction.guild.id).text_channel_id = interaction.channel_id
    await interaction.response.send_message(f"✅ 已加入語音頻道：{channel.name}")

@bot.tree.command(name="restart", description="重啟 Discord bot")
@app_commands.checks.has_permissions(administrator=True)
async def restart_command(interaction: discord.Interaction):
    if interaction.channel_id is not None:
        restart_state_file.write_text(json.dumps({"channel_id": interaction.channel_id}), encoding="utf-8")

    await interaction.response.send_message("🔄 正在重啟 bot...")
    await bot.close()
    os.execv(sys.executable, [sys.executable] + sys.argv)

@bot.tree.command(name="stop", description="完全關閉 Discord bot")
@app_commands.checks.has_permissions(administrator=True)
async def stop_command(interaction: discord.Interaction):
    await interaction.response.send_message("🛑 正在完全關閉 bot...")
    await bot.close()

@bot.tree.command(name="play", description="播放指定 YouTube 影片的音訊")
async def play_command(interaction: discord.Interaction, url: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ 這個指令只能在伺服器裡使用。", ephemeral=True)
        return

    server = interaction.guild
    state = get_guild_state(server.id)
    state.text_channel_id = interaction.channel_id

    await interaction.response.defer()

    if not server.voice_client:
        voice_state = interaction.user.voice
        if not voice_state:
            await interaction.followup.send("❌ 你必須先加入一個語音頻道喔！")
            return

        try:
            await ensure_voice_connected(server, voice_state.channel)
        except RuntimeError as error:
            await interaction.followup.send(f"❌ {error}")
            return

    if not ffmpeg_path:
        await interaction.followup.send("❌ 找不到 ffmpeg，請先安裝 ffmpeg 並加入 PATH，或設定 FFMPEG_PATH。")
        return

    try:
        data = await extract_youtube_info(url, no_playlist=True)
    except Exception as error:
        await interaction.followup.send(f"❌ 抓取音源失敗：{error}")
        return

    state.queue.append(QueueItem(title=data.get('title', url), webpage_url=data.get('webpage_url', url)))
    await interaction.followup.send(f"✅ 已加入佇列：**{data.get('title', url)}**")
    await play_next_track(server.id)


@bot.tree.command(name="queue", description="查看目前播放佇列")
async def queue_command(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("❌ 這個指令只能在伺服器裡使用。", ephemeral=True)
        return

    state = guild_states.get(interaction.guild.id)
    if not state:
        await interaction.response.send_message("📭 目前沒有任何播放佇列。")
        return

    lines = []
    if state.current_track:
        lines.append(f"▶ 目前播放：{state.current_track.title}")

    if state.queue:
        lines.append("\n待播清單：")
        for index, item in enumerate(list(state.queue)[:15], start=1):
            lines.append(f"{index}. {item.title}")

        if len(state.queue) > 15:
            lines.append(f"... 還有 {len(state.queue) - 15} 首")
    else:
        lines.append("\n待播清單：空")

    lines.append(f"\n單曲重播：{'開啟' if state.is_looping else '關閉'}")
    await interaction.response.send_message("```\n" + "\n".join(lines) + "\n```")

# 【新增】重播指令
@bot.tree.command(name="replay", description="切換單曲重播模式 (開啟/關閉)")
async def replay_command(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("❌ 這個指令只能在伺服器裡使用。", ephemeral=True)
        return

    state = get_guild_state(interaction.guild.id)
    state.is_looping = not state.is_looping

    status = "開啟 🔁" if state.is_looping else "關閉 ➡"
    note = "，會在下一首歌開始後生效" if state.current_track is None else ""
    await interaction.response.send_message(f"單曲重播模式已 **{status}**{note}")

@play_command.error
async def play_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CommandInvokeError):
        if interaction.response.is_done():
            await interaction.followup.send("❌ 請用法：`/play 影片網址`")
        else:
            await interaction.response.send_message("❌ 請用法：`/play 影片網址`")
        return
    raise error

@restart_command.error
@stop_command.error
async def admin_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        if interaction.response.is_done():
            await interaction.followup.send("❌ 你沒有權限使用這個管理指令。", ephemeral=True)
        else:
            await interaction.response.send_message("❌ 你沒有權限使用這個管理指令。", ephemeral=True)
        return
    raise error

@bot.tree.command(name="leave", description="讓機器人離開語音頻道")
async def leave_command(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("❌ 這個指令只能在伺服器裡使用。", ephemeral=True)
        return

    voice_client = interaction.guild.voice_client
    if voice_client:
        state = guild_states.get(interaction.guild.id)
        if state:
            state.queue.clear()
            state.current_track = None  # 【新增】清空歌曲紀錄
            state.is_looping = False    # 【新增】關閉重播
            
        await voice_client.disconnect()
        await interaction.response.send_message("👋 掰掰，我先走囉！")
    else:
        await interaction.response.send_message("❌ 我現在不在任何語音頻道裡。", ephemeral=True)

# 啟動機器人
bot.run(get_discord_token())