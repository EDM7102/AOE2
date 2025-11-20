"""
AoE2 Match Tracking Bot (Improved)
=================================

This script implements a Telegram bot that monitors Age of Empires II matches
for a group of friends.  The original version relied on an undocumented API
endpoint provided by AoE2Insights.  As of late 2025 that endpoint is no
longer publicly accessible, resulting in failed requests and empty results.

The new version focuses on robustness and extensibility.  It retains the
original feature set—live status, basic statistics, recent history and a
leaderboard—but gracefully handles API failures.  When the external service
is unreachable the bot will fall back to showing only direct profile links
for each player.  Additionally, the API base URL can be overridden via an
environment variable (`AOE_API_BASE`) so that alternative data sources can
easily be tested without changing the code.  Should the API behaviour
change again in the future the endpoint paths can be configured through
`AOE_API_LASTMATCH_PATH` and `AOE_API_MATCHES_PATH` environment variables.

Key improvements over the old implementation:

* **Graceful failure handling:** All API requests are wrapped in try/except
  blocks and will not crash the bot.  Errors are logged and the UI falls
  back to a helpful message.
* **Configurable API endpoints:** The base URL as well as the relative
  paths for the last match and recent matches endpoints can be set via
  environment variables.  This makes it possible to switch to a different
  provider (such as an official or community API) without code changes.
* **Improved user feedback:** When data cannot be retrieved the bot
  explicitly tells the user that no data is available and provides a link
  to the player’s profile on AoE2Insights.

Before running the bot make sure you have installed the `python-telegram-bot`
package (version 13 or later) and that you have set the required environment
variables listed below.

Environment variables
---------------------

```
BOT_TOKEN             The API token for your Telegram bot (required).
CHAT_ID               The ID of the chat where notifications will be sent
                      (required).  For group chats this is a negative number.
AOE_API_BASE          Base URL of the API to query (optional).
                      Defaults to "https://www.aoe2insights.com/api".
AOE_API_LASTMATCH_PATH  Path segment used to fetch the last match for a
                      player.  Defaults to "/player/{id}/lastmatch/".
AOE_API_MATCHES_PATH    Path segment used to fetch recent matches for a
                      player.  Defaults to "/player/{id}/matches/".
CHECK_INTERVAL        How often (in seconds) to poll the API for new
                      matches (optional, default 60 seconds).
```

The friends you want to monitor must be defined in the `FRIENDS` dictionary
below.  Use their AoE2Insights profile IDs as values.  If you want to
support multiple data sources you can include IDs from other services here
as long as the selected API understands them.

If you decide to experiment with a different API (for example the Relic
community API or the official Age II stats API) you can set `AOE_API_BASE`
accordingly and adjust the last match and matches paths.  The request
functions in this script simply append the paths to the base URL and do
not assume a specific schema beyond returning JSON.
"""

import os
import logging
from typing import Dict, Optional, Any, List, Tuple

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

# Telegram bot token and chat ID come from environment variables.  Raise
# exceptions early if mandatory values are missing so that misconfigurations
# are detected immediately.
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID_ENV = os.getenv("CHAT_ID")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is not set. Please provide your Telegram bot token via the BOT_TOKEN environment variable."
    )

if not CHAT_ID_ENV:
    raise RuntimeError(
        "CHAT_ID is not set. Please provide the target chat ID via the CHAT_ID environment variable."
    )

try:
    CHAT_ID = int(CHAT_ID_ENV)
except ValueError:
    raise RuntimeError(
        "CHAT_ID must be a numeric ID (e.g. -1003484505307 for group chats)."
    )

# Configure the API base URL and endpoint paths via environment variables.  If
# the AoE2Insights API ever changes its structure you can override these
# values without editing the code.  For example to use the Relic community
# API set AOE_API_BASE="https://aoe-api.reliclink.com/community/leaderboard"
# and adjust the LASTMATCH and MATCHES paths accordingly (e.g. set
# AOE_API_LASTMATCH_PATH="/getRecentMatchHistory" and pass a suitable
# profile ID list as a query parameter in fetch_recent_matches).
AOE_API_BASE: str = os.getenv("AOE_API_BASE", "https://www.aoe2insights.com/api")
AOE_API_LASTMATCH_PATH: str = os.getenv(
    "AOE_API_LASTMATCH_PATH", "/player/{id}/lastmatch/"
)
AOE_API_MATCHES_PATH: str = os.getenv(
    "AOE_API_MATCHES_PATH", "/player/{id}/matches/"
)
CHECK_INTERVAL: int = int(os.getenv("CHECK_INTERVAL", "60"))

