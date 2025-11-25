import argparse
import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests
from requests.exceptions import RequestException
from bs4 import BeautifulSoup
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

CHAT_ID_ENV = os.getenv("CHAT_ID")  # aktuell nicht zwingend nötig

# Deine drei Spieler
FRIENDS: Dict[str, int] = {
    "EDM7101": 10770866,
    "JustForFun": 10769949,
    "rollthedice": 10775508,
}

# Offline-Demo-HTMLs, damit das CLI auch ohne Internet lauffähig bleibt
OFFLINE_HTML: Dict[int, str] = {
    10770866: """
    <html><body>
    <h1>EDM7101</h1>
    <div>Game Id: 10770866</div>
    <div>Rankings</div>
    <div>1v1 RM Rating 1499 All Time High: 1550</div>
    <div>Team RM #321 Rating 1450 All Time High: 1500</div>
    <div>About</div>
    <p>EDM liebt schnelle Rushes und hat insgesamt eine record of playing 812 matches.</p>
    <p>He chooses Byzantines as their favorite civilization with a pick probability of 41% and a win rate of 56% in 210 matches.</p>
    <p>They show a win rate of 61% across 180 matches and excels on the Arabia map.</p>
    <p>Player dominates the Pocket position with a win rate of 58% from 130 matches.</p>
    <div>Ratings</div>
    <div>Last matches</div>
    <div>RM 1v1</div><div>Arabia</div><div>38:12</div><div>5 hours ago</div><div>#1234567</div>
    <div>Team RM</div><div>Acropolis</div><div>32:01</div><div>1 day ago</div><div>#1234566</div>
    </body></html>
    """,
    10769949: """
    <html><body>
    <h1>JustForFun</h1>
    <div>Game Id: 10769949</div>
    <div>Rankings</div>
    <div>1v1 RM Rating 1320 All Time High: 1400</div>
    <div>Team RM Rating 1340 All Time High: 1420</div>
    <div>About</div>
    <p>record of playing 640 matches.</p>
    <p>chooses Mayans as their favorite civilization with a pick probability of 35% and a win rate of 52% in 180 matches.</p>
    <p>win rate of 55% across 150 matches and excels on the Arena map.</p>
    <p>dominates the Flank position with a win rate of 53% from 120 matches.</p>
    <div>Ratings</div>
    <div>Last matches</div>
    <div>RM AUTOMATCH</div><div>Serengeti</div><div>29:44</div><div>3 hours ago</div><div>#2233445</div>
    </body></html>
    """,
    10775508: """
    <html><body>
    <h1>rollthedice</h1>
    <div>Game Id: 10775508</div>
    <div>Rankings</div>
    <div>1v1 RM Rating 1250 All Time High: 1330</div>
    <div>Team RM #890 Rating 1360 All Time High: 1410</div>
    <div>About</div>
    <p>record of playing 410 matches.</p>
    <p>chooses Vikings as their favorite civilization with a pick probability of 44% and a win rate of 57% in 170 matches.</p>
    <p>win rate of 59% across 140 matches and excels on the Black Forest map.</p>
    <p>dominates the Pocket position with a win rate of 60% from 100 matches.</p>
    <div>Ratings</div>
    <div>Last matches</div>
    <div>RM 1v1</div><div>Four Lakes</div><div>31:20</div><div>2 hours ago</div><div>#8899771</div>
    </body></html>
    """,
}

AOE_BASE_URL = "https://www.aoe2insights.com"

# ===================== LOGGING =====================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ===================== DATA CLASSES =====================

@dataclass
class LadderInfo:
    rating_1v1_rm: Optional[int] = None
    ath_1v1_rm: Optional[int] = None
    rating_team_rm: Optional[int] = None
    ath_team_rm: Optional[int] = None
    rank_team_rm: Optional[int] = None


@dataclass
class MatchInfo:
    mode: str
    map_name: str
    duration: str
    when: str
    match_id: str


@dataclass
class PlayerProfile:
    name: str
    game_id: int
    country: Optional[str] = None
    about_text: Optional[str] = None
    ladder: LadderInfo = field(default_factory=LadderInfo)
    total_matches: Optional[int] = None
    fav_civ_line: Optional[str] = None
    fav_map_line: Optional[str] = None
    fav_role_line: Optional[str] = None
    last_matches: List[MatchInfo] = field(default_factory=list)


# ===================== SCRAPING =====================

def profile_url(player_id: int) -> str:
    return f"{AOE_BASE_URL}/user/{player_id}/"


