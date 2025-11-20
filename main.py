import os
import time
import requests
from bs4 import BeautifulSoup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram import Update
import logging

# ========================= CONFIG =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID"))

FRIENDS = {
    "EDM7101": 10770866,
    "JustForFun": 10769949,
    "rollthedice": 10775508,
}

INSIGHTS_URL = "https://www.aoe2insights.com/user/{pid}/matches/"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Speichert die letzte bekannte Match-ID, um doppelte Nachrichten zu verhindern
last_match_ids = {name: None for name in FRIENDS}


# ========================= SCRAPING FUNKTIONEN =========================

def scrape_last_match(player_id):
    """Holt das neueste Match über HTML Scraping."""
    url = INSIGHTS_URL.format(pid=player_id)

    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Fehler beim Laden: {e}")
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    table = soup.find("table", {"class": "table"})
    if not table:
        return None

    first_row = table.find("tr", {"class": "match-row"})
    if not first_row:
        return None

    match_id = first_row.get("data-match-id")
    cells = first_row.find_all("td")

    data = {
        "match_id": match_id,
        "map": cells[2].get_text(strip=True),
        "civ": cells[3].get_text(strip=True),
        "elo": cells[4].get_text(strip=True),
        "result": cells[5].get_text(strip=True),
        "status": cells[1].get_text(strip=True).lower(),  # ongoing / finished
    }

    return data


def get_current_elo(player_id):
    """Liest nur Elo über Scraping aus."""
    url = INSIGHTS_URL.format(pid=player_id)

    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except Exception as e:
        logger.error(f"ELO Fehler: {e}")
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table", {"class": "table"})
    if not table:
        return None

    first_row = table.find("tr", {"class": "match-row"})
    if not first_row:
        return None

    cells = first_row.find_all("td")
    return cells[4].get_text(strip=True)


# ========================= JOB: MATCH CHECK =========================

async def check_matches(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot

    for name, pid in FRIENDS.items():
        match = scrape_last_match(pid)
        if not match:
            continue

        match_id = match["match_id"]

        # --- MATCH START ---
        if match["status"] == "ongoing" and last_match_ids[name] != match_id:
            last_match_ids[name] = match_id

            text = (
                f"🎮 {name} spielt jetzt!\n"
                f"🗺 Map: {match['map']}\n"
                f"🧬 Civ: {match['civ']}\n"
                f"⭐ Elo: {match['elo']}\n"
                f"🆔 Match-ID: {match_id}"
            )
            await bot.send_message(chat_id=CHAT_ID, text=text)

        # --- MATCH ENDE ---
        if match["status"] != "ongoing" and last_match_ids[name] == match_id:
            text = (
                f"🏁 Match beendet für {name}\n"
                f"🏆 Ergebnis: {match['result']}\n"
                f"⭐ Neues Elo: {match['elo']}\n"
                f"🗺 Map: {match['map']}\n"
            )
            await bot.send_message(chat_id=CHAT_ID, text=text)

            last_match_ids[name] = None


# ========================= COMMANDS =========================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot aktiv! Ich überwache Matches über AoE2Insights-Scraping.")

async def elo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "📊 *Aktuelle Elo-Werte:*\n\n"
    for name, pid in FRIENDS.items():
        elo = get_current_elo(pid)
        if elo:
            text += f"⭐ *{name}*: {elo}\n"
        else:
            text += f"⚠️ *{name}*: Keine Daten\n"

    await update.message.reply_text(text, parse_mode="Markdown")


# ========================= MAIN =========================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("elo", elo_cmd))

    # Auto-Matchcheck alle 15 Sekunden
    app.job_queue.run_repeating(check_matches, interval=15, first=5)

    app.run_polling()


if __name__ == "__main__":
    main()
