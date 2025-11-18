import os
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List

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


# ============================================================================
#  Configuration
# ============================================================================

# Telegram bot token and group chat ID are expected to be provided via
# environment variables when running on Render.  For local testing you can
# define them here or export them in your environment.
BOT_TOKEN = os.getenv("BOT_TOKEN")

# The CHAT_ID must be set to the group ID where notifications are sent.  It is
# stored as a string because IDs may start with a minus sign.
CHAT_ID_ENV = os.getenv("CHAT_ID")
if CHAT_ID_ENV is not None:
    try:
        CHAT_ID = int(CHAT_ID_ENV)
    except ValueError:
        CHAT_ID = None
else:
    CHAT_ID = None

# Interval (in seconds) at which the job queue checks for new matches.
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

# Path for persisting data across restarts.  On Render this file will live in
# the working directory.  If you change this path ensure it points to a
# writable location.
DATA_FILE = os.getenv("DATA_FILE", "aoe2_data.json")

# Default tilt threshold: number of consecutive losses before sending a tilt
# warning.  Can be overridden per player via /tiltthreshold command.
DEFAULT_TILT_THRESHOLD = int(os.getenv("TILT_THRESHOLD", "3"))

# Friend mapping: display names to AoE2Insights player IDs.  Update this
# dictionary to add or remove tracked players.  The keys should be exactly
# what you want shown in notifications.  The values are the Insights profile
# identifiers extracted from aoe2insights.com.
FRIENDS: Dict[str, int] = {
    "EDM7101": 10770866,
    "JustForFun": 10769949,
    "rollthedice": 10775508,
}


# ============================================================================
#  Logging setup
# ============================================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ============================================================================
#  Data persistence utilities
# ============================================================================

def load_data() -> Dict[str, Any]:
    """Load persisted data from the DATA_FILE.

    If no file exists, initialise a new data structure for all players.

    Returns:
        dict: The loaded or initialised data structure.
    """
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # ensure all players from FRIENDS exist in data
            for name, pid in FRIENDS.items():
                if name not in data.get("players", {}):
                    data.setdefault("players", {})[name] = _new_player_data(pid)
            return data
        except Exception as e:
            logger.error(f"Fehler beim Laden der Daten: {e}")
    # initialise new data
    data: Dict[str, Any] = {
        "players": {},
        "global": {
            "memes_enabled": False,
            "weekly_last_sent": None,
        },
    }
    for name, pid in FRIENDS.items():
        data["players"][name] = _new_player_data(pid)
    return data


def _new_player_data(player_id: int) -> Dict[str, Any]:
    """Return a new player data dictionary with defaults."""
    return {
        "id": player_id,
        "match_history": [],  # list of match records
        "rating_history": [],  # list of (timestamp ISO, rating)
        "current_match_id": None,
        "current_match_start_rating": None,
        "wins_streak": 0,
        "losses_streak": 0,
        "civ_stats": {},  # civ name -> {"wins": int, "losses": int}
        "tilt_threshold": DEFAULT_TILT_THRESHOLD,
        "elo_thresholds": [],  # list of ints (Elo values) to alert on
        "triggered_elo_alerts": [],  # list of thresholds already triggered
        "playtime": {},  # date string (YYYY-MM-DD) -> total seconds
    }


def save_data(data: Dict[str, Any]) -> None:
    """Persist data to DATA_FILE."""
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Fehler beim Speichern der Daten: {e}")


# Initialise data at module import
data: Dict[str, Any] = load_data()


# ============================================================================
#  Helper functions for AoE2 Insights API
# ============================================================================

API_BASE = "https://aoe2insights.com/api"


def fetch_last_match(player_id: int) -> Dict[str, Any]:
    """Fetch the last (most recent) match for a player.

    Args:
        player_id (int): AoE2Insights profile ID.

    Returns:
        dict: The JSON response for the last match, or an empty dict if request
            fails or returns invalid data.
    """
    url = f"{API_BASE}/player/{player_id}/lastmatch/"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        js = r.json()
        match = js.get("match")
        if isinstance(match, dict):
            return match
    except Exception as exc:
        logger.warning(f"Fehler beim Abrufen des letzten Matches für {player_id}: {exc}")
    return {}