def parse_player_html(html_text: str, player_id: int) -> PlayerProfile:
    """Parst ein AoE2Insights-Profil-HTML in eine PlayerProfile-Struktur."""
    soup = BeautifulSoup(html_text, "html.parser")
    full_text = soup.get_text(separator="\n")

    # Name und Game Id
    name = str(player_id)
    game_id = player_id
    country = None

    lines = [line.strip() for line in full_text.splitlines()]
    for i, line in enumerate(lines):
        if f"Game Id: {player_id}" in line:
            game_id = player_id
            for j in range(i - 1, max(i - 5, -1), -1):
                candidate = lines[j].strip()
                if candidate and "aka." not in candidate:
                    name = candidate
                    break
            break

    # Rankings
    ladder = LadderInfo()
    try:
        idx_rank = full_text.index("Rankings")
        text_rank = full_text[idx_rank:]
    except ValueError:
        text_rank = ""

    m_1v1 = re.search(r"1v1 RM.*?Rating\s+(\d+).*?All Time High:\s+(\d+)", text_rank, re.DOTALL)
    if m_1v1:
        ladder.rating_1v1_rm = int(m_1v1.group(1))
        ladder.ath_1v1_rm = int(m_1v1.group(2))

    m_team = re.search(
        r"Team RM.*?(?:#(\d+))?.*?Rating\s+(\d+).*?All Time High:\s+(\d+)",
        text_rank,
        re.DOTALL,
    )
    if m_team:
        if m_team.group(1):
            ladder.rank_team_rm = int(m_team.group(1))
        ladder.rating_team_rm = int(m_team.group(2))
        ladder.ath_team_rm = int(m_team.group(3))

    # About-Text
    about_text = None
    fav_civ_line = None
    fav_map_line = None
    fav_role_line = None
    total_matches = None

    try:
        idx_about = full_text.index("About")
        try:
            idx_after = full_text.index("Ratings", idx_about + 5)
        except ValueError:
            idx_after = idx_about + 1000
        about_block = full_text[idx_about:idx_after]
        about_lines = [l.strip() for l in about_block.splitlines() if l.strip()]
        about_text = " ".join(about_lines[1:])

        m_tot = re.search(r"record of playing\s+(\d+)\s+matches", about_text)
        if m_tot:
            total_matches = int(m_tot.group(1))

        m_civ = re.search(
            r"chooses\s+(.+?)\s+as their favorite civilization.*?pick probability of\s+([\d\.]+%)"
            r".*?win rate of\s+([\d\.]+%)\s+in\s+(\d+)\s+matches",
            about_text,
        )
        if m_civ:
            fav_civ_line = (
                f"{m_civ.group(1)} – {m_civ.group(3)} WR, "
                f"{m_civ.group(2)} Pickrate ({m_civ.group(4)} Spiele)"
            )

        m_map = re.search(
            r"win rate of\s+([\d\.]+%)\s+across\s+(\d+)\s+matches.*?excels on the\s+(.+?)\s+map",
            about_text,
        )
        if m_map:
            fav_map_line = (
                f"{m_map.group(3)} – {m_map.group(1)} WR ({m_map.group(2)} Spiele)"
            )

        m_role = re.search(
            r"dominates the\s+(.+?)\s+position.*?win rate of\s+([\d\.]+%)\s+from\s+(\d+)\s+matches",
            about_text,
        )
        if m_role:
            fav_role_line = (
                f"{m_role.group(1)} – {m_role.group(2)} WR ({m_role.group(3)} Spiele)"
            )
    except ValueError:
        pass

    # Letzte Matches
    last_matches: List[MatchInfo] = []
    try:
        idx_lm = full_text.index("Last matches")
        after_lm = full_text[idx_lm:]
        lm_lines_raw = [l.strip() for l in after_lm.splitlines()]
        lm_lines = [l for l in lm_lines_raw if l]

        i = 0
        while i < len(lm_lines) - 4 and len(last_matches) < 6:
            mode = lm_lines[i]
            map_ = lm_lines[i + 1]
            duration = lm_lines[i + 2]
            when = lm_lines[i + 3]
            match_id_line = lm_lines[i + 4]

            if (
                (("v" in mode) or ("AUTOMATCH" in mode) or ("RM" in mode))
                and ("ago" in when)
                and match_id_line.startswith("#")
            ):
                mi = MatchInfo(
                    mode=mode,
                    map_name=map_,
                    duration=duration,
                    when=when,
                    match_id=match_id_line.lstrip("#"),
                )
                last_matches.append(mi)
                i += 5
            else:
                i += 1
    except ValueError:
        pass

    return PlayerProfile(
        name=name,
        game_id=game_id,
        country=country,
        about_text=about_text,
        ladder=ladder,
        total_matches=total_matches,
        fav_civ_line=fav_civ_line,
        fav_map_line=fav_map_line,
        fav_role_line=fav_role_line,
        last_matches=last_matches,
    )


