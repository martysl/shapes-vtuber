
import asyncio
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import json
import websockets
import time
import requests
import re
from collections import deque
import os
import random
import subprocess
import logging
import pickle
import uuid
import socket
from typing import Optional, TYPE_CHECKING

# Import BiliBiliLivePlatform
try:
    from bilibili_live import BiliBiliLivePlatform
    BILIBILI_AVAILABLE = True
except ImportError as e:
    logging.warning(f"bilibili_live.py import failed: {e}. BiliBili functionality will be disabled.")
    BILIBILI_AVAILABLE = False
    # Create a dummy class for type checking
    if TYPE_CHECKING:
        from bilibili_live import BiliBiliLivePlatform
    else:
        BiliBiliLivePlatform = None

# ---- Global Config ----
CONFIG = {
    "ws_url": "ws://localhost:12393/proxy-ws",
    "youtube_enabled": False,
    "twitch_enabled": False,
    "irc_enabled": False,
    "bilibili_enabled": False,
    "youtube_channel_id": "YOUR_CHANNEL_ID",
    "youtube_api_key": "YOUR_YT_API_KEY",
    "twitch_username": "yourtwitchuser",
    "twitch_oauth": "oauth:yourtoken",
    "irc_server": "irc.libera.chat",
    "irc_port": 6667,
    "irc_channel": "#yourchannel",
    "bilibili_room_ids": [],
    "bilibili_sessdata": "",
    "blacklist_file": "blacklist.txt",
    "message_queue_limit": 100,
    "ai_rate_limit_sec": 6,
    "idle_messages_file": "msg.txt",
    "idle_interval_sec": 30,
    "idle_enabled": True,
    "idle_as_only_mode": True,
    "config_file": "bridge_config.json",
    "layout_file": "gui_state.pkl"
}

CLIENT_ID = str(uuid.uuid4())
running_tasks = []
outbox = None
last_activity_time = time.time()
message_queue = deque(maxlen=CONFIG["message_queue_limit"])
bilibili_platform_instance: Optional['BiliBiliLivePlatform'] = None
main_loop = None

# ---- Logging ----
logger = logging.getLogger("chat_bridge")
logger.setLevel(logging.INFO)
log_stream = []

class StreamToList:
    def write(self, message):
        if message.strip():
            log_stream.append(message.strip())
    def flush(self): pass

logging.basicConfig(handlers=[logging.StreamHandler(StreamToList())], level=logging.INFO)

# ---- File Setup ----
def ensure_files():
    for file, content in {
        CONFIG["blacklist_file"]: "putin\nnazi\nhate\nslur\n",
        CONFIG["idle_messages_file"]: "Hey I'm still here!",
        CONFIG["config_file"]: json.dumps(CONFIG, indent=2),
    }.items():
        if not os.path.exists(file):
            with open(file, "w", encoding="utf-8") as f:
                f.write(content)

def load_config():
    """Load configuration from file if it exists"""
    if os.path.exists(CONFIG["config_file"]):
        try:
            with open(CONFIG["config_file"], "r", encoding="utf-8") as f:
                loaded_config = json.load(f)
                CONFIG.update(loaded_config)
        except Exception as e:
            logger.error(f"Error loading config: {e}")

ensure_files()
load_config()

def load_blacklist():
    try:
        with open(CONFIG["blacklist_file"], "r", encoding="utf-8") as f:
            return set(word.strip().lower() for word in f if word.strip())
    except FileNotFoundError:
        return set()

blacklist = load_blacklist()

# ---- Filters ----
def is_allowed_message(text):
    return not any(word in blacklist for word in re.findall(r"\w+", text.lower()))

# ---- Queue Enqueue ----
async def send_to_ai(user: str, text: str):
    """
    Filters and queues a message to send to the VTuber WebSocket.

    Args:
        user: The username or source prefix (e.g. 'twitch:username')
        text: The chat message text
    """
    global last_activity_time, outbox

    if not outbox:
        return

    if not is_allowed_message(text):
        # Message blocked due to blacklist
        return

    message_id = f"{user}:{text}"
    if message_id in message_queue:
        # Avoid duplicate messages flooding
        return

    message_queue.append(message_id)
    last_activity_time = time.time()

    # Prepare a structured message including group join invite if needed
    # The "client_uid" here is the unique ID for this chat message sender
    client_uid = f"bridge-{user}"

    # Enqueue the message tuple for the websocket sender to pick up
    await outbox.put((client_uid, text))