def extract_player_info(match: Dict[str, Any], player_id: int) -> Dict[str, Any]:
    """Extract the info for a specific player from a match dict.

    Args:
        match (dict): The match object from the API.
        player_id (int): The player's AoE2Insights ID.

    Returns:
        dict: A dict containing keys "rating", "civilization", "won" (bool) if available.
    """
    res = {
        "rating": None,
        "civilization": None,
        "won": None,
    }
    players = match.get("players", [])
    for p in players:
        if p.get("player_id") == player_id:
            res["rating"] = p.get("rating")
            # civilization may be nested under p["civilization"] or p["civ"] depending on API
            res["civilization"] = (
                p.get("civilization")
                or p.get("civilization_name")
                or p.get("civ_name")
                or p.get("civ")
            )
            # "won" might be provided as True/False or as a string ("won": "True")
            won_raw = p.get("won")
            if isinstance(won_raw, bool):
                res["won"] = won_raw
            elif isinstance(won_raw, str):
                res["won"] = won_raw.lower() == "true"
            break
    return res


# ============================================================================
#  Stats and tracking helpers
# ============================================================================

def record_match_end(name: str, match: Dict[str, Any]) -> str:
    """Record a finished match for a given player, update streaks, civ stats etc.

    Args:
        name (str): Display name of the player.
        match (dict): The match object from the API.

    Returns:
        str: A formatted summary of the match result for notifications.
    """
    player_data = data["players"][name]
    pid = player_data["id"]
    # Extract player-specific info
    info = extract_player_info(match, pid)
    rating_after = info.get("rating")
    civ = info.get("civilization") or "Unbekannt"
    won = info.get("won")
    # Determine rating before
    rating_before = player_data.get("current_match_start_rating")
    if rating_before is None:
        # Fallback: take last rating from rating history if exists
        rating_before = player_data["rating_history"][-1][1] if player_data["rating_history"] else rating_after
    # Compute rating difference
    diff = None
    if rating_after is not None and rating_before is not None:
        diff = rating_after - rating_before
    # Determine result
    if won is not None:
        result_text = "gewonnen" if won else "verloren"
    elif diff is not None:
        result_text = "gewonnen" if diff > 0 else ("verloren" if diff < 0 else "unentschieden")
    else:
        result_text = ""  # unknown
    # Duration: match.get("duration") might be seconds
    duration_seconds = match.get("duration")
    duration_text = None
    if duration_seconds:
        try:
            secs = int(duration_seconds)
            minutes = secs // 60
            seconds = secs % 60
            duration_text = f"{minutes}:{seconds:02d}"
        except Exception:
            duration_text = None
    # Map name
    map_name = match.get("map", {}).get("name") or match.get("name") or match.get("map_type") or "Unbekannt"
    # Streak updates
    if result_text == "gewonnen":
        player_data["wins_streak"] += 1
        player_data["losses_streak"] = 0
    elif result_text == "verloren":
        player_data["losses_streak"] += 1
        player_data["wins_streak"] = 0
    else:
        # draw or unknown resets both
        player_data["wins_streak"] = 0
        player_data["losses_streak"] = 0
    # Tilt detection
    tilt_threshold = player_data.get("tilt_threshold", DEFAULT_TILT_THRESHOLD)
    tilt_text = None
    if player_data["losses_streak"] >= tilt_threshold:
        tilt_text = f"⚠️ {name} hat {player_data['losses_streak']} Spiele in Folge verloren! Vielleicht eine Pause einlegen?"
        # Reset streak after alert to avoid repeat spam
        player_data["losses_streak"] = 0
    # Win streak detection
    streak_text = None
    if player_data["wins_streak"] >= 3:
        streak_text = f"🔥 {name} ist auf einem {player_data['wins_streak']}-Win-Streak!"
        # Reset streak after alert so next streak can trigger again later
        player_data["wins_streak"] = 0
    # Civ stats update
    civ_stats = player_data.setdefault("civ_stats", {})
    civ_record = civ_stats.setdefault(civ, {"wins": 0, "losses": 0})
    if result_text == "gewonnen":
        civ_record["wins"] += 1
    elif result_text == "verloren":
        civ_record["losses"] += 1
    # Playtime update (for stats)
    if duration_seconds and duration_seconds > 0:
        end_time_ts = match.get("finished")  # seconds since epoch
        if end_time_ts:
            try:
                dt = datetime.utcfromtimestamp(int(end_time_ts)).date()
                date_key = dt.isoformat()
            except Exception:
                date_key = datetime.utcnow().date().isoformat()
        else:
            date_key = datetime.utcnow().date().isoformat()
        player_data["playtime"][date_key] = player_data["playtime"].get(date_key, 0) + duration_seconds
    # Match history record
    history_entry = {
        "time": datetime.utcnow().isoformat(),
        "map": map_name,
        "civ": civ,
        "rating_before": rating_before,
        "rating_after": rating_after,
        "rating_diff": diff,
        "result": result_text,
        "duration_sec": duration_seconds,
    }
    player_data["match_history"].append(history_entry)
    # Keep last 50 matches
    if len(player_data["match_history"]) > 50:
        player_data["match_history"] = player_data["match_history"][-50:]
    # Update rating history
    if rating_after is not None:
        player_data["rating_history"].append((datetime.utcnow().isoformat(), rating_after))
        # Keep last 100 rating points
        if len(player_data["rating_history"]) > 100:
            player_data["rating_history"] = player_data["rating_history"][-100:]
    # Clear current match
    player_data["current_match_id"] = None
    player_data["current_match_start_rating"] = None
    # Compose summary text
    diff_text = ""
    if diff is not None:
        if diff > 0:
            diff_text = f"🔺 +{diff} Elo"
        elif diff < 0:
            diff_text = f"🔻 {diff} Elo"
        else:
            diff_text = "➖ Elo unverändert"
    summary_lines = [
        f"🏁 {name} hat ein Match beendet ({result_text})",
        f"🗺️ Karte: {map_name}",
        f"🧬 Civ: {civ}",
    ]
    if duration_text:
        summary_lines.append(f"⏱ Dauer: {duration_text}")
    if diff_text:
        summary_lines.append(diff_text)
    summary = "\n".join(summary_lines)
    return summary + ("\n" + tilt_text if tilt_text else "") + ("\n" + streak_text if streak_text else "")


