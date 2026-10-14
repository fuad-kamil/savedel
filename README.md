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
* `/help` - FAQ and troubleshooting

---

## 🔒 Privacy & Storage
* All messages are stored locally on your machine in `anti_delete.db` (SQLite).
* No data is sent to third-party cloud services or servers.
* Deleted media (photos, voice notes, videos) are preserved via Telegram's `file_id` without eating up your hard drive space.
