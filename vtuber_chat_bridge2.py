# 📦 Multi-Chat Bridge GUI for VTuber WebSocket
# Enhanced with proxy-ws support, group join, Twitch/YouTube/IRC, idle-only, and no mic auto-start

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

# ---- Global Config ----
CONFIG = {
    "ws_url": "ws://localhost:12393/proxy-ws",
    "avatar_uid": "tasia-main",
    "youtube_enabled": True,
    "twitch_enabled": True,
    "irc_enabled": True,
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

running_tasks = []
outbox = None
last_activity_time = time.time()
message_queue = deque(maxlen=CONFIG["message_queue_limit"])

# ---- Setup Logging ----
logger = logging.getLogger("chat_bridge")
logger.setLevel(logging.INFO)
log_stream = []

class StreamToList:
    def write(self, message):
        if message.strip():
            log_stream.append(message.strip())
    def flush(self):
        pass

logging.basicConfig(handlers=[logging.StreamHandler(StreamToList())], level=logging.INFO)

# ---- Ensure Files Exist ----
def ensure_files():
    for file, content in {
        CONFIG["blacklist_file"]: "putin\nnazi\nhate\nslur\n",
        CONFIG["idle_messages_file"]: "Hello! I'm still here!\nDon't be shy, say hi!\nTalk to me, I'm listening!\n",
        CONFIG["config_file"]: json.dumps(CONFIG, indent=2),
    }.items():
        if not os.path.exists(file):
            with open(file, "w", encoding="utf-8") as f:
                f.write(content)

ensure_files()

def load_blacklist():
    with open(CONFIG["blacklist_file"], "r", encoding="utf-8") as f:
        return set(word.strip().lower() for word in f if word.strip())

blacklist = load_blacklist()

# ---- Filters ----
def is_allowed_message(text: str) -> bool:
    global blacklist
    words = re.findall(r"\w+", text.lower())
    for word in words:
        if word in blacklist:
            logger.info(f"Blocked word: {word}")
            return False
    return True

# ---- Enqueue ----
async def send_to_ai(user: str, text: str):
    global last_activity_time
    if not is_allowed_message(text):
        return
    message_id = f"{user}:{text}"
    if message_id in message_queue:
        return
    message_queue.append(message_id)
    last_activity_time = time.time()
    await outbox.put((user, text))

# ---- Background Tasks ----
async def websocket_sender_loop(local_queue):
    while True:
        user, text = await local_queue.get()
        try:
            async with websockets.connect(CONFIG["ws_url"]) as ws:
                client_uid = f"bridge-{user}"
                if CONFIG.get("avatar_uid"):
                    await ws.send(json.dumps({
                        "type": "add-client-to-group",
                        "invitee_uid": CONFIG["avatar_uid"]
                    }))
                    await asyncio.sleep(0.2)
                await ws.send(json.dumps({
                    "type": "text-input",
                    "uid": client_uid,
                    "text": text,
                    "source": "bridge"
                }))
                await asyncio.sleep(CONFIG["ai_rate_limit_sec"])
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        local_queue.task_done()

async def idle_loop():
    global last_activity_time
    while CONFIG["idle_enabled"]:
        await asyncio.sleep(CONFIG["idle_interval_sec"])
        if CONFIG["idle_as_only_mode"] or time.time() - last_activity_time > CONFIG["idle_interval_sec"]:
            if os.path.exists(CONFIG["idle_messages_file"]):
                with open(CONFIG["idle_messages_file"], "r", encoding="utf-8") as f:
                    lines = [line.strip() for line in f if line.strip()]
                if lines:
                    await send_to_ai("idle_bot", random.choice(lines))

# ---- YouTube / Twitch / IRC ----
def mock_external_chat():
    def simulate():
        sources = []
        if CONFIG["youtube_enabled"]:
            sources.append("YouTube")
        if CONFIG["twitch_enabled"]:
            sources.append("Twitch")
        if CONFIG["irc_enabled"]:
            sources.append("IRC")

        if not sources:
            return

        sample_msgs = [
            ("anon1", "Hey Tasia!"),
            ("anon2", "What's up?"),
            ("anon3", "Nice stream!"),
            ("anon4", "Tell me a joke!"),
        ]

        while True:
            source = random.choice(sources)
            user, msg = random.choice(sample_msgs)
            asyncio.run_coroutine_threadsafe(send_to_ai(f"{source}:{user}", msg), asyncio.get_event_loop())
            time.sleep(random.randint(15, 40))

    threading.Thread(target=simulate, daemon=True).start()

# ---- Run All ----
async def run_all():
    global outbox
    outbox = asyncio.Queue()
    mock_external_chat()
    tasks = [
        asyncio.create_task(websocket_sender_loop(outbox)),
        asyncio.create_task(idle_loop())
    ]
    running_tasks.extend(tasks)
    await asyncio.gather(*tasks)

# ---- Start Asyncio Safely ----
def start_asyncio():
    try:
        asyncio.run(run_all())
    except Exception as e:
        logger.error(f"Asyncio error: {e}")

# ---- GUI ----
def launch_gui():
    root = tk.Tk()
    root.title("VTuber Chat Bridge")
    try:
        if os.path.exists(CONFIG["layout_file"]):
            with open(CONFIG["layout_file"], "rb") as f:
                geom = pickle.load(f)
                root.geometry(geom)
        else:
            root.geometry("550x580")
    except:
        root.geometry("550x580")

    def save_layout():
        with open(CONFIG["layout_file"], "wb") as f:
            pickle.dump(root.geometry(), f)

    tk.Label(root, text="Bridge Settings").pack(pady=10)
    idle_var = tk.BooleanVar(value=CONFIG['idle_enabled'])
    idle_only_var = tk.BooleanVar(value=CONFIG['idle_as_only_mode'])
    tk.Checkbutton(root, text="Enable Idle Messages", variable=idle_var).pack(anchor='w', padx=20)
    tk.Checkbutton(root, text="Idle-Only Mode (disable chat forwarding)", variable=idle_only_var).pack(anchor='w', padx=20)

    def open_file(file):
        try:
            subprocess.Popen([r"C:\\Program Files\\Notepad++\\notepad++.exe", file])
        except FileNotFoundError:
            subprocess.Popen(["notepad", file])

    def start():
        CONFIG['idle_enabled'] = idle_var.get()
        CONFIG['idle_as_only_mode'] = idle_only_var.get()
        with open(CONFIG["config_file"], "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, indent=2)
        threading.Thread(target=start_asyncio, daemon=True).start()
        messagebox.showinfo("Started", "Bridge started!")

    def stop():
        for task in running_tasks:
            task.cancel()
        messagebox.showinfo("Stopped", "Background tasks cancelled.")

    def reload_files():
        global blacklist
        blacklist = load_blacklist()
        logger.info("Reloaded blacklist.")

    ttk.Button(root, text="Start Bridge", command=start).pack(pady=10)
    ttk.Button(root, text="Pause/Stop", command=stop).pack(pady=5)
    ttk.Button(root, text="Edit Blacklist", command=lambda: open_file(CONFIG["blacklist_file"])).pack(pady=5)
    ttk.Button(root, text="Edit Idle Messages", command=lambda: open_file(CONFIG["idle_messages_file"])).pack(pady=5)
    ttk.Button(root, text="Edit Config (API Keys, etc)", command=lambda: open_file(CONFIG["config_file"])).pack(pady=5)
    ttk.Button(root, text="Reload Filters", command=reload_files).pack(pady=5)

    log_output = scrolledtext.ScrolledText(root, height=10, wrap=tk.WORD)
    log_output.pack(pady=10, fill=tk.BOTH, expand=True)

    def refresh_logs():
        log_output.delete('1.0', tk.END)
        log_output.insert(tk.END, "\n".join(log_stream[-100:]))
        root.after(1000, refresh_logs)

    refresh_logs()
    root.protocol("WM_DELETE_WINDOW", lambda: (save_layout(), root.destroy()))
    root.mainloop()

if __name__ == '__main__':
    launch_gui()
