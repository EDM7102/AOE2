import os
import logging
from datetime import datetime
from typing import Dict, Optional, Any, List

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ===================== CONFIG =====================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID_ENV = os.getenv("CHAT_ID")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN ist nicht gesetzt. Bitte als Environment Variable setzen.")

if not CHAT_ID_ENV:
    raise RuntimeError("CHAT_ID ist nicht gesetzt. Bitte als Environment Variable setzen.")

try:
    CHAT_ID = int(CHAT_ID_ENV)
except ValueError:
    raise RuntimeError("CHAT_ID muss eine Zahl sein (z.B. -1003484505307).")

# Deine AoE2Insights Player IDs
FRIENDS: Dict[str, int] = {
    "EDM7101": 10770866,
    "JustForFun": 10769949,
    "rollthedice": 10775508,
}

AOE_API_BASE = "https://aoe2insights.com/api"
CHECK_INTERVAL = 60  # Sekunden

# ===================== LOGGING =====================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ===================== STATE =====================

# aktuell laufendes Match je Spieler (uuid)
current_matches: Dict[str, Optional[str]] = {name: None for name in FRIENDS}
# Rating vor Start des laufenden Matches
rating_before_match: Dict[str, Optional[int]] = {name: None for name in FRIENDS}
# letztes bekanntes Rating (egal ob aus laufendem oder beendetem Match)
last_ratings: Dict[str, Optional[int]] = {name: None for name in FRIENDS}
# Streak: >0 = Win-Streak, <0 = Lose-Streak
streaks: Dict[str, int] = {name: 0 for name in FRIENDS}


# ===================== API HELFER =====================

def fetch_last_match(player_id: int) -> Optional[Dict[str, Any]]:
    """
    Holt das letzte Match eines Spielers.
    Erwartet Endpoint: /api/player/<id>/lastmatch/
    """
    url = f"{AOE_API_BASE}/player/{player_id}/lastmatch/"
    try:
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error(f"Fehler bei fetch_last_match({player_id}): {e}")
        return None

    return data.get("match")