def scrape_player(player_id: int) -> PlayerProfile:
    """
    Holt das HTML der Profilseite und parst:
    - Namen, Game Id
    - Rankings (1v1 RM / Team RM)
    - About-Text (Highlights)
    - letzte Matches (modus, map, dauer, wann, match-id)
    """
    url = profile_url(player_id)
    logger.info(f"Scrape {url}")
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        resp.raise_for_status()
    except RequestException as exc:
        logger.error("Fehler beim Abruf von %s: %s", url, exc)
        raise RuntimeError(f"Konnte {url} nicht laden: {exc}") from exc

    return parse_player_html(resp.text, player_id)


def scrape_player_offline(player_id: int) -> PlayerProfile:
    html = OFFLINE_HTML.get(player_id)
    if not html:
        raise RuntimeError(
            "Kein Offline-Datensatz verfügbar. Bitte Online-Modus oder einen bekannten Spieler wählen."
        )
    logger.info("Lade Offline-Daten für %s", player_id)
    return parse_player_html(html, player_id)


# ===================== FORMATTING =====================

def format_group_overview(profiles: Dict[str, PlayerProfile]) -> str:
    lines = ["🏠 AoE2 Gruppen-Übersicht", ""]
    lines.append("Spieler:")

    for name in FRIENDS.keys():
        p = profiles.get(name)
        if not p:
            lines.append(f"• {name}")
            continue
        ladder = p.ladder
        rating_1v1 = ladder.rating_1v1_rm if ladder.rating_1v1_rm is not None else "–"
        rating_team = ladder.rating_team_rm if ladder.rating_team_rm is not None else "–"
        lines.append(f"• {name}: 1v1 {rating_1v1} | Team {rating_team}")

    lines.append("")
    lines.append("Wähle einen Spieler für Details oder nutze die Gruppen-Buttons.")

    return "\n".join(lines)


def format_player_main_card(name: str, profile: PlayerProfile) -> str:
    p = profile
    l = p.ladder

    rating_1v1 = l.rating_1v1_rm if l.rating_1v1_rm is not None else "–"
    rating_team = l.rating_team_rm if l.rating_team_rm is not None else "–"
    total_matches = p.total_matches if p.total_matches is not None else "unbekannt"

    lines = [f"🇩🇪 {name} – Übersicht", ""]

    lines.append("🏆 Ladder")
    lines.append(f"• 1v1 RM: {rating_1v1}")
    if l.ath_1v1_rm is not None:
        lines.append(f"  ATH: {l.ath_1v1_rm}")
    lines.append(f"• Team RM: {rating_team}")
    if l.rank_team_rm is not None:
        lines.append(f"  Rank: #{l.rank_team_rm}")
    if l.ath_team_rm is not None:
        lines.append(f"  ATH: {l.ath_team_rm}")

    lines.append("")
    lines.append("🔥 Highlights")
    lines.append(f"• Spiele gesamt: {total_matches}")
    if p.fav_civ_line:
        lines.append(f"• Beste Civ: {p.fav_civ_line}")
    if p.fav_map_line:
        lines.append(f"• Beste Map: {p.fav_map_line}")
    if p.fav_role_line:
        lines.append(f"• Beste Rolle: {p.fav_role_line}")

    if p.last_matches:
        lm = p.last_matches[0]
        lines.append("")
        lines.append("🕹 Letztes Match")
        lines.append(f"{lm.map_name} • {lm.mode}")
        lines.append(f"{lm.duration} • {lm.when}")
        lines.append(f"Match-ID: #{lm.match_id}")

    lines.append("")
    lines.append(f"🔗 Profil: {profile_url(p.game_id)}")

    return "\n".join(lines)