async def websocket_sender_loop(outbox_queue: asyncio.Queue, ws_url: str, avatar_uid: str):
    """
    Continuously sends messages from the outbox queue to the WebSocket server,
    automatically sending a group join invite for the avatar UID before the text.

    Args:
        outbox_queue: Queue where (client_uid, text) tuples arrive
        ws_url: WebSocket server URL (e.g., "ws://localhost:12393/proxy-ws")
        avatar_uid: The UID of the avatar to invite to group chat
    """
    while True:
        try:
            client_uid, text = await outbox_queue.get()

            async with websockets.connect(ws_url) as ws:
                # Invite avatar UID to join the group if specified
                if avatar_uid:
                    invite_msg = {
                        "type": "add-client-to-group",
                        "invitee_uid": avatar_uid
                    }
                    await ws.send(json.dumps(invite_msg))
                    await asyncio.sleep(0.2)  # small delay before next message

                # Send the chat text message
                chat_msg = {
                    "type": "text-input",
                    "uid": client_uid,
                    "text": text,
                    "source": "bridge"
                }
                await ws.send(json.dumps(chat_msg))

                # Optional rate limit delay between sends
                await asyncio.sleep(CONFIG["ai_rate_limit_sec"])

        except Exception as e:
            logger.error(f"WebSocket error sending message: {e}")
            await asyncio.sleep(1)  # Wait before retrying

        finally:
            outbox_queue.task_done()

# ---- Idle Loop ----
async def idle_loop():
    global last_activity_time
    while CONFIG["idle_enabled"]:
        await asyncio.sleep(CONFIG["idle_interval_sec"])
        if CONFIG["idle_as_only_mode"] or time.time() - last_activity_time > CONFIG["idle_interval_sec"]:
            if os.path.exists(CONFIG["idle_messages_file"]):
                try:
                    with open(CONFIG["idle_messages_file"], "r", encoding="utf-8") as f:
                        lines = [line.strip() for line in f if line.strip()]
                    if lines:
                        await send_to_ai("idle_bot", random.choice(lines))
                except Exception as e:
                    logger.error(f"Error reading idle messages: {e}")

# ---- Twitch IRC ----
def twitch_thread():
    global main_loop
    try:
        sock = socket.socket()
        sock.connect(("irc.chat.twitch.tv", 6667))
        sock.send(f"PASS {CONFIG['twitch_oauth']}\r\n".encode())
        sock.send(f"NICK {CONFIG['twitch_username']}\r\n".encode())
        sock.send(f"JOIN #{CONFIG['twitch_username']}\r\n".encode())
        
        while True:
            resp = sock.recv(2048).decode()
            if "PING" in resp:
                sock.send("PONG :tmi.twitch.tv\r\n".encode())
            match = re.search(r":(\w+)!.* PRIVMSG #[^ ]+ :(.+)", resp)
            if match:
                user, msg = match.groups()
                if main_loop:
                    asyncio.run_coroutine_threadsafe(send_to_ai(f"Twitch:{user}", msg), main_loop)
    except Exception as e:
        logger.error(f"Twitch IRC error: {e}")

# ---- YouTube Poller ----
def youtube_thread():
    global main_loop
    last_comment_ids = set()
    url = f"https://www.googleapis.com/youtube/v3/commentThreads?part=snippet&videoId={CONFIG['youtube_channel_id']}&key={CONFIG['youtube_api_key']}&maxResults=10&order=time"
    
    while True:
        try:
            r = requests.get(url)
            items = r.json().get("items", [])
            for item in items:
                comment = item['snippet']['topLevelComment']['snippet']
                cid = item['id']
                if cid not in last_comment_ids:
                    last_comment_ids.add(cid)
                    user = comment['authorDisplayName']
                    msg = comment['textDisplay']
                    if main_loop:
                        asyncio.run_coroutine_threadsafe(send_to_ai(f"YouTube:{user}", msg), main_loop)
            time.sleep(10)
        except Exception as e:
            logger.error(f"YouTube error: {e}")
            time.sleep(30)

# ---- IRC ----
def irc_thread():
    global main_loop
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((CONFIG['irc_server'], CONFIG['irc_port']))
        sock.send(f"NICK bridgebot\r\nUSER bridgebot 0 * :bridgebot\r\nJOIN {CONFIG['irc_channel']}\r\n".encode())
        
        while True:
            data = sock.recv(2048).decode("utf-8", errors="ignore")
            for line in data.split("\r\n"):
                match = re.search(r":(\S+)!.* PRIVMSG \S+ :(.+)", line)
                if match:
                    user, msg = match.groups()
                    if main_loop:
                        asyncio.run_coroutine_threadsafe(send_to_ai(f"IRC:{user}", msg), main_loop)
    except Exception as e:
        logger.error(f"IRC error: {e}")