def record_match_start(name: str, match: Dict[str, Any]) -> str:
    """Record the start of a new match for a given player and return a summary string."""
    player_data = data["players"][name]
    pid = player_data["id"]
    info = extract_player_info(match, pid)
    rating = info.get("rating")
    civ = info.get("civilization") or "Unbekannt"
    # Save starting Elo for later comparison
    player_data["current_match_start_rating"] = rating
    player_data["current_match_id"] = match.get("uuid")
    # Map name
    map_name = match.get("map", {}).get("name") or match.get("name") or match.get("map_type") or "Unbekannt"
    # Ladder or game type
    ladder = match.get("leaderboard") or "Unbekannt"
    lines = [
        f"🎮 {name} hat ein neues Match gestartet!",
        f"🗺️ Karte: {map_name}",
        f"📋 Ladder: {ladder}",
        f"🧬 Civ: {civ}",
    ]
    if rating is not None:
        lines.append(f"⭐ Elo: {rating}")
    return "\n".join(lines)


def build_main_menu() -> InlineKeyboardMarkup:
    """Construct the main menu keyboard."""
    buttons = [
        [
            InlineKeyboardButton("📡 Live Status", callback_data="menu|live"),
            InlineKeyboardButton("📊 Stats", callback_data="menu|stats"),
        ],
        [
            InlineKeyboardButton("📜 History", callback_data="menu|history"),
            InlineKeyboardButton("📈 Trend", callback_data="menu|trend"),
        ],
        [
            InlineKeyboardButton("⏱ Playtime", callback_data="menu|playtime"),
            InlineKeyboardButton("🧬 Civ Stats", callback_data="menu|civstats"),
        ],
        [
            InlineKeyboardButton("🎯 Civ Picker", callback_data="menu|civ"),
            InlineKeyboardButton("🏆 Leaderboard", callback_data="menu|leaderboard"),
        ],
        [
            InlineKeyboardButton("🎒 Teams", callback_data="menu|teams"),
            InlineKeyboardButton("📅 Weekly Report", callback_data="weekly_report"),
        ],
        [
            InlineKeyboardButton("⚙️ Admin", callback_data="menu|admin"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def build_player_choice_menu(prefix: str) -> InlineKeyboardMarkup:
    """Build a keyboard where each button corresponds to a player.

    Args:
        prefix (str): A prefix that will be included in the callback data to
            indicate which action is being taken on the selected player.

    Returns:
        InlineKeyboardMarkup: The keyboard markup.
    """
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for name in FRIENDS.keys():
        row.append(InlineKeyboardButton(name, callback_data=f"{prefix}|{name}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Zurück", callback_data="back|main")])
    return InlineKeyboardMarkup(rows)


def build_admin_menu() -> InlineKeyboardMarkup:
    """Admin menu for setting thresholds and toggles."""
    buttons = [
        [
            InlineKeyboardButton("Toggle Memes", callback_data="admin|memes"),
            InlineKeyboardButton("Set Tilt Threshold", callback_data="admin|tilt"),
        ],
        [
            InlineKeyboardButton("Set Elo Alert", callback_data="admin|elo"),
        ],
        [
            InlineKeyboardButton("⬅️ Zurück", callback_data="back|main"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


# Simple static build orders for demonstration.  These are generic and may not
# reflect optimal play but provide a baseline reference for common openings.
BUILD_ORDERS: Dict[str, str] = {
    "generic": "6 Schafe sammeln → 4 Holzfäller → 1 Wildschwein → 3 Beeren → 1 Wildschwein → 4 Farmen → Feudalzeit → schließe Übergangseinheiten an.",
    "Mongols": "6 Schafe → 3 Holz → 4 auf Wildschwein → 1 Lämmer → Sammle nochmals Wildschwein → 4 Farmen → Aufstieg in Feudalzeit; Scouts produzieren.",
    "Britons": "6 Schafe → 3 Holz → 1 Wildschwein → 4 Beeren → 1 Wildschwein → 4 Farmen → Bogenschützen in Feudalzeit.",
}


# Static counter-civ suggestions.  These lists are highly simplified and may
# not account for all matchups but give general guidance.
COUNTER_CIVS: Dict[str, List[str]] = {
    "Dravidians": ["Persians", "Byzantines", "Poles"],
    "Mongols": ["Teutons", "Franks", "Byzantines"],
    "Britons": ["Goths", "Celts", "Byzantines"],
}


# ============================================================================
#  Command Handlers
# ============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command: greet user and show main menu."""
    user = update.effective_user
    text = (
        f"Willkommen, {user.first_name if user else 'Spieler'}!\n"
        "Ich bin dein AoE2 Insights Bot und werde Matches deiner Freunde verfolgen."
    )
    await update.message.reply_text(text, reply_markup=build_main_menu())


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /menu command: show main menu again."""
    await update.message.reply_text("🏠 Hauptmenü", reply_markup=build_main_menu())


async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /live command: choose player for live status."""
    await update.message.reply_text(
        "Wähle einen Spieler für den Live-Status:",
        reply_markup=build_player_choice_menu("live"),
    )


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /leaderboard command: show rating leaderboard based on last known ratings."""
    items = []
    for name, pdata in data["players"].items():
        # last rating is last entry in rating_history
        rating = pdata["rating_history"][-1][1] if pdata["rating_history"] else None
        items.append((name, rating))
    # sort descending by rating (None last)
    items.sort(key=lambda x: x[1] if x[1] is not None else -99999, reverse=True)
    lines = ["🏆 Elo-Leaderboard:"]
    for idx, (name, rating) in enumerate(items, 1):
        rating_display = rating if rating is not None else "N/A"
        lines.append(f"{idx}. {name}: {rating_display}")
    await update.message.reply_text("\n".join(lines))


async def teams_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /teams command: generate fair teams for all players or specified number."""
    players = list(FRIENDS.keys())
    args = context.args
    if args:
        try:
            n = int(args[0])
            # use first n players in FRIENDS order if available
            players = players[:n]
        except Exception:
            pass
    # Gather last known ratings (None as 0)
    ratings = []
    for name in players:
        pdata = data["players"][name]
        rating = pdata["rating_history"][-1][1] if pdata["rating_history"] else 1000
        ratings.append((name, rating))
    # Sort by rating descending
    ratings.sort(key=lambda x: x[1], reverse=True)
    # Greedy pairing: highest with lowest
    teams: List[List[str]] = []
    i, j = 0, len(ratings) - 1
    while i <= j:
        if i == j:
            teams.append([ratings[i][0]])
        else:
            teams.append([ratings[i][0], ratings[j][0]])
        i += 1
        j -= 1
    # Format text
    lines = ["👥 Team-Vorschlag:"]
    for idx, team in enumerate(teams, 1):
        lines.append(f"Team {idx}: " + " + ".join(team))
    await update.message.reply_text("\n".join(lines))


# ============================================================================
#  Callback query handler
# ============================================================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all button presses from inline keyboards."""
    query = update.callback_query
    await query.answer()
    data_str = query.data or ""
    # Handle special commands
    if data_str == "weekly_report":
        # Trigger weekly report immediately
        await send_weekly_report(context)
        await query.edit_message_text("Wochenbericht gesendet.", reply_markup=build_main_menu())
        return
    # Split by '|' to parse commands
    parts = data_str.split("|")
    if not parts:
        return
    if parts[0] == "back":
        # Back to previous menu
        if parts[1] == "main":
            await query.edit_message_text("🏠 Hauptmenü", reply_markup=build_main_menu())
        return
    if parts[0] == "menu":
        # Submenu selections
        sub = parts[1]
        if sub == "live":
            await query.edit_message_text(
                "Wähle einen Spieler für den Live-Status:",
                reply_markup=build_player_choice_menu("live"),
            )
        elif sub == "stats":
            await query.edit_message_text(
                "Wähle einen Spieler für Stats:",
                reply_markup=build_player_choice_menu("stats"),
            )
        elif sub == "history":
            await query.edit_message_text(
                "Wähle einen Spieler für die Spielhistorie:",
                reply_markup=build_player_choice_menu("history"),
            )
        elif sub == "trend":
            await query.edit_message_text(
                "Wähle einen Spieler für den Elo-Trend:",
                reply_markup=build_player_choice_menu("trend"),
            )
        elif sub == "playtime":
            await query.edit_message_text(
                "Wähle einen Spieler für die Spielzeit-Analyse:",
                reply_markup=build_player_choice_menu("playtime"),
            )
        elif sub == "civstats":
            await query.edit_message_text(
                "Wähle einen Spieler für Civ-Statistiken:",
                reply_markup=build_player_choice_menu("civstats"),
            )
        elif sub == "civ":
            await query.edit_message_text(
                "Wähle einen Spieler für Civ-Empfehlung:",
                reply_markup=build_player_choice_menu("civpick"),
            )
        elif sub == "leaderboard":
            # Display leaderboard immediately
            items = []
            for name, pdata in data["players"].items():
                rating = pdata["rating_history"][-1][1] if pdata["rating_history"] else None
                items.append((name, rating))
            items.sort(key=lambda x: x[1] if x[1] is not None else -99999, reverse=True)
            lines = ["🏆 Elo-Leaderboard:"]
            for idx, (name, rating) in enumerate(items, 1):
                lines.append(f"{idx}. {name}: {rating if rating is not None else 'N/A'}")
            await query.edit_message_text("\n".join(lines), reply_markup=build_main_menu())
        elif sub == "teams":
            # Show team generation using default players
            players = list(FRIENDS.keys())
            ratings = []
            for name in players:
                pdata = data["players"][name]
                rating = pdata["rating_history"][-1][1] if pdata["rating_history"] else 1000
                ratings.append((name, rating))
            ratings.sort(key=lambda x: x[1], reverse=True)
            teams: List[List[str]] = []
            i, j = 0, len(ratings) - 1
            while i <= j:
                if i == j:
                    teams.append([ratings[i][0]])
                else:
                    teams.append([ratings[i][0], ratings[j][0]])
                i += 1
                j -= 1
            lines = ["👥 Team-Vorschlag:"]
            for idx, team in enumerate(teams, 1):
                lines.append(f"Team {idx}: " + " + ".join(team))
            await query.edit_message_text("\n".join(lines), reply_markup=build_main_menu())
        elif sub == "admin":
            await query.edit_message_text(
                "⚙️ Admin Menü", reply_markup=build_admin_menu()
            )
        return
    # Player-specific actions
    if len(parts) >= 2:
        action = parts[0]
        name = parts[1]
        if name not in FRIENDS:
            await query.edit_message_text("Unbekannter Spieler.")
            return
        if action == "live":
            # Live status: show current match or info
            pid = data["players"][name]["id"]
            match = fetch_last_match(pid)
            if not match:
                await query.edit_message_text(f"Keine Matchdaten für {name} gefunden.", reply_markup=build_main_menu())
                return
            if match.get("ongoing"):
                info = extract_player_info(match, pid)
                rating = info.get("rating")
                civ = info.get("civilization") or "Unbekannt"
                map_name = match.get("map", {}).get("name") or match.get("name") or match.get("map_type") or "Unbekannt"
                started_ts = match.get("started")
                if started_ts:
                    try:
                        dt = datetime.utcfromtimestamp(int(started_ts))
                        duration = datetime.utcnow() - dt
                        minutes = int(duration.total_seconds() // 60)
                        seconds = int(duration.total_seconds() % 60)
                        dur_text = f"{minutes}:{seconds:02d}"
                    except Exception:
                        dur_text = None
                else:
                    dur_text = None
                lines = [
                    f"📡 {name} spielt gerade!",
                    f"🗺️ Karte: {map_name}",
                    f"🧬 Civ: {civ}",
                ]
                if dur_text:
                    lines.append(f"⏱ Dauer: {dur_text}")
                if rating is not None:
                    lines.append(f"⭐ Elo: {rating}")
                # Refresh and back buttons
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Aktualisieren", callback_data=f"live|{name}"), InlineKeyboardButton("⬅️ Zurück", callback_data="back|main")]
                ])
                await query.edit_message_text("\n".join(lines), reply_markup=keyboard)
            else:
                await query.edit_message_text(f"{name} hat kein laufendes Match.", reply_markup=build_main_menu())
        elif action == "stats":
            # Basic stats: last rating, total matches, win rate
            pdata = data["players"][name]
            rating = pdata["rating_history"][-1][1] if pdata["rating_history"] else None
            total = len(pdata["match_history"])
            wins = sum(1 for m in pdata["match_history"] if m["result"] == "gewonnen")
            losses = sum(1 for m in pdata["match_history"] if m["result"] == "verloren")
            winrate = f"{(wins/total*100):.1f}%" if total else "N/A"
            lines = [
                f"📊 Stats für {name}",
                f"⭐ Letztes Elo: {rating if rating is not None else 'N/A'}",
                f"📈 Spiele insgesamt: {total}",
                f"✅ Siege: {wins}",
                f"❌ Niederlagen: {losses}",
                f"📊 Winrate: {winrate}",
            ]
            await query.edit_message_text("\n".join(lines), reply_markup=build_main_menu())
        elif action == "history":
            # Last 5 matches summary
            pdata = data["players"][name]
            hist = pdata["match_history"][-5:][::-1]  # last 5
            if not hist:
                await query.edit_message_text(f"Keine Matches für {name} gespeichert.", reply_markup=build_main_menu())
                return
            lines = [f"📜 Letzte Spiele von {name}:"]
            for idx, m in enumerate(hist, 1):
                map_name = m.get("map") or "?"
                civ = m.get("civ") or "?"
                result = m.get("result") or "?"
                diff = m.get("rating_diff")
                diff_text = ""
                if diff is not None:
                    if diff > 0:
                        diff_text = f"(+{diff})"
                    elif diff < 0:
                        diff_text = f"({diff})"
                    else:
                        diff_text = "(±0)"
                lines.append(f"{idx}. {result} – {map_name} – {civ} {diff_text}")
            await query.edit_message_text("\n".join(lines), reply_markup=build_main_menu())
        elif action == "trend":
            # Elo trend over last 10 ratings
            pdata = data["players"][name]
            rh = pdata["rating_history"][-10:]
            if len(rh) < 2:
                await query.edit_message_text(f"Nicht genug Daten für Elo-Trend von {name}.", reply_markup=build_main_menu())
                return
            ratings = [r for (_, r) in rh if r is not None]
            if not ratings:
                await query.edit_message_text(f"Keine validen Elo-Daten für {name}.", reply_markup=build_main_menu())
                return
            diff = ratings[-1] - ratings[0]
            sign = "🔺" if diff > 0 else ("🔻" if diff < 0 else "➖")
            lines = [
                f"📈 Elo-Trend (letzte {len(ratings)} Spiele) für {name}",
                f"Start: {ratings[0]}",
                f"Ende: {ratings[-1]}",
                f"Differenz: {sign} {diff}",
            ]
            await query.edit_message_text("\n".join(lines), reply_markup=build_main_menu())
        elif action == "playtime":
            # Show playtime for last 7 days
            pdata = data["players"][name]
            today = datetime.utcnow().date()
            lines = [f"⏱ Spielzeit (letzte 7 Tage) für {name}"]
            total_sec = 0
            for i in range(6, -1, -1):
                date = (today - timedelta(days=i)).isoformat()
                sec = pdata["playtime"].get(date, 0)
                total_sec += sec
                minutes = int(sec // 60)
                hours = int(minutes // 60)
                minutes = minutes % 60
                if sec > 0:
                    lines.append(f"{date}: {hours}h {minutes}m")
            if not any(pdata["playtime"].get((today - timedelta(days=i)).isoformat(), 0) > 0 for i in range(7)):
                lines.append("Keine Spielzeit in den letzten 7 Tagen.")
            else:
                total_min = int(total_sec // 60)
                total_hours = total_min // 60
                total_minutes = total_min % 60
                lines.append(f"Gesamt: {total_hours}h {total_minutes}m")
            await query.edit_message_text("\n".join(lines), reply_markup=build_main_menu())
        elif action == "civstats":
            pdata = data["players"][name]
            civs = pdata["civ_stats"]
            if not civs:
                await query.edit_message_text(f"Keine Civ-Daten für {name}.", reply_markup=build_main_menu())
                return
            lines = [f"🧬 Civ-Stats für {name}"]
            for civ_name, res in sorted(civs.items(), key=lambda x: sum(x[1].values()), reverse=True):
                wins = res.get("wins", 0)
                losses = res.get("losses", 0)
                total = wins + losses
                winrate = f"{(wins/total*100):.1f}%" if total > 0 else "N/A"
                lines.append(f"{civ_name}: {wins}/{total} Siege ({winrate})")
            await query.edit_message_text("\n".join(lines), reply_markup=build_main_menu())
        elif action == "civpick":
            # Suggest a civ: best winrate or random if no data
            pdata = data["players"][name]
            civs = pdata["civ_stats"]
            if civs:
                best_civ = max(civs.items(), key=lambda x: (x[1]["wins"] / (x[1]["wins"] + x[1]["losses"])) if (x[1]["wins"] + x[1]["losses"]) > 0 else 0)[0]
                suggestion = f"Wähle {best_civ} – das ist deine erfolgreichste Civ."
            else:
                suggestion = "Wähle eine beliebige Civ – du hast noch keine Daten."  # fallback
            await query.edit_message_text(
                f"🎯 Civ-Empfehlung für {name}:\n{suggestion}", reply_markup=build_main_menu()
            )
        elif action == "admin":
            # handle admin commands with player context
            pass
    # Handle admin actions from menu
    if parts[0] == "admin":
        subaction = parts[1]
        if subaction == "memes":
            data["global"]["memes_enabled"] = not data["global"]["memes_enabled"]
            state = "aktiviert" if data["global"]["memes_enabled"] else "deaktiviert"
            await query.edit_message_text(f"Meme-Modus {state}.", reply_markup=build_main_menu())
            save_data(data)
        elif subaction == "tilt":
            # When admin|tilt pressed, show player choice
            await query.edit_message_text(
                "Wähle einen Spieler, um die Tilt-Schwelle anzupassen:",
                reply_markup=build_player_choice_menu("settilt"),
            )
        elif subaction == "elo":
            await query.edit_message_text(
                "Wähle einen Spieler, um einen Elo-Alert zu setzen:",
                reply_markup=build_player_choice_menu("setelo"),
            )
        return
    # Specific admin operations
    if parts[0] == "settilt":
        name = parts[1]
        # Ask user to send a message with the new threshold
        await query.edit_message_text(f"Sende den neuen Tilt-Schwellenwert für {name} als Nachricht im Chat.")
        # Store context for next message
        context.user_data["awaiting_tilt"] = name
        return
    if parts[0] == "setelo":
        name = parts[1]
        await query.edit_message_text(f"Sende den Elo-Wert für den Alert für {name} als Nachricht.")
        context.user_data["awaiting_elo"] = name
        return


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages used for admin input after pressing buttons."""
    text = update.message.text.strip()
    # Check if user is expected to provide tilt threshold
    if context.user_data.get("awaiting_tilt"):
        name = context.user_data.pop("awaiting_tilt")
        try:
            val = int(text)
            data["players"][name]["tilt_threshold"] = val
            save_data(data)
            await update.message.reply_text(f"Tilt-Schwelle für {name} gesetzt auf {val}.")
        except Exception:
            await update.message.reply_text("Ungültiger Wert. Bitte gib eine Zahl ein.")
        return
    # Check if user is expected to provide elo alert
    if context.user_data.get("awaiting_elo"):
        name = context.user_data.pop("awaiting_elo")
        try:
            val = int(text)
            pdata = data["players"][name]
            if val not in pdata["elo_thresholds"]:
                pdata["elo_thresholds"].append(val)
                save_data(data)
                await update.message.reply_text(f"Elo-Alert für {name} bei {val} gesetzt.")
            else:
                await update.message.reply_text(f"Dieser Elo-Alert existiert bereits für {name}.")
        except Exception:
            await update.message.reply_text("Ungültiger Wert. Bitte gib eine Zahl ein.")
        return
    # Otherwise, ignore or provide help
    await update.message.reply_text("Unbekannter Befehl oder Kontext. Benutze /menu um zurückzugehen.")


# ============================================================================
#  Background job for monitoring matches
# ============================================================================

async def check_friends(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: poll AoE2Insights for each friend and detect match start/end."""
    bot = context.bot
    for name, pid in FRIENDS.items():
        pdata = data["players"][name]
        match = fetch_last_match(pid)
        if not match:
            continue
        current_id = pdata.get("current_match_id")
        match_id = match.get("uuid")
        ongoing = bool(match.get("ongoing"))
        # Update rating history even during match
        info = extract_player_info(match, pid)
        rating = info.get("rating")
        if rating is not None:
            pdata["rating_history"].append((datetime.utcnow().isoformat(), rating))
            if len(pdata["rating_history"]) > 100:
                pdata["rating_history"] = pdata["rating_history"][-100:]
        # If there is an ongoing match
        if ongoing:
            if current_id != match_id:
                # New match started
                pdata["current_match_id"] = match_id
                pdata["current_match_start_rating"] = rating
                summary = record_match_start(name, match)
                try:
                    await bot.send_message(chat_id=CHAT_ID, text=summary)
                except Exception as e:
                    logger.error(f"Fehler beim Senden der Start-Meldung: {e}")
        else:
            # Match finished; if the ID matches our current match ID, record it
            if current_id and current_id == match_id:
                summary = record_match_end(name, match)
                # Check Elo alerts
                rating_after = info.get("rating")
                if rating_after is not None:
                    for threshold in pdata.get("elo_thresholds", []):
                        if threshold not in pdata.get("triggered_elo_alerts", []) and rating_after >= threshold:
                            alert_text = f"🎯 {name} hat die Elo-Schwelle {threshold} erreicht!"
                            pdata.setdefault("triggered_elo_alerts", []).append(threshold)
                            summary += "\n" + alert_text
                # Send summary and any streak/tilt alerts
                try:
                    await bot.send_message(chat_id=CHAT_ID, text=summary)
                except Exception as e:
                    logger.error(f"Fehler beim Senden der End-Meldung: {e}")
        # Save after processing each friend
    save_data(data)


async def send_weekly_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Compile and send a weekly report summarising players' performances."""
    bot = context.bot
    last_sent_iso = data["global"].get("weekly_last_sent")
    last_sent = None
    if last_sent_iso:
        try:
            last_sent = datetime.fromisoformat(last_sent_iso)
        except Exception:
            last_sent = None
    now = datetime.utcnow()
    report_lines: List[str] = []
    report_lines.append("📅 Wochenbericht (letzte 7 Tage):")
    for name, pdata in data["players"].items():
        # Gather matches in last 7 days
        recent_matches = [
            m for m in pdata["match_history"]
            if (now - datetime.fromisoformat(m["time"])) < timedelta(days=7)
        ]
        wins = sum(1 for m in recent_matches if m["result"] == "gewonnen")
        losses = sum(1 for m in recent_matches if m["result"] == "verloren")
        total = len(recent_matches)
        if total == 0:
            report_lines.append(f"{name}: Keine Spiele.")
            continue
        # Most played civ
        civ_count: Dict[str, int] = {}
        for m in recent_matches:
            civ_count[m["civ"]] = civ_count.get(m["civ"], 0) + 1
        top_civ = max(civ_count.items(), key=lambda x: x[1])[0]
        # Playtime
        play_sec = sum(m.get("duration_sec", 0) or 0 for m in recent_matches)
        play_min = int(play_sec // 60)
        play_hr = play_min // 60
        play_min = play_min % 60
        report_lines.append(
            f"{name}: {wins}/{total} Siege, Top Civ: {top_civ}, Spielzeit: {play_hr}h {play_min}m"
        )
    # Prevent duplicate sending within same day
    if last_sent and (now - last_sent) < timedelta(days=6):
        # Already sent recently; don't spam but update timestamp
        data["global"]["weekly_last_sent"] = now.isoformat()
        save_data(data)
        return
    # Send report
    try:
        await bot.send_message(chat_id=CHAT_ID, text="\n".join(report_lines))
        data["global"]["weekly_last_sent"] = now.isoformat()
        save_data(data)
    except Exception as e:
        logger.error(f"Fehler beim Senden des Wochenberichts: {e}")


# ============================================================================
#  Main entry point and application setup
# ============================================================================

async def main() -> None:
    """Entrypoint: create the Telegram application and run it."""
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN ist nicht gesetzt. Bitte setze die Umgebungsvariable BOT_TOKEN."
        )
    if CHAT_ID is None:
        raise RuntimeError(
            "CHAT_ID ist nicht gesetzt. Bitte setze die Umgebungsvariable CHAT_ID mit der Gruppen-ID."
        )
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("live", live_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CommandHandler("teams", teams_command))
    # General message handler for admin inputs
    app.add_handler(CommandHandler("weekly", lambda u, c: send_weekly_report(c)))
    app.add_handler(CallbackQueryHandler(callback_handler))
    # Fallback text handler for admin thresholds
    app.add_handler(CommandHandler("help", menu_command))
    # Use a message handler to capture text input for thresholds
    from telegram.ext import MessageHandler, filters
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    # Job: poll AoE2Insights
    app.job_queue.run_repeating(check_friends, interval=CHECK_INTERVAL, first=5)
    # Job: weekly report every 7 days
    app.job_queue.run_repeating(send_weekly_report, interval=7 * 24 * 3600, first=3600)
    # Start polling
    await app.run_polling()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())