def fetch_recent_matches(player_id: int, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Holt die letzten Matches eines Spielers für /history und /trend.
    AoE2Insights hat einen Matches-Endpoint; wenn die Struktur anders ist,
    kommt einfach eine leere Liste zurück.
    """
    url = f"{AOE_API_BASE}/player/{player_id}/matches/"
    params = {"page": 1}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error(f"Fehler bei fetch_recent_matches({player_id}): {e}")
        return []

    matches = data.get("matches") or data.get("results") or []
    return matches[:limit]


def get_player_rating_from_match(match: Dict[str, Any], player_id: int) -> Optional[int]:
    players = match.get("players", [])
    for p in players:
        if p.get("player_id") == player_id:
            return p.get("rating")
    return None


def get_player_civ_from_match(match: Dict[str, Any], player_id: int) -> Optional[str]:
    players = match.get("players", [])
    for p in players:
        if p.get("player_id") == player_id:
            civ = p.get("civ")
            if isinstance(civ, dict):
                return civ.get("name")
            return civ
    return None


def parse_result_from_rating(before: Optional[int], after: Optional[int]) -> str:
    if before is None or after is None:
        return "Ergebnis unbekannt"
    if after > before:
        return "Win"
    if after < before:
        return "Loss"
    return "Unentschieden / kein Elo-Change"


# ===================== FORMATTER =====================

def format_match_start(player_name: str, match: Dict[str, Any], rating_before: Optional[int]) -> str:
    map_name = match.get("map", {}).get("name", "Unbekannt")
    uuid = match.get("uuid")
    leaderboard = match.get("leaderboard", "Unbekannt")
    started = match.get("started")
    pid = FRIENDS[player_name]
    civ = get_player_civ_from_match(match, pid)

    lines = [
        f"🎮 {player_name} hat ein neues Match gestartet!",
        f"📋 Ladder: {leaderboard}",
        f"🗺 Karte: {map_name}",
    ]
    if civ:
        lines.append(f"🧬 Civ: {civ}")
    if rating_before is not None:
        lines.append(f"⭐ Elo vor Start: {rating_before}")
    if started:
        lines.append(f"⏱ Start: {started}")
    if uuid:
        lines.append(f"🆔 Match-ID: {uuid}")

    return "\n".join(lines)


def format_match_end(player_name: str, match: Dict[str, Any], before: Optional[int], after: Optional[int]) -> str:
    map_name = match.get("map", {}).get("name", "Unbekannt")
    leaderboard = match.get("leaderboard", "Unbekannt")
    uuid = match.get("uuid")
    pid = FRIENDS[player_name]
    civ = get_player_civ_from_match(match, pid)

    result = parse_result_from_rating(before, after)
    diff_text = ""
    if before is not None and after is not None:
        diff = after - before
        if diff > 0:
            diff_text = f"🔺 +{diff} Elo (jetzt {after})"
        elif diff < 0:
            diff_text = f"🔻 {diff} Elo (jetzt {after})"
        else:
            diff_text = f"➖ Elo unverändert ({after})"
    elif after is not None:
        diff_text = f"⭐ Elo jetzt: {after}"

    lines = [
        f"🏁 Match beendet für {player_name}",
        f"📋 Ladder: {leaderboard}",
        f"🗺 Karte: {map_name}",
        f"🏆 Ergebnis: {result}",
    ]
    if civ:
        lines.append(f"🧬 Civ: {civ}")
    if diff_text:
        lines.append(diff_text)
    if uuid:
        lines.append(f"🆔 Match-ID: {uuid}")

    # Streak Info
    st = streaks.get(player_name, 0)
    if st >= 3:
        lines.append(f"🔥 Win-Streak: {st} in Folge!")
    elif st <= -3:
        lines.append(f"⚠️ Tilt-Warnung: {abs(st)} Niederlagen in Folge!")

    return "\n".join(lines)


def format_live_status(player_name: str, match: Dict[str, Any]) -> str:
    pid = FRIENDS[player_name]
    map_name = match.get("map", {}).get("name", "Unbekannt")
    started = match.get("started")
    leaderboard = match.get("leaderboard", "Unbekannt")
    ongoing = match.get("ongoing", False)
    rating = get_player_rating_from_match(match, pid)
    civ = get_player_civ_from_match(match, pid)

    lines = [
        f"📡 Live-Status für {player_name}",
        f"📋 Ladder: {leaderboard}",
        f"🗺 Karte: {map_name}",
        f"🔁 Läuft: {'Ja' if ongoing else 'Nein'}",
    ]
    if civ:
        lines.append(f"🧬 Civ: {civ}")
    if rating is not None:
        lines.append(f"⭐ Aktuelles Elo (laut Matchdaten): {rating}")
    if started:
        lines.append(f"⏱ Start: {started}")

    return "\n".join(lines)


def format_basic_stats(player_name: str) -> str:
    rating = last_ratings.get(player_name)
    rating_text = rating if rating is not None else "unbekannt"
    st = streaks.get(player_name, 0)

    streak_text = "keine Serie aktuell"
    if st > 0:
        streak_text = f"🔥 {st} Wins in Folge"
    elif st < 0:
        streak_text = f"⚠️ {abs(st)} Losses in Folge"

    lines = [
        f"📊 Basic Stats für {player_name}",
        f"⭐ Letztes bekanntes Elo: {rating_text}",
        f"📈 Streak: {streak_text}",
    ]
    return "\n".join(lines)


def format_history(player_name: str, matches: List[Dict[str, Any]]) -> str:
    if not matches:
        return f"Keine Matches für {player_name} gefunden."

    pid = FRIENDS[player_name]
    lines = [f"📜 Letzte Matches – {player_name}"]

    for m in matches:
        map_name = m.get("map", {}).get("name", "Unbekannt")
        started = m.get("started")
        civ = get_player_civ_from_match(m, pid)
        after = get_player_rating_from_match(m, pid)

        # AoE2Insights speichert evtl. rating_change in players?
        before = None
        result = parse_result_from_rating(before, after)

        line = f"• {map_name}"
        if civ:
            line += f" – {civ}"
        if after is not None:
            line += f" – Elo: {after}"
        if started:
            line += f" – {started}"
        line += f" – {result}"
        lines.append(line)

    return "\n".join(lines)


def format_leaderboard() -> str:
    # sortiere nach letztem bekannten Rating
    items = sorted(
        last_ratings.items(),
        key=lambda x: x[1] if x[1] is not None else 0,
        reverse=True,
    )
    lines = ["🏆 Gruppen-Leaderboard (letzte bekannte Elo):"]
    for name, rating in items:
        r = rating if rating is not None else "unbekannt"
        lines.append(f"• {name}: {r}")
    return "\n".join(lines)


# ===================== MENÜS =====================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("📡 Live", callback_data="menu_live"),
            InlineKeyboardButton("📊 Stats", callback_data="menu_stats"),
        ],
        [
            InlineKeyboardButton("📜 History", callback_data="menu_history"),
            InlineKeyboardButton("🏆 Leaderboard", callback_data="menu_leaderboard"),
        ],
        [
            InlineKeyboardButton("🎯 Coaching", callback_data="menu_coach"),
            InlineKeyboardButton("ℹ️ Hilfe", callback_data="menu_help"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def player_choice_keyboard(prefix: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for name in FRIENDS.keys():
        row.append(InlineKeyboardButton(name, callback_data=f"{prefix}|{name}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Zurück", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


# ===================== JOB: AUTO CHECK =====================

async def check_friends(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Wird alle CHECK_INTERVAL Sekunden ausgeführt.
    Erkennt neue Matches + beendete Matches und sendet Nachrichten.
    """
    global current_matches, rating_before_match, last_ratings, streaks

    bot = context.bot

    for name, pid in FRIENDS.items():
        match = fetch_last_match(pid)
        last_uuid = current_matches.get(name)
        before = rating_before_match.get(name)

        if not match:
            # kein Match gefunden
            continue

        ongoing = match.get("ongoing", False)
        uuid = str(match.get("uuid"))
        rating_now = get_player_rating_from_match(match, pid)

        if rating_now is not None:
            last_ratings[name] = rating_now

        # Fall 1: laufendes Match, neues uuid
        if ongoing:
            if last_uuid != uuid:
                # neues Match startet
                current_matches[name] = uuid
                rating_before_match[name] = last_ratings.get(name) or rating_now

                text = format_match_start(name, match, rating_before_match[name])
                try:
                    await bot.send_message(chat_id=CHAT_ID, text=text)
                except Exception as e:
                    logger.error(f"Fehler beim Senden (Match-Start {name}): {e}")
            # wenn uuid gleich bleibt → nichts tun
            continue

        # Fall 2: Match nicht mehr ongoing
        if last_uuid == uuid and last_uuid is not None:
            # war das zuletzt laufende Match → jetzt beendet
            after = rating_now
            text = format_match_end(name, match, before, after)

            # Streak aktualisieren
            if before is not None and after is not None:
                if after > before:
                    # Win
                    if streaks[name] >= 0:
                        streaks[name] += 1
                    else:
                        streaks[name] = 1
                elif after < before:
                    # Loss
                    if streaks[name] <= 0:
                        streaks[name] -= 1
                    else:
                        streaks[name] = -1

            try:
                await bot.send_message(chat_id=CHAT_ID, text=text)
            except Exception as e:
                logger.error(f"Fehler beim Senden (Match-Ende {name}): {e}")

            current_matches[name] = None
            rating_before_match[name] = None


# ===================== COMMAND HANDLER =====================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 AoE2 Insights Bot V2 ist aktiv.\n\n"
        "Ich tracke automatisch Matches von:\n"
        "• EDM7101\n"
        "• JustForFun\n"
        "• rollthedice\n\n"
        "Nutze das Menü unten für Live-Status, Stats, History und Coaching."
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🏠 Hauptmenü", reply_markup=main_menu_keyboard())


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = format_leaderboard()
    await update.message.reply_text(text)


async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Wähle einen Spieler für den Live-Status:",
        reply_markup=player_choice_keyboard("live"),
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Wähle einen Spieler für Basic Stats:",
        reply_markup=player_choice_keyboard("stats"),
    )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Wähle einen Spieler für Match-History:",
        reply_markup=player_choice_keyboard("history"),
    )