# ---- BiliBili Thread ----
async def bilibili_live_thread():
    global bilibili_platform_instance
    if not BILIBILI_AVAILABLE:
        logger.error("BiliBili functionality is disabled due to missing blivedm library.")
        return

    # Check if room_ids is not empty
    if not CONFIG["bilibili_room_ids"]:
        logger.warning("BiliBili room_ids not configured. BiliBili bridge will not start.")
        return

    try:
        bilibili_platform_instance = BiliBiliLivePlatform(
            room_ids=CONFIG["bilibili_room_ids"],
            sessdata=CONFIG["bilibili_sessdata"]
        )

        async def bilibili_message_handler(message: dict):
            if "text" in message:
                await send_to_ai("BiliBili", message["text"])

        await bilibili_platform_instance.register_message_handler(bilibili_message_handler)
        await bilibili_platform_instance.run()
    except ImportError as ie:
        logger.error(f"BiliBili setup failed due to missing library: {ie}")
    except Exception as e:
        logger.error(f"BiliBili Live error: {e}")

# ---- All Together ----
async def run_all():
    global outbox, bilibili_platform_instance, main_loop
    
    # Set the main loop reference
    main_loop = asyncio.get_running_loop()
    
    outbox = asyncio.Queue()
    tasks = [
        asyncio.create_task(websocket_sender_loop(outbox, CONFIG["ws_url"], "tasia-main")),
        asyncio.create_task(idle_loop())
    ]
    
    if CONFIG["twitch_enabled"]:
        threading.Thread(target=twitch_thread, daemon=True).start()
    if CONFIG["youtube_enabled"]:
        threading.Thread(target=youtube_thread, daemon=True).start()
    if CONFIG["irc_enabled"]:
        threading.Thread(target=irc_thread, daemon=True).start()
    if CONFIG["bilibili_enabled"] and BILIBILI_AVAILABLE:
        tasks.append(asyncio.create_task(bilibili_live_thread()))

    running_tasks.extend(tasks)
    
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Tasks cancelled")
    except Exception as e:
        logger.error(f"Error in run_all: {e}")

def start_asyncio():
    try:
        asyncio.run(run_all())
    except Exception as e:
        logger.error(f"Asyncio error: {e}")

