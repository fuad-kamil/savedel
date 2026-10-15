# 🛡️ Telegram Anti-Delete & Edit Guard Bot

A Telegram Business bot that automatically caches, tracks, and recovers **deleted** and **edited** messages from your private chats in real-time.

---

## ⚡ How It Works
1. When anyone sends you a message (text, photo, voice note, video, document, audio, sticker), the bot caches it in a local SQLite database (`anti_delete.db`).
2. If the other person presses **"Delete for everyone"**, Telegram sends a `deleted_business_messages` event to your bot.
3. The bot immediately finds the deleted message from its database and sends you an instant private alert containing the exact text and/or deleted media.
4. If a message is **edited**, the bot notifies you showing the **Before** and **After** versions.

---

## 🚀 Setup Guide (Step-by-Step)

### Step 1: Create Your Bot & Enable "Business Mode"
1. Open Telegram and search for **[@BotFather](https://t.me/BotFather)**.
2. Send `/newbot` and follow the instructions to name your bot and choose a username (e.g., `MyAntiDeleteBot`).
3. Copy the **HTTP API Token** provided by BotFather.
4. **CRITICAL STEP:** While in `@BotFather`:
   * Send `/mybots`
   * Select your newly created bot
   * Click **Bot Settings**
   * Click **Business Mode**
   * Click **Turn On** (Telegram requires this for business bots).

---

### Step 2: Configure Environment Variables
Open the `.env` file in this folder and paste your bot token:

```env
BOT_TOKEN=1234567890:ABCdefGhIJKlmNoPQRsTUVwxyZ
```

---

### Step 3: Run the Bot
Make sure you are in the project folder and run:

```bash
python bot.py
```

You should see:
```text
🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀
Anti-Delete Business Bot is starting...
Keep this terminal open to keep the bot active.
```

---

### Step 4: Connect the Bot to Your Telegram Account
1. Ensure your Telegram account has **Telegram Premium**.
2. Open Telegram on your phone or desktop.
3. Go to **Settings** ➔ **Telegram Business** (or **Chat Automation**).
4. Tap **Chatbots** (or **Chat Automation**).
5. Search for your bot username (e.g. `@MyAntiDeleteBot`) and select it.
6. Enable chat permissions and save.
7. Your bot will immediately send you a message:  
   *"🎉 Success! Bot Connected to Your Account. I am now actively monitoring your private business chats."*

---

## 💬 Bot Commands
Send these commands directly to your bot in private chat:
* `/start` - Setup instructions and connection status
* `/status` - Live statistics (cached messages, recovered deletions, logged edits)
* `/recent` - View the last 5 recovered deleted messages
* `/saved` - View saved user vault messages (both user messages and receiver replies)
* `/admin` - Open the interactive Administrator Control Panel
* `/help` - FAQ and troubleshooting

---

## 🚀 Persistent Start Menu & Instant Actions
* **Integrated Native Chat Menu Button**: Telegram commands and menu button are automatically registered with `set_chat_menu_button` and `set_my_commands`.
* **Persistent Bottom Reply Keyboard**: Users and Admins never have to type `/start` manually. Quick buttons (`🚀 Start / Menu`, `💾 My Saved Messages`, `👑 Admin Dashboard`, `📊 System Status`) are permanently docked at the bottom of the chat.

---

## 🔔 Admin Activity Notifications
Whenever users interact with the bot or changes occur across chats, a structured alert is dispatched to administrators detailing:
1. **To Whom It Is Sent / Recipient**: Target chat name or bot handle with chat ID.
2. **From Whom It Was Sent**: Sender's full name, username, and Telegram user ID.
3. **Change Type & Action**:
   - ⏱️ `Timed / View-Once Media Saved`
   - ✏️ `Message Content Edited` (with before/after diff)
   - 🗑️ `Deleted Message Recovered` / `Media Purged`
   - 💾 `Direct User Message / Media Saved`
4. **Content Preview & Timestamp**: Message snippets and exact UTC timestamp.

---

## 🔒 Privacy & Storage
* All messages are stored in `anti_delete.db` (SQLite) and backed up to Cloudinary cloud storage.
* Non-blocking background threads ensure media uploads never lag the Telegram bot.
