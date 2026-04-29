import os
import json
import threading
import random
import string
import logging
from datetime import datetime, timedelta

from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from pyrogram.errors import RPCError

# === CONFIGURATION (from environment variables) ===
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "YourBotUsername")
STORAGE_CHANNEL = int(os.environ.get("STORAGE_CHANNEL", 0))
OWNER_ID = int(os.environ.get("OWNER_ID", 0))
WELCOME_VIDEO_URL = os.environ.get("WELCOME_VIDEO_URL", "")

# === FILE PATHS ===
STORAGE_FILE = "storage.json"
USERS_FILE = "users.json"

# === THREAD LOCKS ===
storage_lock = threading.Lock()
users_lock = threading.Lock()

# === STATE ===
pending_add = set()
pending_batches = {}   # uid -> list of saved items metadata
pending_options = {}   # uid -> "with"|"without"|"protect"

# === SETUP LOGGING ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# === UTILITIES ===
def ensure_files():
    for file, default in [(STORAGE_FILE, {}), (USERS_FILE, [])]:
        if not os.path.exists(file):
            with open(file, "w") as f:
                json.dump(default, f)

def read_json(path, lock):
    with lock:
        with open(path, "r") as f:
            return json.load(f)

def write_json(path, data, lock):
    with lock:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

def generate_code(length=8):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def build_menu(uid):
    buttons = [[InlineKeyboardButton("➕ Add Batch", callback_data="add")]]
    if uid == OWNER_ID:
        buttons.append([
            InlineKeyboardButton("👥 Users", callback_data="users"),
            InlineKeyboardButton("📊 Stats", callback_data="stats")
        ])
    return InlineKeyboardMarkup(buttons)

# === INIT ===
ensure_files()
app = Client("file_storage_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# === START /start ===
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, m: Message):
    uid = m.from_user.id
    users = read_json(USERS_FILE, users_lock)
    if uid not in users:
        users.append(uid)
        write_json(USERS_FILE, users, users_lock)

    # If just /start show welcome + menu
    if len(m.command) == 1:
        buttons = build_menu(uid)
        caption = (
            "╭━━━━━━━━━━━━━━━━━━━━━━━━━━━━╮\n"
            "┃  📁 File Storage Bot\n"
            "┃━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "┃\n"
            "┃  ⚡ **Quick Start:**\n"
            "┃  1️⃣ /add → Add messages\n"
            "┃  2️⃣ /done → Get share link\n"
            "┃  3️⃣ /my_batches → Your links\n"
            "┃\n"
            "┃  📌 **More Commands:**\n"
            "┃  • /delete `code` → Remove\n"
            "┃  • /exp `code` `days` → Expiry\n"
            "┃\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯"
        )
        if WELCOME_VIDEO_URL:
            return await client.send_video(
                chat_id=m.chat.id,
                video=WELCOME_VIDEO_URL,
                caption=caption,
                reply_markup=buttons
            )
        else:
            return await m.reply(caption, reply_markup=buttons)

    # Otherwise treat /start <code> as retrieval
    code = m.command[1]
    store = read_json(STORAGE_FILE, storage_lock)
    entry = store.get(code)

    if not entry:
        return await m.reply("❌ Invalid or expired code.")

    # Check expiry
    exp = entry.get("expires_at")
    if exp and datetime.utcnow() > datetime.fromisoformat(exp):
        return await m.reply("⏳ This batch has expired.")

    # Update download count
    entry["downloads"] = entry.get("downloads", 0) + 1
    write_json(STORAGE_FILE, store, storage_lock)

    failed = 0
    for item in entry["batch"]:
        method = item.get("method", "copy")
        protect = item.get("protect", False)
        try:
            if method == "forward":
                try:
                    await client.forward_messages(
                        chat_id=m.chat.id,
                        from_chat_id=item["chat_id"],
                        message_ids=item["msg_id"]
                    )
                except Exception as e_forward:
                    logger.info(f"forward failed for {item['msg_id']}: {e_forward} -> falling back to copy")
                    await client.copy_message(
                        chat_id=m.chat.id,
                        from_chat_id=item["chat_id"],
                        message_id=item["msg_id"],
                        protect_content=protect
                    )
            else:
                await client.copy_message(
                    chat_id=m.chat.id,
                    from_chat_id=item["chat_id"],
                    message_id=item["msg_id"],
                    protect_content=protect
                )
        except Exception as e:
            logger.error(f"Failed to deliver saved item {item}: {e}")
            failed += 1

    if failed:
        await m.reply(f"⚠️ {failed} messages were missing or deleted.")