# Define your friends here.  The keys are display names used in Telegram
# messages, the values are the profile IDs.  You can add more entries as
# needed.  Do not set duplicate names.
FRIENDS: Dict[str, int] = {
    "EDM7101": 10770866,
    "JustForFun": 10769949,
    "rollthedice": 10775508,
}

# ===================== LOGGING =====================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===================== STATE =====================

# Track the current match for each player (by UUID).  None means no match in
# progress.  We also remember the rating before a match starts to compute
# rating differences afterwards, plus the last known rating and win/loss
# streaks.  All these dictionaries are keyed by player name.
current_matches: Dict[str, Optional[str]] = {name: None for name in FRIENDS}
rating_before_match: Dict[str, Optional[int]] = {name: None for name in FRIENDS}
last_ratings: Dict[str, Optional[int]] = {name: None for name in FRIENDS}
streaks: Dict[str, int] = {name: 0 for name in FRIENDS}

# ===================== HELPER =====================

def profile_url(player_name: str) -> str:
    """Return the AoE2Insights profile URL for a player."""
    pid = FRIENDS[player_name]
    return f"https://www.aoe2insights.com/user/{pid}/"


def _build_url(path_template: str, player_id: int) -> str:
    """
    Substitute the player's ID into a path template and combine it with the
    configured API base URL.  Templates should contain "{id}" where the
    numeric player ID should be inserted.  Leading and trailing slashes are
    handled gracefully.
    """
    # Ensure the base does not end with a slash to avoid double slashes.
    base = AOE_API_BASE.rstrip("/")
    # Substitute the ID into the path and ensure it begins with a slash.
    path = path_template.format(id=player_id)
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}"