# ===================== CALLBACK HANDLER (BUTTONS) =====================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    # Hauptmenü Navigation
    if data == "menu_live":
        await query.edit_message_text(
            "Wähle einen Spieler für den Live-Status:",
            reply_markup=player_choice_keyboard("live"),
        )
        return

    if data == "menu_stats":
        await query.edit_message_text(
            "Wähle einen Spieler für Basic Stats:",
            reply_markup=player_choice_keyboard("stats"),
        )
        return

    if data == "menu_history":
        await query.edit_message_text(
            "Wähle einen Spieler für Match-History:",
            reply_markup=player_choice_keyboard("history"),
        )
        return

    if data == "menu_leaderboard":
        text = format_leaderboard()
        await query.edit_message_text(text, reply_markup=main_menu_keyboard())
        return

    if data == "menu_coach":
        text = (
            "🎯 Coaching Tools (Basis):\n\n"
            "• Nutze deine stärksten Civs auf offenen Maps (z.B. Mongols, Mayans).\n"
            "• Spiel was du gut kennst, wenn du tiltest.\n\n"
            "Später kann man hier noch Civ-Picker & Counter-Civ einbauen."
        )
        await query.edit_message_text(text, reply_markup=main_menu_keyboard())
        return

    if data == "menu_help":
        text = (
            "ℹ️ Hilfe\n\n"
            "Ich nutze aoe2insights.com, um Matches deiner Leute zu erkennen.\n"
            "• Auto-Alerts bei Matchstart & Matchende\n"
            "• Erkennung von Win-/Lose-Streaks\n"
            "• Live-Status & Basic Stats über das Menü\n"
        )
        await query.edit_message_text(text, reply_markup=main_menu_keyboard())
        return

    if data == "back_main":
        await query.edit_message_text("🏠 Hauptmenü", reply_markup=main_menu_keyboard())
        return

    # Aktionen mit Spieler: Prefix|Name
    if "|" in data:
        prefix, name = data.split("|", 1)
        if name not in FRIENDS:
            await query.edit_message_text("Unbekannter Spieler.", reply_markup=main_menu_keyboard())
            return

        pid = FRIENDS[name]

        if prefix == "live":
            match = fetch_last_match(pid)
            if not match:
                text = f"Für {name} wurde kein Match gefunden."
            else:
                text = format_live_status(name, match)

            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Aktualisieren", callback_data=f"live|{name}")],
                    [InlineKeyboardButton("⬅️ Zurück", callback_data="menu_live")],
                ]
            )
            await query.edit_message_text(text, reply_markup=keyboard)
            return

        if prefix == "stats":
            text = format_basic_stats(name)
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📡 Live-Status", callback_data=f"live|{name}")],
                    [InlineKeyboardButton("⬅️ Zurück", callback_data="menu_stats")],
                ]
            )
            await query.edit_message_text(text, reply_markup=keyboard)
            return

        if prefix == "history":
            matches = fetch_recent_matches(pid, limit=5)
            text = format_history(name, matches)
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Neu laden", callback_data=f"history|{name}")],
                    [InlineKeyboardButton("⬅️ Zurück", callback_data="menu_history")],
                ]
            )
            await query.edit_message_text(text, reply_markup=keyboard)
            return


# ===================== MAIN =====================

def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("live", live_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))

    # Buttons
    app.add_handler(CallbackQueryHandler(callback_handler))

    # JobQueue: regelmäßiges Checken der Freunde
    app.job_queue.run_repeating(
        check_friends,
        interval=CHECK_INTERVAL,
        first=5,
    )

    app.run_polling()


if __name__ == "__main__":
    main()