# === /add ===
@app.on_message(filters.command("add") & filters.private)
async def add_cmd(client, m: Message):
    uid = m.from_user.id
    pending_add.add(uid)
    pending_batches[uid] = []
    pending_options[uid] = None

    buttons = [
        [InlineKeyboardButton("📨 With Forward Tag", callback_data="opt_with")],
        [InlineKeyboardButton("📋 Without Tag (Copy)", callback_data="opt_without")],
        [InlineKeyboardButton("🔒 Without Tag + Protect", callback_data="opt_protect")],
        [InlineKeyboardButton("✅ Done", callback_data="done"),
         InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
    ]
    await m.reply("📤 Choose how you want to add messages (or press Done when finished):",
                  reply_markup=InlineKeyboardMarkup(buttons))

# === /done ===
@app.on_message(filters.command("done") & filters.private)
async def done_cmd(client, m: Message):
    uid = m.from_user.id
    if uid not in pending_add:
        return await m.reply("❌ No active batch. Use /add.")
    items = pending_batches.get(uid, [])
    if not items:
        pending_add.discard(uid)
        pending_batches.pop(uid, None)
        pending_options.pop(uid, None)
        return await m.reply("❌ No messages added. Batch cancelled.")
    code = generate_code()
    store = read_json(STORAGE_FILE, storage_lock)
    store[code] = {
        "user_id": uid,
        "batch": items,
        "downloads": 0
    }
    write_json(STORAGE_FILE, store, storage_lock)

    opt = pending_options.get(uid)
    if opt == "with":
        opt_text = "With Forward Tag"
    elif opt == "protect":
        opt_text = "Without Tag + Protect"
    else:
        opt_text = "Without Tag"

    pending_add.discard(uid)
    pending_batches.pop(uid, None)
    pending_options.pop(uid, None)

    await m.reply(
        f"✅ Saved {len(items)} items ({opt_text})!\n"
        f"🔗 Link: https://t.me/{BOT_USERNAME}?start={code}\n"
        f"YOUR CODE : {code}"
    )

# === /exp ===
@app.on_message(filters.command("exp") & filters.private)
async def exp_cmd(client, m: Message):
    parts = m.text.split()
    if len(parts) != 3:
        return await m.reply("Usage: /exp code days")
    code, days = parts[1], parts[2]
    try:
        days = int(days)
    except:
        return await m.reply("⛔ Days must be an integer.")
    store = read_json(STORAGE_FILE, storage_lock)
    entry = store.get(code)
    if not entry:
        return await m.reply("❌ Invalid code.")
    if m.from_user.id != entry["user_id"] and m.from_user.id != OWNER_ID:
        return await m.reply("🚫 Not your batch.")
    entry["expires_at"] = (datetime.utcnow() + timedelta(days=days)).isoformat()
    write_json(STORAGE_FILE, store, storage_lock)
    await m.reply(f"⏳ Expiry set to {days} day(s) from now.")

# === /my_batches ===
@app.on_message(filters.command("my_batches") & filters.private)
async def my_batches(client, m: Message):
    uid = m.from_user.id
    store = read_json(STORAGE_FILE, storage_lock)
    user_links = [code for code, data in store.items() if data.get("user_id") == uid]
    if not user_links:
        return await m.reply("❌ You haven't created any batches yet.")
    msg = "\n".join(f"🔗 https://t.me/{BOT_USERNAME}?start={code}  YOUR CODE : {code}" for code in user_links)
    await m.reply(f"🧾 Your batches:\n\n{msg}")

# === /delete ===
@app.on_message(filters.command("delete") & filters.private)
async def delete_cmd(client, m: Message):
    parts = m.text.split()
    if len(parts) != 2:
        return await m.reply("Usage: /delete code")
    code = parts[1]
    store = read_json(STORAGE_FILE, storage_lock)
    entry = store.get(code)
    if not entry:
        return await m.reply("❌ Code not found.")
    if m.from_user.id != entry["user_id"] and m.from_user.id != OWNER_ID:
        return await m.reply("🚫 Not your batch.")
    store.pop(code)
    write_json(STORAGE_FILE, store, storage_lock)
    await m.reply(f"✅ Deleted batch `{code}`")

# === /broadcast ===
@app.on_message(filters.command("broadcast") & filters.private)
async def broadcast(client, m: Message):
    if m.from_user.id != OWNER_ID:
        return
    if len(m.command) < 2:
        return await m.reply("Usage: /broadcast <message>")
    msg = m.text.split(None, 1)[1]
    users = read_json(USERS_FILE, users_lock)
    success = failed = 0
    for uid in users:
        try:
            await client.send_message(uid, msg)
            success += 1
        except:
            failed += 1
    await m.reply(f"📢 Sent to {success} users.\n❌ Failed: {failed}")

# === BATCH HANDLER: capture messages while adding ===
@app.on_message(filters.private & ~filters.command(["add", "done", "start", "my_batches", "delete", "exp", "broadcast"]))
async def batch_handler(client, m: Message):
    uid = m.from_user.id
    if uid not in pending_add:
        return

    # if user hasn't chosen option yet, default to WITHOUT tag
    if pending_options.get(uid) is None:
        pending_options[uid] = "without"
        try:
            await m.reply("⚠️ No option selected — defaulting to: Without Tag (Copy).")
        except:
            pass

    choice = pending_options.get(uid)
    try:
        if choice == "with":
            res = await client.forward_messages(
                chat_id=STORAGE_CHANNEL,
                from_chat_id=m.chat.id,
                message_ids=m.id
            )
            msg_obj = res[0] if isinstance(res, (list, tuple)) else res
            saved_id = msg_obj.id
            saved_item = {"chat_id": STORAGE_CHANNEL, "msg_id": saved_id, "method": "forward", "protect": False}
        elif choice == "protect":
            res = await client.copy_message(
                chat_id=STORAGE_CHANNEL,
                from_chat_id=m.chat.id,
                message_id=m.id,
                protect_content=True
            )
            saved_id = res.id
            saved_item = {"chat_id": STORAGE_CHANNEL, "msg_id": saved_id, "method": "copy", "protect": True}
        else:  # without
            res = await client.copy_message(
                chat_id=STORAGE_CHANNEL,
                from_chat_id=m.chat.id,
                message_id=m.id
            )
            saved_id = res.id
            saved_item = {"chat_id": STORAGE_CHANNEL, "msg_id": saved_id, "method": "copy", "protect": False}

        pending_batches[uid].append(saved_item)
        await m.reply(f"✅ Added. Total: {len(pending_batches[uid])}")
    except RPCError as e:
        logger.error(f"Error saving message from {uid}: {e}")
        await m.reply("❌ Could not save message. Please make sure the bot is admin in the storage channel and has permission to post.")
    except Exception as e:
        logger.error(f"Unexpected error saving message from {uid}: {e}")
        await m.reply("❌ Unexpected error occurred while saving the message.")

# === CALLBACKS (buttons) ===
@app.on_callback_query()
async def cb_handler(client, cb):
    uid = cb.from_user.id
    data = cb.data

    if data == "add":
        fake = cb.message
        fake.from_user = cb.from_user
        await add_cmd(client, fake)
    elif data == "done":
        fake = cb.message
        fake.from_user = cb.from_user
        await done_cmd(client, fake)
    elif data == "cancel":
        pending_add.discard(uid)
        pending_batches.pop(uid, None)
        pending_options.pop(uid, None)
        await cb.message.reply("❌ Batch cancelled.")
    elif data == "users" and uid == OWNER_ID:
        users = read_json(USERS_FILE, users_lock)
        await cb.message.reply(f"👥 Total users: {len(users)}")
    elif data == "stats" and uid == OWNER_ID:
        store = read_json(STORAGE_FILE, storage_lock)
        await cb.message.reply(f"📦 Total batches: {len(store)}")
    elif data == "opt_with":
        pending_options[uid] = "with"
        await cb.message.reply("✅ Selected: With Forward Tag. Now send your messages, and /done.")
    elif data == "opt_without":
        pending_options[uid] = "without"
        await cb.message.reply("✅ Selected: Without Tag (Copy). Now send your messages, and /done.")
    elif data == "opt_protect":
        pending_options[uid] = "protect"
        await cb.message.reply("✅ Selected: Without Tag + Protect. Now send your messages, and /done.")
    else:
        await cb.answer()

# === SET BOT COMMANDS ===
async def set_commands():
    await app.set_bot_commands([
        BotCommand("start", "🏠 Start the bot"),
        BotCommand("add", "➕ Add messages to batch"),
        BotCommand("done", "✅ Finish batch and get link"),
        BotCommand("my_batches", "🧾 View your batches"),
        BotCommand("delete", "❌ Delete a batch"),
        BotCommand("exp", "⏳ Set batch expiry"),
        BotCommand("broadcast", "📢 Broadcast (Owner)")
    ])
    logger.info("✅ Bot commands registered!")

# === START BOT ===
async def main():
    await app.start()
    await set_commands()
    logger.info("🤖 Bot started!")
    await idle()
    await app.stop()

if __name__ == "__main__":
    app.run(main())