def format_player_matches(name: str, profile: PlayerProfile) -> str:
    if not profile.last_matches:
        return (
            f"📜 Letzte Matches – {name}\n\n"
            "Es konnten keine Matches geparst werden."
        )

    lines = [f"📜 Letzte Matches – {name}", ""]
    for i, m in enumerate(profile.last_matches[:5], start=1):
        lines.append(f"{i}) {m.map_name} • {m.mode}")
        lines.append(f"   {m.duration} • {m.when}")
        lines.append(f"   ID: #{m.match_id}")
        lines.append("")
    lines.append(f"🔗 Alle Matches: {profile_url(profile.game_id)}matches/")

    return "\n".join(lines)


def format_player_civs(name: str, profile: PlayerProfile) -> str:
    lines = [f"🧬 Civ-Überblick – {name}", ""]
    if profile.fav_civ_line:
        lines.append("Top-Civ:")
        lines.append(f"• {profile.fav_civ_line}")
    else:
        lines.append("Keine Civ-Infos aus dem About-Text gefunden.")

    if profile.about_text:
        lines.append("")
        lines.append("Auszug aus 'About':")
        snippet = profile.about_text
        if len(snippet) > 400:
            snippet = snippet[:400] + "..."
        lines.append(snippet)

    lines.append("")
    lines.append(f"🔗 Stats: {profile_url(profile.game_id)}stats/")
    return "\n".join(lines)


def format_group_stats(profiles: Dict[str, PlayerProfile]) -> str:
    lines = ["🤝 Gruppen-Stats", ""]
    for name in FRIENDS.keys():
        p = profiles.get(name)
        if not p:
            lines.append(f"{name}: keine Daten")
            continue
        l = p.ladder
        r1 = l.rating_1v1_rm if l.rating_1v1_rm is not None else "–"
        rt = l.rating_team_rm if l.rating_team_rm is not None else "–"
        tm = p.total_matches if p.total_matches is not None else "unbekannt"
        lines.append(f"{name}: 1v1 {r1} | Team {rt} | Spiele: {tm}")
    return "\n".join(lines)


# ===================== KEYBOARDS =====================