# ---- GUI ----
def launch_gui():
    root = tk.Tk()
    root.title("VTuber Chat Bridge")
    root.geometry("550x700")

    def save_layout():
        try:
            with open(CONFIG["layout_file"], "wb") as f:
                pickle.dump(root.geometry(), f)
        except Exception as e:
            logger.error(f"Error saving layout: {e}")

    def load_layout():
        try:
            if os.path.exists(CONFIG["layout_file"]):
                with open(CONFIG["layout_file"], "rb") as f:
                    geometry = pickle.load(f)
                    root.geometry(geometry)
        except Exception as e:
            logger.error(f"Error loading layout: {e}")

    def reload_files():
        global blacklist
        blacklist = load_blacklist()
        load_config()
        logger.info("Reloaded blacklist and config.")

    load_layout()

    tk.Label(root, text="Bridge Settings").pack(pady=10)
    yt_var = tk.BooleanVar(value=CONFIG['youtube_enabled'])
    twitch_var = tk.BooleanVar(value=CONFIG['twitch_enabled'])
    irc_var = tk.BooleanVar(value=CONFIG['irc_enabled'])
    bilibili_var = tk.BooleanVar(value=CONFIG['bilibili_enabled'])
    idle_var = tk.BooleanVar(value=CONFIG['idle_enabled'])
    idle_only_var = tk.BooleanVar(value=CONFIG['idle_as_only_mode'])

    tk.Checkbutton(root, text="YouTube", variable=yt_var).pack(anchor='w', padx=20)
    tk.Checkbutton(root, text="Twitch", variable=twitch_var).pack(anchor='w', padx=20)
    tk.Checkbutton(root, text="IRC", variable=irc_var).pack(anchor='w', padx=20)
    tk.Checkbutton(root, text="BiliBili", variable=bilibili_var).pack(anchor='w', padx=20)
    tk.Checkbutton(root, text="Enable Idle Messages", variable=idle_var).pack(anchor='w', padx=20)
    tk.Checkbutton(root, text="Idle-Only Mode", variable=idle_only_var).pack(anchor='w', padx=20)

    # BiliBili Room ID Entry
    tk.Label(root, text="BiliBili Room IDs (comma-separated):").pack(anchor='w', padx=20)
    bilibili_room_ids_entry = tk.Entry(root, width=50)
    bilibili_room_ids_entry.insert(0, ",".join(map(str, CONFIG['bilibili_room_ids'])))
    bilibili_room_ids_entry.pack(anchor='w', padx=20)

    # BiliBili SESSDATA Entry
    tk.Label(root, text="BiliBili SESSDATA (optional):").pack(anchor='w', padx=20)
    bilibili_sessdata_entry = tk.Entry(root, width=50)
    bilibili_sessdata_entry.insert(0, CONFIG['bilibili_sessdata'])
    bilibili_sessdata_entry.pack(anchor='w', padx=20)

    def open_file(file):
        try:
            if os.name == 'nt':  # Windows
                os.startfile(file)
            else:  # macOS and Linux
                subprocess.call(['open', file] if os.uname().sysname == 'Darwin' else ['xdg-open', file])
        except Exception as e:
            logger.error(f"Error opening file {file}: {e}")

    def start():
        global bilibili_platform_instance
        CONFIG['youtube_enabled'] = yt_var.get()
        CONFIG['twitch_enabled'] = twitch_var.get()
        CONFIG['irc_enabled'] = irc_var.get()
        CONFIG['bilibili_enabled'] = bilibili_var.get()
        CONFIG['idle_enabled'] = idle_var.get()
        CONFIG['idle_as_only_mode'] = idle_only_var.get()

        # Update BiliBili room IDs and SESSDATA from GUI
        try:
            room_ids_str = bilibili_room_ids_entry.get()
            CONFIG['bilibili_room_ids'] = [int(rid.strip()) for rid in room_ids_str.split(',') if rid.strip()]
        except ValueError:
            messagebox.showerror("Input Error", "BiliBili Room IDs must be comma-separated numbers.")
            return

        CONFIG['bilibili_sessdata'] = bilibili_sessdata_entry.get().strip()

        try:
            with open(CONFIG["config_file"], "w", encoding="utf-8") as f:
                json.dump(CONFIG, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving config: {e}")
            
        threading.Thread(target=start_asyncio, daemon=True).start()
        messagebox.showinfo("Started", "Bridge started!")

    async def stop_bilibili():
        global bilibili_platform_instance
        if bilibili_platform_instance:
            try:
                await bilibili_platform_instance.disconnect()
                bilibili_platform_instance = None
                logger.info("BiliBili client stopped.")
            except Exception as e:
                logger.error(f"Error stopping BiliBili client: {e}")

    def stop():
        global main_loop
        for task in running_tasks:
            task.cancel()
        if bilibili_platform_instance and main_loop:
            try:
                asyncio.run_coroutine_threadsafe(stop_bilibili(), main_loop)
            except Exception as e:
                logger.error(f"Error stopping BiliBili: {e}")
        messagebox.showinfo("Stopped", "Background tasks cancelled.")

    ttk.Button(root, text="Start Bridge", command=start).pack(pady=10)
    ttk.Button(root, text="Stop", command=stop).pack(pady=5)
    ttk.Button(root, text="Edit Blacklist", command=lambda: open_file(CONFIG["blacklist_file"])).pack(pady=5)
    ttk.Button(root, text="Edit Idle Messages", command=lambda: open_file(CONFIG["idle_messages_file"])).pack(pady=5)
    ttk.Button(root, text="Edit Config", command=lambda: open_file(CONFIG["config_file"])).pack(pady=5)
    ttk.Button(root, text="Reload Filters", command=reload_files).pack(pady=5)

    log_output = scrolledtext.ScrolledText(root, height=10, wrap=tk.WORD)
    log_output.pack(pady=10, fill=tk.BOTH, expand=True)

    def refresh_logs():
        try:
            log_output.delete('1.0', tk.END)
            log_output.insert(tk.END, "\n".join(log_stream[-100:]))
            log_output.see(tk.END)
        except Exception as e:
            pass  # Ignore errors during log refresh
        root.after(1000, refresh_logs)

    refresh_logs()
    root.protocol("WM_DELETE_WINDOW", lambda: (save_layout(), root.destroy()))
    root.mainloop()

if __name__ == '__main__':
    launch_gui()
