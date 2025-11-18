from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram import Update
import os

# ======================================================
#   BOT_TOKEN kommt als Environment Variable von Render
# ======================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Responds to the /start command by sending the chat ID.
    """
    chat_id = update.effective_chat.id
    # Send the chat ID back to the group or user
    await update.message.reply_text(f"CHAT ID: {chat_id}")

if __name__ == "__main__":
    # Ensure the Bot token is provided via environment variable
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN fehlt! In Render → Environment Variables setzen.")

    # Build and run the bot
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # Register the /start command handler
    app.add_handler(CommandHandler("start", start))
    # Start polling for updates
    app.run_polling()