def start_keyboard() -> InlineKeyboardMarkup:
    buttons: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("👤 EDM7101", callback_data="player|EDM7101"),
            InlineKeyboardButton("👤 JustForFun", callback_data="player|JustForFun"),
        ],
        [
            InlineKeyboardButton("👤 rollthedice", callback_data="player|rollthedice"),
        ],
        [
            InlineKeyboardButton("🤝 Gruppen-Stats", callback_data="group|stats"),
            InlineKeyboardButton("📜 Gruppen-Matches", callback_data="group|matches"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def player_keyboard(name: str) -> InlineKeyboardMarkup:
    buttons: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("📡 Live (Profil)", url=profile_url(FRIENDS[name])),
            InlineKeyboardButton("📜 Matches", callback_data=f"player_matches|{name}"),
        ],
        [
            InlineKeyboardButton("🔥 Civs", callback_data=f"player_civs|{name}"),
        ],
        [
            InlineKeyboardButton("⬅️ Zurück", callback_data="back_start"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def group_stats_keyboard() -> InlineKeyboardMarkup:
    buttons: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("👤 EDM7101", callback_data="player|EDM7101"),
            InlineKeyboardButton("👤 JustForFun", callback_data="player|JustForFun"),
        ],
        [
            InlineKeyboardButton("👤 rollthedice", callback_data="player|rollthedice"),
        ],
        [
            InlineKeyboardButton("⬅️ Zurück", callback_data="back_start"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def group_matches_keyboard() -> InlineKeyboardMarkup:
    buttons: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("👤 EDM7101", callback_data="player_matches|EDM7101"),
        ],
        [
            InlineKeyboardButton("👤 JustForFun", callback_data="player_matches|JustForFun"),
        ],
        [
            InlineKeyboardButton("👤 rollthedice", callback_data="player_matches|rollthedice"),
        ],
        [
            InlineKeyboardButton("⬅️ Zurück", callback_data="back_start"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


# ===================== CACHING =====================

PROFILE_CACHE: Dict[str, PlayerProfile] = {}


def get_profile(name: str, offline: bool = False) -> PlayerProfile:
    cache_key = f"{name}-offline" if offline else name
    if cache_key in PROFILE_CACHE:
        return PROFILE_CACHE[cache_key]
    pid = FRIENDS[name]
    profile = scrape_player_offline(pid) if offline else scrape_player(pid)
    PROFILE_CACHE[cache_key] = profile
    return profile


def get_all_profiles(offline: bool = False) -> Dict[str, PlayerProfile]:
    res: Dict[str, PlayerProfile] = {}
    for name in FRIENDS.keys():
        try:
            res[name] = get_profile(name, offline=offline)
        except Exception as e:
            logger.error(f"Fehler beim Scrapen für {name}: {e}")
    return res


# ===================== COMMAND HANDLER =====================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Lade Daten von AoE2Insights...")
    loop = asyncio.get_running_loop()
    profiles = await loop.run_in_executor(None, get_all_profiles)

    text = format_group_overview(profiles)
    await update.message.reply_text(text, reply_markup=start_keyboard())


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    loop = asyncio.get_running_loop()
    profiles = await loop.run_in_executor(None, get_all_profiles)
    text = format_group_overview(profiles)
    await update.message.reply_text(text, reply_markup=start_keyboard())


# ===================== CALLBACK HANDLER =====================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    loop = asyncio.get_running_loop()

    async def load_profile(name: str) -> Optional[PlayerProfile]:
        try:
            return await loop.run_in_executor(None, get_profile, name)
        except Exception as exc:  # noqa: BLE001
            logger.error("Fehler beim Laden von %s: %s", name, exc)
            await query.edit_message_text(
                "⚠️ Konnte die Spielerdaten momentan nicht laden. Bitte später erneut versuchen.",
                reply_markup=start_keyboard(),
            )
            return None

    if data == "back_start":
        profiles = await loop.run_in_executor(None, get_all_profiles)
        text = format_group_overview(profiles)
        await query.edit_message_text(text, reply_markup=start_keyboard())
        return

    if data == "group|stats":
        profiles = await loop.run_in_executor(None, get_all_profiles)
        text = format_group_stats(profiles)
        await query.edit_message_text(text, reply_markup=group_stats_keyboard())
        return

    if data == "group|matches":
        text = (
            "📜 Gruppen-Matches\n\n"
            "Wähle einen Spieler, um seine letzten Matches zu sehen.\n"
            "Eine echte kombinierte Gruppen-History können wir später noch bauen."
        )
        await query.edit_message_text(text, reply_markup=group_matches_keyboard())
        return

    if data.startswith("player|"):
        _, name = data.split("|", 1)
        if name not in FRIENDS:
            await query.edit_message_text("Unbekannter Spieler.", reply_markup=start_keyboard())
            return
        profile = await load_profile(name)
        if profile is None:
            return
        text = format_player_main_card(name, profile)
        await query.edit_message_text(text, reply_markup=player_keyboard(name))
        return

    if data.startswith("player_matches|"):
        _, name = data.split("|", 1)
        if name not in FRIENDS:
            await query.edit_message_text("Unbekannter Spieler.", reply_markup=start_keyboard())
            return
        profile = await load_profile(name)
        if profile is None:
            return
        text = format_player_matches(name, profile)
        await query.edit_message_text(text, reply_markup=player_keyboard(name))
        return

    if data.startswith("player_civs|"):
        _, name = data.split("|", 1)
        if name not in FRIENDS:
            await query.edit_message_text("Unbekannter Spieler.", reply_markup=start_keyboard())
            return
        profile = await load_profile(name)
        if profile is None:
            return
        text = format_player_civs(name, profile)
        await query.edit_message_text(text, reply_markup=player_keyboard(name))
        return


# ===================== MAIN =====================

def main() -> None:
    parser = argparse.ArgumentParser(description="AoE2 Insights Telegram Bot")
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Scrape und drucke die Übersicht in die Konsole (ohne Telegram)",
    )
    parser.add_argument(
        "--player",
        choices=list(FRIENDS.keys()),
        help="Optional nur einen Spieler im CLI-Modus scrapen",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Nutze Offline-Demodaten statt Live-Abruf (hilfreich ohne Internet)",
    )
    args = parser.parse_args()

    if args.cli:
        try:
            if args.player:
                profile = get_profile(args.player, offline=args.offline)
                print(format_player_main_card(args.player, profile))
                print()
                print(format_player_matches(args.player, profile))
            else:
                profiles = get_all_profiles(offline=args.offline)
                print(format_group_overview(profiles))
        except Exception as exc:  # noqa: BLE001
            print(f"Fehler beim Abrufen der Daten: {exc}")
            raise SystemExit(1) from exc
        return

    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("BOT_TOKEN ist nicht gesetzt. Bitte als Environment Variable setzen.")

    app = ApplicationBuilder().token(bot_token).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CallbackQueryHandler(callback_handler))

    app.run_polling()


if __name__ == "__main__":
    main()