def fetch_json(url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    """
    Perform a HTTP GET request to the given URL and return the parsed JSON
    response.  If the request fails (HTTP errors, JSON decode errors or
    connection problems) None is returned and the exception is logged.  This
    helper centralises error handling so that the calling functions can
    remain concise.
    """
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.HTTPError as e:
        logger.error(f"HTTP error fetching {url}: {e}")
    except requests.RequestException as e:
        logger.error(f"Connection error fetching {url}: {e}")
    except ValueError as e:
        # JSON decoding error
        logger.error(f"Failed to decode JSON from {url}: {e}")
    return None


def fetch_last_match(player_id: int) -> Optional[Dict[str, Any]]:
    """
    Fetch the last match for a player from the configured API.  Returns a
    dictionary describing the match or None if no data is available.  This
    implementation simply calls the configured last match endpoint and
    attempts to extract the match dictionary from the response.  For
    AoE2Insights the response schema contains a top level key "match" with
    the match details.  If your chosen API uses a different schema you
    should adjust the parsing accordingly.
    """
    url = _build_url(AOE_API_LASTMATCH_PATH, player_id)
    data = fetch_json(url)
    if not data:
        return None
    # AoE2Insights returned a dict with key "match".  Other APIs may
    # structure this differently.  Try several common keys and fall back to
    # returning the entire response if nothing matches.
    for key in ("match", "last_match", "data", "result"):
        if isinstance(data, dict) and key in data:
            return data[key]
    # If the response itself looks like a match object (has players etc.)
    if isinstance(data, dict) and "players" in data:
        return data
    logger.debug(f"Unexpected last match schema: {data}")
    return None


def fetch_recent_matches(player_id: int, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Fetch a list of recent matches for a player.  The number of matches
    returned is limited by ``limit``.  The configured MATCHES endpoint is
    called and several possible keys are tried to extract the list.  If
    nothing matches an empty list is returned.
    """
    url = _build_url(AOE_API_MATCHES_PATH, player_id)
    data = fetch_json(url, params={"count": limit, "game": "aoe2de"})
    if not data:
        return []
    # Try to extract matches from common keys.  For AoE2Insights the key
    # "matches" was used.  For other APIs the key may be "matchHistory",
    # "results", "data" or simply the response itself.
    for key in ("matches", "matchHistory", "results", "data"):
        val = data.get(key) if isinstance(data, dict) else None
        if isinstance(val, list):
            return val[:limit]
    # Fallback: if the response is already a list assume it is the list of
    # matches.
    if isinstance(data, list):
        return data[:limit]
    logger.debug(f"Unexpected recent matches schema: {data}")
    return []


def get_player_rating_from_match(match: Dict[str, Any], player_id: int) -> Optional[int]:
    """
    Extract the player's rating from the match.  This helper supports
    multiple possible field names for the rating (e.g. ``rating``,
    ``new_rating``, ``current_rating``) and will return the first one it
    finds.  Returns None if no rating is present.
    """
    players = match.get("players", [])
    for p in players:
        # AoE2Insights used key "player_id".  Other APIs may use
        # "profile_id" or "profileId".  Try a few possibilities.
        pid = p.get("player_id") or p.get("profile_id") or p.get("profileId")
        if pid == player_id:
            # Try different rating field names.
            for rating_key in (
                "rating",
                "new_rating",
                "newRating",
                "current_rating",
                "elo",
            ):
                if rating_key in p and isinstance(p[rating_key], int):
                    return p[rating_key]
    return None


def get_player_civ_from_match(match: Dict[str, Any], player_id: int) -> Optional[str]:
    """
    Extract the civilization name from the match data for the given player.
    Supports multiple schema variants: AoE2Insights nested a dict under
    ``civ`` with a ``name`` key, while other APIs may store a string under
    ``civ`` or ``civilization``.
    """
    players = match.get("players", [])
    for p in players:
        pid = p.get("player_id") or p.get("profile_id") or p.get("profileId")
        if pid == player_id:
            civ = p.get("civ") or p.get("civilization") or p.get("civName")
            if isinstance(civ, dict):
                return civ.get("name")
            return civ
    return None


def parse_result_from_rating(before: Optional[int], after: Optional[int]) -> str:
    """Derive a textual result (Win/Loss/Unentschieden) from rating change."""
    if before is None or after is None:
        return "Ergebnis unbekannt"
    if after > before:
        return "Win"
    if after < before:
        return "Loss"
    return "Unentschieden / kein Elo-Change"


def sanitize_timestamp(ts: Any) -> Optional[str]:
    """
    Some APIs may return timestamps in seconds since epoch, while others
    return ISO8601 strings.  This helper normalises a variety of inputs to
    a human-friendly string.  If parsing fails, the original value is
    returned unchanged (as a string).  For AoE2Insights the timestamp is
    already a string like '2025-11-17 21:53:02'.
    """
    if ts is None:
        return None
    # If it's already a string return it directly
    if isinstance(ts, str):
        return ts
    # If it's a number treat it as a UNIX timestamp
    try:
        import datetime as _dt  # import lazily to avoid overhead
        if isinstance(ts, (int, float)):
            dt = _dt.datetime.utcfromtimestamp(ts)
            return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        pass
    # Fallback: convert to string
    return str(ts)


# ===================== FORMATTER =====================

def format_match_start(player_name: str, match: Dict[str, Any], rating_before: Optional[int]) -> str:
    """Format a notification for a newly started match."""
    map_name = match.get("map", {}).get("name") if isinstance(match.get("map"), dict) else match.get("map", "Unbekannt")
    uuid = match.get("uuid") or match.get("match_id") or match.get("matchId")
    leaderboard = match.get("leaderboard", "Unbekannt")
    started_raw = match.get("started") or match.get("started_at") or match.get("matchstart")
    started = sanitize_timestamp(started_raw)
    pid = FRIENDS[player_name]
    civ = get_player_civ_from_match(match, pid)

    lines = [
        f"🎮 {player_name} hat ein neues Match gestartet!",
        f"📋 Ladder: {leaderboard}",
        f"🗺 Karte: {map_name or 'Unbekannt'}",
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
    """Format a notification for a completed match."""
    map_name = match.get("map", {}).get("name") if isinstance(match.get("map"), dict) else match.get("map", "Unbekannt")
    leaderboard = match.get("leaderboard", "Unbekannt")
    uuid = match.get("uuid") or match.get("match_id") or match.get("matchId")
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
        f"🗺 Karte: {map_name or 'Unbekannt'}",
        f"🏆 Ergebnis: {result}",
    ]
    if civ:
        lines.append(f"🧬 Civ: {civ}")
    if diff_text:
        lines.append(diff_text)
    if uuid:
        lines.append(f"🆔 Match-ID: {uuid}")

    st = streaks.get(player_name, 0)
    if st >= 3:
        lines.append(f"🔥 Win-Streak: {st} in Folge!")
    elif st <= -3:
        lines.append(f"⚠️ Tilt-Warnung: {abs(st)} Niederlagen in Folge!")
    return "\n".join(lines)


def format_live_status(player_name: str, match: Optional[Dict[str, Any]]) -> str:
    """
    Format the live status text for a player.  If ``match`` is None (because
    the API returned no data) a fallback message with a profile link is
    shown.
    """
    if not match:
        return (
            f"📡 Live-Status für {player_name}\n"
            f"Zurzeit konnten keine Matchdaten vom API geladen werden.\n"
            f"Entweder existiert noch kein Match oder der Dienst ist nicht erreichbar.\n\n"
            f"🔗 Direktes Profil: {profile_url(player_name)}"
        )

    pid = FRIENDS[player_name]
    map_name = match.get("map", {}).get("name") if isinstance(match.get("map"), dict) else match.get("map", "Unbekannt")
    started_raw = match.get("started") or match.get("started_at") or match.get("matchstart")
    started = sanitize_timestamp(started_raw)
    leaderboard = match.get("leaderboard", "Unbekannt")
    ongoing = match.get("ongoing", False) or bool(match.get("is_ongoing"))
    rating = get_player_rating_from_match(match, pid)
    civ = get_player_civ_from_match(match, pid)

    lines = [
        f"📡 Live-Status für {player_name}",
        f"📋 Ladder: {leaderboard}",
        f"🗺 Karte: {map_name or 'Unbekannt'}",
        f"🔁 Läuft: {'Ja' if ongoing else 'Nein'}",
    ]
    if civ:
        lines.append(f"🧬 Civ: {civ}")
    if rating is not None:
        lines.append(f"⭐ Aktuelles Elo (Matchdaten): {rating}")
    if started:
        lines.append(f"⏱ Start: {started}")
    lines.append(f"🔗 Profil: {profile_url(player_name)}")
    return "\n".join(lines)


def format_basic_stats(player_name: str, last_match: Optional[Dict[str, Any]]) -> str:
    """
    Display basic statistics based on the last match plus the stored streak.
    If no match data is available a fallback message with a profile link is
    shown.
    """
    pid = FRIENDS[player_name]
    rating = last_ratings.get(player_name)
    map_name: Optional[str] = "unbekannt"
    civ: Optional[str] = None
    started: Optional[str] = None

    if last_match:
        map_name = (
            last_match.get("map", {}).get("name")
            if isinstance(last_match.get("map"), dict)
            else last_match.get("map", "Unbekannt")
        )
        civ = get_player_civ_from_match(last_match, pid)
        started_raw = last_match.get("started") or last_match.get("started_at") or last_match.get("matchstart")
        started = sanitize_timestamp(started_raw)
        match_rating = get_player_rating_from_match(last_match, pid)
        if rating is None and match_rating is not None:
            rating = match_rating
            last_ratings[player_name] = match_rating

    rating_text = rating if rating is not None else "unbekannt"
    st = streaks.get(player_name, 0)

    if st > 0:
        streak_text = f"🔥 {st} Wins in Folge"
    elif st < 0:
        streak_text = f"⚠️ {abs(st)} Losses in Folge"
    else:
        streak_text = "keine Serie aktuell"

    lines = [
        f"📊 Basic Stats für {player_name}",
        f"⭐ Elo (zuletzt bekannt): {rating_text}",
    ]

    if last_match:
        lines.append(f"🗺 Letzte Map: {map_name}")
        if civ:
            lines.append(f"🧬 Letzte Civ: {civ}")
        if started:
            lines.append(f"⏱ Letztes Match: {started}")
    else:
        lines.append(
            "ℹ️ Es konnten keine Matchdaten geladen werden (API liefert keine Daten oder der Dienst ist nicht erreichbar)."
        )

    lines.append(f"📈 Streak: {streak_text}")
    lines.append(f"🔗 Profil: {profile_url(player_name)}")
    return "\n".join(lines)


def format_history(player_name: str, matches: List[Dict[str, Any]]) -> str:
    """
    Format a list of recent matches into a readable history.  If no matches
    are provided a fallback message is returned.
    """
    if not matches:
        return (
            f"📜 Match-History – {player_name}\n\n"
            f"Zurzeit konnten keine Matchdaten vom API geladen werden (leere Antwort oder Fehler).\n\n"
            f"🔗 Schau direkt auf AoE2Insights nach:\n{profile_url(player_name)}"
        )

    pid = FRIENDS[player_name]
    lines = [f"📜 Letzte Matches – {player_name}"]

    for m in matches:
        map_name = (
            m.get("map", {}).get("name")
            if isinstance(m.get("map"), dict)
            else m.get("map", "Unbekannt")
        )
        started_raw = m.get("started") or m.get("started_at") or m.get("matchstart")
        started = sanitize_timestamp(started_raw)
        civ = get_player_civ_from_match(m, pid)
        after = get_player_rating_from_match(m, pid)

        line_parts: List[str] = []
        line_parts.append(map_name or "Unbekannt")
        if civ:
            line_parts.append(f"Civ: {civ}")
        if after is not None:
            line_parts.append(f"Elo: {after}")
        if started:
            line_parts.append(started)
        # Join parts with " – "
        lines.append("• " + " – ".join(line_parts))

    lines.append(f"\n🔗 Mehr Details: {profile_url(player_name)}")
    return "\n".join(lines)


def format_leaderboard() -> str:
    """
    Build a simple leaderboard based on the last known ratings.  Players
    without a known rating will be sorted below those with ratings.
    """
    items: List[Tuple[str, Optional[int]]] = sorted(
        last_ratings.items(), key=lambda x: x[1] if x[1] is not None else 0, reverse=True
    )
    lines = ["🏆 Gruppen-Leaderboard (letzte bekannte Elo):"]
    for name, rating in items:
        r = rating if rating is not None else "unbekannt"
        lines.append(f"• {name}: {r}")
    return "\n".join(lines)


# ===================== MENÜS =====================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Return the main menu inline keyboard."""
    buttons = [
        [
            InlineKeyboardButton("📡 Live", callback_data="menu_live"),
            InlineKeyboardButton("📊 Stats", callback_data="menu_stats"),
        ],
        [
            InlineKeyboardButton("📜 History", callback_data="menu_history"),
            InlineKeyboardButton("🏆 Leaderboard", callback_data="menu_leaderboard"),
        ],
        [InlineKeyboardButton("ℹ️ Hilfe", callback_data="menu_help")],
    ]
    return InlineKeyboardMarkup(buttons)


def player_choice_keyboard(prefix: str) -> InlineKeyboardMarkup:
    """
    Build a keyboard listing all players.  Each button's callback data has
    the form "{prefix}|{player_name}" which allows the callback handler
    to distinguish actions.
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
    rows.append([InlineKeyboardButton("⬅️ Zurück", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


# ===================== JOB: AUTO CHECK =====================

async def check_friends(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Periodically poll the API for each friend.  If a new match starts or a
    match ends, send a Telegram notification.  The polling interval is
    configured via the CHECK_INTERVAL environment variable.  Errors are
    logged rather than raising exceptions so that the job continues
    running even if a particular request fails.
    """
    global current_matches, rating_before_match, last_ratings, streaks

    bot = context.bot

    for name, pid in FRIENDS.items():
        match = fetch_last_match(pid)
        last_uuid = current_matches.get(name)
        before = rating_before_match.get(name)

        if not match:
            # No data returned.  Skip sending notifications.  We'll fall
            # back to direct profile links when the user requests status
            continue

        ongoing = match.get("ongoing", False) or bool(match.get("is_ongoing"))
        uuid = str(match.get("uuid") or match.get("match_id") or match.get("matchId"))
        rating_now = get_player_rating_from_match(match, pid)

        if rating_now is not None:
            last_ratings[name] = rating_now

        # If the match is ongoing
        if ongoing:
            if last_uuid != uuid:
                # A new match has started
                current_matches[name] = uuid
                rating_before_match[name] = last_ratings.get(name) or rating_now

                text = format_match_start(name, match, rating_before_match[name])
                try:
                    await bot.send_message(chat_id=CHAT_ID, text=text)
                except Exception as e:
                    logger.error(f"Error sending match start for {name}: {e}")
            continue

        # Not ongoing any more.  If we previously recorded this match as current
        if last_uuid == uuid and last_uuid is not None:
            after = rating_now
            text = format_match_end(name, match, before, after)

            # Update streaks
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
                logger.error(f"Error sending match end for {name}: {e}")

            current_matches[name] = None
            rating_before_match[name] = None


# ===================== COMMAND HANDLER =====================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Respond to /start by explaining the bot's purpose and showing the main
    menu.  Also mention that the bot may fall back to profile links when
    the external API is unavailable.
    """
    text = (
        "👋 AoE2 Match Tracker ist aktiv.\n\n"
        "Ich versuche (soweit das konfigurierte API Daten liefert) Matches von:\n"
        "• EDM7101\n"
        "• JustForFun\n"
        "• rollthedice\n\n"
        "Nutze das Menü unten für Live-Status, Stats, History und Leaderboard.\n"
        "Hinweis: Wenn das API keine Daten liefert (404/500), siehst du stattdessen einen Hinweis und einen direkten Profil-Link."
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🏠 Hauptmenü", reply_markup=main_menu_keyboard())


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = format_leaderboard()
    await update.message.reply_text(text)


async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Wähle einen Spieler für den Live-Status:", reply_markup=player_choice_keyboard("live")
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Wähle einen Spieler für Basic Stats:", reply_markup=player_choice_keyboard("stats")
    )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Wähle einen Spieler für Match-History:", reply_markup=player_choice_keyboard("history")
    )


# ===================== CALLBACK HANDLER (BUTTONS) =====================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    # Main menu navigation
    if data == "menu_live":
        await query.edit_message_text(
            "Wähle einen Spieler für den Live-Status:", reply_markup=player_choice_keyboard("live")
        )
        return
    if data == "menu_stats":
        await query.edit_message_text(
            "Wähle einen Spieler für Basic Stats:", reply_markup=player_choice_keyboard("stats")
        )
        return
    if data == "menu_history":
        await query.edit_message_text(
            "Wähle einen Spieler für Match-History:", reply_markup=player_choice_keyboard("history")
        )
        return
    if data == "menu_leaderboard":
        text = format_leaderboard()
        await query.edit_message_text(text, reply_markup=main_menu_keyboard())
        return
    if data == "menu_help":
        text = (
            "ℹ️ Hilfe\n\n"
            "• Der Bot versucht, über das konfigurierte API Matchdaten zu holen.\n"
            "• Wenn das API 404/500 liefert oder keine Daten zurückgibt, bekommst du stattdessen einen Hinweis und den direkten Profil-Link.\n"
            "• Auto-Alerts bei Matchstart/Matchende funktionieren nur, wenn das API die Daten rechtzeitig bereitstellt.\n\n"
            f"Aktuell konfiguriertes API: {AOE_API_BASE}"
        )
        await query.edit_message_text(text, reply_markup=main_menu_keyboard())
        return
    if data == "back_main":
        await query.edit_message_text("🏠 Hauptmenü", reply_markup=main_menu_keyboard())
        return

    # Handle player-specific actions: prefix|Name
    if "|" in data:
        prefix, name = data.split("|", 1)
        if name not in FRIENDS:
            await query.edit_message_text("Unbekannter Spieler.", reply_markup=main_menu_keyboard())
            return
        pid = FRIENDS[name]
        if prefix == "live":
            match = fetch_last_match(pid)
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
            last_match = fetch_last_match(pid)
            text = format_basic_stats(name, last_match)
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📡 Live-Status", callback_data=f"live|{name}")],
                    [InlineKeyboardButton("📜 History", callback_data=f"history|{name}")],
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
    """Start the Telegram bot application and register handlers."""
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # Register commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("live", live_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    # Register callback handler for buttons
    app.add_handler(CallbackQueryHandler(callback_handler))
    # Add the periodic job to check matches
    app.job_queue.run_repeating(check_friends, interval=CHECK_INTERVAL, first=5)
    # Start polling
    app.run_polling()


if __name__ == "__main__":
    main()
