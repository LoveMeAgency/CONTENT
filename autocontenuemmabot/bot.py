import asyncio
import logging
import os
import tempfile
import urllib.request
import ssl
import certifi
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any

from pyrogram import Client, filters, idle
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from pyrogram.errors import RPCError, ChatAdminRequired, BadRequest

import config

# ---------------- Logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ---------------- Folders ----------------
BASE_DIR = Path(__file__).resolve().parent
SESSION_DIR = BASE_DIR / "session"
SESSION_DIR.mkdir(parents=True, exist_ok=True)

# ---------------- Pyrogram Client ----------------
app_1 = Client(
    name=str(SESSION_DIR / "bot1"),
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN_1,
)

# ---------------- SQLite (uniquement pour planifier les suppressions) ----------------
DB_PATH = BASE_DIR / "autopost.sqlite3"

def db_init():
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS deletions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                delete_at INTEGER NOT NULL
            )
        """)
        con.commit()

def db_schedule_deletion(chat_id: int, message_id: int, delete_at_ts: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO deletions (chat_id, message_id, delete_at) VALUES (?, ?, ?)",
            (chat_id, message_id, delete_at_ts)
        )
        con.commit()

def db_fetch_due_deletions(now_ts: int, limit: int = 200) -> List[Tuple[int, int, int]]:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT id, chat_id, message_id FROM deletions WHERE delete_at <= ? ORDER BY id ASC LIMIT ?",
            (now_ts, limit)
        )
        return cur.fetchall()

def db_delete_deletion_row(row_id: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM deletions WHERE id=?", (row_id,))
        con.commit()

db_init()

# ---------------- Utils ----------------
_FR_WEEKDAYS = {
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3,
    "vendredi": 4, "samedi": 5, "dimanche": 6
}

def _kb(buttons: Optional[List[Tuple[str, str]]]) -> Optional[InlineKeyboardMarkup]:
    if not buttons:
        return None
    rows = [[InlineKeyboardButton(text=txt, url=url)] for (txt, url) in buttons]
    return InlineKeyboardMarkup(rows)

async def _download_if_url(maybe_url: Optional[str]) -> Optional[str]:
    if not maybe_url:
        return None
    s = str(maybe_url)
    if s.startswith(("http://", "https://")):
        try:
            req = urllib.request.Request(s, headers={"User-Agent": "Mozilla/5.0"})
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            with urllib.request.urlopen(req, timeout=120, context=ssl_ctx) as resp:
                suffix = Path(s).suffix or ""
                fd, temp_path = tempfile.mkstemp(prefix="ap_dl_", suffix=suffix)
                with os.fdopen(fd, "wb") as f:
                    f.write(resp.read())
            return temp_path
        except Exception as e:
            logger.warning(f"Téléchargement media KO {s}: {e}")
            return None
    return s

def _seconds_until_next_weekly(weekday_idx: int, hour: int, minute: int, tz_str: str) -> float:
    tz = ZoneInfo(tz_str)
    now = datetime.now(tz)
    days_ahead = (weekday_idx - now.weekday()) % 7
    target = (now + timedelta(days=days_ahead)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds()

def _resolve_schedule_tuple(schedule_var_name: str) -> Tuple[int, int, int]:
    """Lit POSTX_SCHEDULE dans config et renvoie (weekday_idx, hour, minute)."""
    if not hasattr(config, schedule_var_name):
        raise ValueError(f"Variable horaire manquante dans config.py: {schedule_var_name}")
    day_str, hhmm = getattr(config, schedule_var_name)
    day_idx = _FR_WEEKDAYS.get(day_str.strip().lower())
    if day_idx is None:
        raise ValueError(f"Jour invalide '{day_str}' pour {schedule_var_name}")
    hour, minute = map(int, hhmm.strip().split(":"))
    return day_idx, hour, minute

async def _resolve_chat_id(chat_ref: int | str) -> Optional[int]:
    """
    Accepte un int (-100...) ou un @username.
    Retourne l'ID numérique (-100...) ou None en cas d'échec.
    """
    try:
        if isinstance(chat_ref, str) and not chat_ref.lstrip("-").isdigit():
            chat = await app_1.get_chat(chat_ref)  # ex: "@mychannel"
            return chat.id
        return int(chat_ref)
    except Exception as e:
        logger.warning(f"[resolve] Impossible de résoudre {chat_ref}: {e}")
        return None

# ---------------- Messages (TOUT est ici) ----------------
MESSAGES: List[Dict[str, Any]] = [
    {
        "name": "post1",
        "schedule_var": "POST1_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Coucouuuu",
        "buttons": [],
    },
    {
        "name": "post2",
        "schedule_var": "POST2_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "J’ai grave envie que tu viennes t’occuper de moi 🥵",
        "buttons": [],
    },
    {
        "name": "post3",
        "schedule_var": "POST3_SCHEDULE",
        "type": "photo",
        "media": "http://my-privatelink.com/wp-content/uploads/2025/10/IMG_8447.jpg",
        "text": "Tu viens ?",
        "buttons": [(":🔞 REJOINDRE LE CANAL (1.14€)", "https://my-privatelink.com/emmagrc/")],
    },
    {
        "name": "post4",
        "schedule_var": "POST4_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Stpppp",
        "buttons": [],
    },
    {
        "name": "post5",
        "schedule_var": "POST5_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Je vais vous faire du contenu pour le canal privé 🫦 \n**Rejoins ici : https://my-privatelink.com/emmagrc**\n\n__(c'est le truc à 1.14€)__",
        "buttons": [(":🔞 REJOINDRE LE CANAL", "https://my-privatelink.com/emmagrc/")],
    },
    {
        "name": "post6",
        "schedule_var": "POST6_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "J’ai trop envie là 😋",
        "buttons": [],
    },
    {
        "name": "post7",
        "schedule_var": "POST7_SCHEDULE",
        "type": "photo",
        "media": "http://my-privatelink.com/wp-content/uploads/2025/10/IMG_8448.jpg",
        "text": "C’est mieux sans rien non ???",
        "buttons": [],
    },
    {
        "name": "post8",
        "schedule_var": "POST8_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Dépêche toi de venir\n👉 **[MON CANAL PRIVÉ](https://my-privatelink.com/emmagrc)**\n\n__(C’est à 1.14€)__",
        "buttons": [(":🔞 REJOINDRE LE CANAL", "https://my-privatelink.com/emmagrc/")],
    },
    {
        "name": "post9",
        "schedule_var": "POST9_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Heyyyy vous allez bien mes chéris 💕💕💕",
        "buttons": [],
    },
    {
        "name": "post10",
        "schedule_var": "POST10_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Je sors tout juste de la douche 🧼",
        "buttons": [],
    },
    {
        "name": "post11",
        "schedule_var": "POST11_SCHEDULE",
        "type": "video",
        "media": "http://my-privatelink.com/wp-content/uploads/2025/10/6515095458993217725.mp4",
        "text": "Je vous ai fait du contenu 😚",
        "buttons": [(":🔞 REJOINDRE LE CANAL", "https://my-privatelink.com/emmagrc/")],
    },
    {
        "name": "post12",
        "schedule_var": "POST12_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "C’est posté sur le canal privé 🍑\n\nPour accéder:\n\n1️⃣ Clique ici: https://my-privatelink.com/emmagrc\n\n 2️⃣ Inscris toi\n\n3️⃣ Prends l’essai à 1.14€\n\n4️⃣ t’aura accès à tout mon contenu et tu pourras te br*nler sur mon contenu 💦",
        "buttons": [],
    },
    {
        "name": "post13",
        "schedule_var": "POST13_SCHEDULE",
        "type": "photo",
        "media": "http://my-privatelink.com/wp-content/uploads/2025/10/IMG_8450.jpg",
        "text": "Je m’habille comme ça aujourd’hui ???",
        "buttons": [],
    },
    {
        "name": "post14",
        "schedule_var": "POST14_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Je rigole",
        "buttons": [],
    },
    {
        "name": "post15",
        "schedule_var": "POST15_SCHEDULE",
        "type": "video",
        "media": "http://my-privatelink.com/wp-content/uploads/2025/10/3921915719519255050.mp4",
        "text": "C’est mieux ça non ?? 👀",
        "buttons": [],
    },
    {
        "name": "post16",
        "schedule_var": "POST16_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Bon ce soir on va s’amuser sur le canal privé alors rejoins vite 👇\n\n**[MON CANAL PRIVÉ](https://my-privatelink.com/emmagrc)**",
        "buttons": [(":🔞 REJOINDRE LE CANAL", "https://my-privatelink.com/emmagrc/")],
    },
    {
        "name": "post17",
        "schedule_var": "POST17_SCHEDULE",
        "type": "photo",
        "media": "http://my-privatelink.com/wp-content/uploads/2025/10/IMG_8452.jpg",
        "text": "En live dans le jacuzzi sur le canal privé 🍒\n\n**Rejoins ici: https://my-privatelink.com/emmagrc**",
        "buttons": [(":🔞 REJOINDRE LE CANAL", "https://my-privatelink.com/emmagrc/")],
    },
    {
        "name": "post18",
        "schedule_var": "POST18_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Vous êtes déjà 110 en trains de me regarder me toucher dans un jacuzzi 🫶🏼🫶🏼",
        "buttons": [],
    },
    {
        "name": "post19",
        "schedule_var": "POST19_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Je reste encore 30 min en live donc dépêche toi 😋",
        "buttons": [],
    },
    {
        "name": "post20",
        "schedule_var": "POST20_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Le live est fini mais vous pouvez toujours accéder à la rediffusion sur l’espace privé 💖💖\n\nAccès à l’espace privé: **[MON CANAL PRIVÉ](https://my-privatelink.com/emmagrc)**",
        "buttons": [(":🔞 REJOINDRE LE CANAL", "https://my-privatelink.com/emmagrc/")],
    },
    {
        "name": "post21",
        "schedule_var": "POST21_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Coucou mes amours ça va ?? 🫦",
        "buttons": [],
    },
    {
        "name": "post22",
        "schedule_var": "POST22_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "J’ai une question",
        "buttons": [],
    },
    {
        "name": "post23",
        "schedule_var": "POST23_SCHEDULE",
        "type": "video",
        "media": "http://my-privatelink.com/wp-content/uploads/2025/10/5918970179071196584.mp4",
        "text": None,
        "buttons": [],
    },
    {
        "name": "post24",
        "schedule_var": "POST24_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Tu fais quoi si je suis comme ça devant toi 🍒",
        "buttons": [],
    },
    {
        "name": "post25",
        "schedule_var": "POST25_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Imagine que je suis en train de te br*nler en même temps 🍆",
        "buttons": [],
    },
    {
        "name": "post26",
        "schedule_var": "POST26_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Je veux que tu finisses sur moi stppp 🍼",
        "buttons": [],
    },
    {
        "name": "post27",
        "schedule_var": "POST27_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Je viens de vous tourner des vidéos venez ici:\n\n**[MON CANAL PRIVÉ 🫦](https://my-privatelink.com/emmagrc)**",
        "buttons": [],
    },
    {
        "name": "post28",
        "schedule_var": "POST28_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Vous allez adorer",
        "buttons": [],
    },
    {
        "name": "post29",
        "schedule_var": "POST29_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Mes bébés 😽",
        "buttons": [],
    },
    {
        "name": "post30",
        "schedule_var": "POST30_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Je suis en manque 💔😪",
        "buttons": [],
    },
    {
        "name": "post31",
        "schedule_var": "POST31_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Personne ne veut de moi…",
        "buttons": [],
    },
    {
        "name": "post32",
        "schedule_var": "POST32_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Je m’ennuie toute seule alors si tu veux venir me parler envoie moi un message ici:\n\n**[MON CANAL PRIVÉ 🫦](https://my-privatelink.com/emmagrc)**",
        "buttons": [(":🔞 REJOINDRE LE CANAL", "https://my-privatelink.com/emmagrc/")],
    },
    {
        "name": "post33",
        "schedule_var": "POST33_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "T’aura une petite surprise 💝",
        "buttons": [],
    },
    {
        "name": "post34",
        "schedule_var": "POST34_SCHEDULE",
        "type": "photo",
        "media": "http://my-privatelink.com/wp-content/uploads/2025/10/IMG_8259.jpg",
        "text": None,
        "buttons": [(":🔞 REJOINDRE LE CANAL", "https://my-privatelink.com/emmagrc/")],
    },
    {
        "name": "post35",
        "schedule_var": "POST35_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "VIIIIEEENNNNNSSSSSS 🍒",
        "buttons": [],
    },
    {
        "name": "post36",
        "schedule_var": "POST36_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "**Je fais un appelle privé avec les 2 prochaines personnes à m’envoyer un message sur mon canal privé 🔞**\n\n**Tu rejoins ici: https://my-privatelink.com/emmagrc**\n\nTu prends l’offre d’essai à 1.14€\n\nTu me dm\n\nEt on s’appelle 🤭🤭🫣",
        "buttons": [(":🔞 REJOINDRE LE CANAL", "https://my-privatelink.com/emmagrc/")],
    },
    {
        "name": "post37",
        "schedule_var": "POST37_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Je vais commencer un Live sur l’espace privé 😝",
        "buttons": [],
    },
    {
        "name": "post38",
        "schedule_var": "POST38_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Viens vite :\n\n**[MON CANAL PRIVÉ 🫦](https://my-privatelink.com/emmagrc)**",
        "buttons": [],
    },
    {
        "name": "post39",
        "schedule_var": "POST39_SCHEDULE",
        "type": "photo",
        "media": "http://my-privatelink.com/wp-content/uploads/2025/10/IMG_8455.jpg",
        "text": "Je suis en live 🥰",
        "buttons": [(":🔞 REJOINDRE LE CANAL", "https://my-privatelink.com/emmagrc/")],
    },
    {
        "name": "post40",
        "schedule_var": "POST40_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Je suis en train de me d*igter 🤟🏼💦",
        "buttons": [],
    },
    {
        "name": "post41",
        "schedule_var": "POST41_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Tout se passe sur mon canal privé:\n\nClique ici: https://my-privatelink.com/emmagrc\n\nInscris toi\n\nPrends l’offre à 1.14€\n\nRejoins mon live 🥰",
        "buttons": [(":🔞 REJOINDRE LE CANAL", "https://my-privatelink.com/emmagrc/")],
    },
    {
        "name": "post42",
        "schedule_var": "POST42_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Je vais bientôt jouir 💦",
        "buttons": [],
    },
    {
        "name": "post43",
        "schedule_var": "POST43_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Je vous avait dit que j’étais un peu fontaine 🤭",
        "buttons": [],
    },
    {
        "name": "post44",
        "schedule_var": "POST44_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "C’était trop bien le live hier 🫦",
        "buttons": [],
    },
    {
        "name": "post45",
        "schedule_var": "POST45_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Aujourd’hui je reste sage",
        "buttons": [],
    },
    {
        "name": "post46",
        "schedule_var": "POST46_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "C’est faux",
        "buttons": [],
    },
    {
        "name": "post47",
        "schedule_var": "POST47_SCHEDULE",
        "type": "photo",
        "media": "http://my-privatelink.com/wp-content/uploads/2025/10/IMG_8456.jpg",
        "text": None,
        "buttons": [],
    },
    {
        "name": "post48",
        "schedule_var": "POST48_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Je suis jamais sage 😇 😈",
        "buttons": [],
    },
    {
        "name": "post49",
        "schedule_var": "POST49_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Alors viens me punir 🫣",
        "buttons": [],
    },
    {
        "name": "post50",
        "schedule_var": "POST50_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Attendez je vais me changer pour vous faire du contenu",
        "buttons": [],
    },
    {
        "name": "post51",
        "schedule_var": "POST51_SCHEDULE",  # <-- corrigé (était POST2_SCHEDULE)
        "type": "photo",
        "media": "http://my-privatelink.com/wp-content/uploads/2025/10/IMG_8457.jpg",
        "text": "Comme ça t’aime bien ???",
        "buttons": [],
    },
    {
        "name": "post52",
        "schedule_var": "POST52_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Tous est ici 👇\n\n**[MON CANAL PRIVÉ 🫦](https://my-privatelink.com/emmagrc)**\n\n__Inscris toi et prends l’offre à 1.14€ pour accéder à plus de 400 contenus 🔞__",
        "buttons": [],
    },
    {
        "name": "post53",
        "schedule_var": "POST53_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Coucouuuuuuuu 🫶🏼",
        "buttons": [],
    },
    {
        "name": "post54",
        "schedule_var": "POST54_SCHEDULE",
        "type": "photo",
        "media": "http://my-privatelink.com/wp-content/uploads/2025/10/IMG_8458.jpg",
        "text": "Je viens de recevoir mon ensemble pour Noël 🫦",
        "buttons": [],
    },
    {
        "name": "post55",
        "schedule_var": "POST55_SCHEDULE",
        "type": "video",
        "media": "http://my-privatelink.com/wp-content/uploads/2025/10/6053736940265003916.mp4",
        "text": "T’aimes bien ?",
        "buttons": [],
    },
    {
        "name": "post56",
        "schedule_var": "POST56_SCHEDULE",
        "type": "text",
        "media": None,
        "text": "Oublie pas que tout mon contenu est ici petit coquin :\n\n**[MON CANAL PRIVÉ 🫦](https://my-privatelink.com/emmagrc)**",
        "buttons": [],
    },
]

# ---------------- Envoi d’un post vers 1 canal ----------------
async def _send_autopost_to_chat(chat_ref: int | str, post_cfg: Dict[str, Any]) -> Optional[int]:
    """
    Envoie un post vers chat_ref (int -100... ou @username).
    Résout d'abord l'ID numérique.
    """
    ptype = (post_cfg.get("type") or "text").lower()
    media = post_cfg.get("media")
    text = post_cfg.get("text") or ""
    buttons = post_cfg.get("buttons")
    markup = _kb(buttons)

    chat_id = await _resolve_chat_id(chat_ref)
    if chat_id is None:
        logger.warning(f"[autopost] Résolution chat KO pour {chat_ref}")
        return None

    temp_path = None
    try:
        media_path = None
        if ptype in ("photo", "video", "voice", "document"):
            media_path = await _download_if_url(media)
            if media_path and os.path.isabs(media_path):
                temp_path = media_path

        if ptype == "text":
            m = await app_1.send_message(chat_id, text or " ", reply_markup=markup)
        elif ptype == "photo":
            m = await app_1.send_photo(chat_id, photo=(media_path or media), caption=text or None, reply_markup=markup)
        elif ptype == "video":
            m = await app_1.send_video(chat_id, video=(media_path or media), caption=text or None, reply_markup=markup, supports_streaming=True)
        elif ptype == "voice":
            m = await app_1.send_voice(chat_id, voice=(media_path or media), caption=text or None, reply_markup=markup)
        elif ptype == "document":
            m = await app_1.send_document(chat_id, document=(media_path or media), caption=text or None, reply_markup=markup)
        else:
            m = await app_1.send_message(chat_id, text or " ", reply_markup=markup)

        return m.id
    except ChatAdminRequired:
        logger.warning(f"[autopost] Pas les droits dans {chat_id} (publier/supprimer).")
    except BadRequest as e:
        logger.warning(f"[autopost] BadRequest {chat_id}: {e}")
    except RPCError as e:
        logger.warning(f"[autopost] RPCError {chat_id}: {e}")
    except Exception as e:
        logger.warning(f"[autopost] Unexpected {chat_id}: {e}")
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
    return None

# ---------------- Workers ----------------
async def _autopost_worker(post_cfg: Dict[str, Any]):
    """Planifie et envoie ce post chaque semaine au jour/heure donnés, dans tous les CHANNEL_IDS."""
    tz = ZoneInfo(config.TIMEZONE)
    wd, h, m = _resolve_schedule_tuple(post_cfg["schedule_var"])

    while True:
        wait_s = _seconds_until_next_weekly(wd, h, m, config.TIMEZONE)
        logger.info(f"[autopost] {post_cfg['name']} prochain envoi dans {int(wait_s)}s ({post_cfg['schedule_var']}).")
        await asyncio.sleep(max(1, wait_s))

        if not getattr(config, "CHANNEL_IDS", None):
            logger.info(f"[autopost] Aucun CHANNEL_IDS dans config.py — envoi ignoré.")
        else:
            sent = 0
            for raw_ref in config.CHANNEL_IDS:  # int -100... ou "@username"
                mid = await _send_autopost_to_chat(raw_ref, post_cfg)
                if mid:
                    delete_at = int((datetime.now(tz) + timedelta(days=config.AUTO_DELETE_AFTER_DAYS)).timestamp())
                    # Résolution de l'ID définitif pour la DB
                    chat_id = await _resolve_chat_id(raw_ref)
                    if chat_id is not None:
                        db_schedule_deletion(chat_id, mid, delete_at)
                    sent += 1
                await asyncio.sleep(0.25)
            logger.info(f"[autopost] {post_cfg['name']} envoyé dans {sent} canal(aux).")

        # recalcul pour itération suivante
        wd, h, m = _resolve_schedule_tuple(post_cfg["schedule_var"])

async def _autodelete_worker():
    """Supprime périodiquement les messages arrivés à échéance (toutes les ~10 min)."""
    while True:
        now_ts = int(datetime.now().timestamp())
        rows = db_fetch_due_deletions(now_ts, limit=200)
        if rows:
            logger.info(f"[autodelete] À supprimer: {len(rows)} messages")
        for row_id, chat_id, message_id in rows:
            try:
                await app_1.delete_messages(chat_id, message_id)
            except Exception as e:
                logger.warning(f"[autodelete] {chat_id}:{message_id} -> {e}")
            finally:
                db_delete_deletion_row(row_id)
            await asyncio.sleep(0.2)
        await asyncio.sleep(600)

# ---------------- Commandes admin (test & debug) ----------------
@app_1.on_message(filters.command("force_post_index") & filters.user(config.ADMIN_ID))
async def force_post_index_handler(client: Client, message: Message):
    parts = message.text.strip().split()
    if len(parts) != 2:
        return await message.reply_text("Usage: /force_post_index <index 0-based>")
    try:
        idx = int(parts[1])
        post = MESSAGES[idx]
    except Exception:
        return await message.reply_text("Index invalide.")
    if not getattr(config, "CHANNEL_IDS", None):
        return await message.reply_text("Aucun CHANNEL_IDS dans config.py.")
    tz = ZoneInfo(config.TIMEZONE)
    sent = 0
    for raw_ref in config.CHANNEL_IDS:
        mid = await _send_autopost_to_chat(raw_ref, post)
        if mid:
            delete_at = int((datetime.now(tz) + timedelta(days=config.AUTO_DELETE_AFTER_DAYS)).timestamp())
            chat_id = await _resolve_chat_id(raw_ref)
            if chat_id is not None:
                db_schedule_deletion(chat_id, mid, delete_at)
            sent += 1
        await asyncio.sleep(0.25)
    await message.reply_text(f"OK: post {idx} envoyé dans {sent} canal(aux).")

@app_1.on_message(filters.command("start") & filters.user(config.ADMIN_ID))
async def start_handler(client: Client, message: Message):
    await message.reply_text("Bot OK. Utilise /force_post_index <i> pour tester un envoi.")

@app_1.on_message(filters.command("resolve") & filters.user(config.ADMIN_ID))
async def resolve_handler(client: Client, message: Message):
    # /resolve @username_ou_-100id
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) != 2:
        return await message.reply_text("Usage: /resolve <@username ou -100id>")
    raw = parts[1]
    try:
        chat = await app_1.get_chat(raw)
        await message.reply_text(f"OK ✅\nTitle: {chat.title}\nType: {chat.type}\nID: {chat.id}")
    except Exception as e:
        await message.reply_text(f"KO ❌: {e}")

# ---------------- Préflight (sanity check droits & accès) ----------------
async def _preflight_check():
    try:
        me = await app_1.get_me()
        for raw in getattr(config, "CHANNEL_IDS", []):
            try:
                chat = await app_1.get_chat(raw)
                # Tentative de lecture des privilèges si le bot est admin
                try:
                    member = await app_1.get_chat_member(chat.id, me.id)
                    can_post = getattr(getattr(member, "privileges", None), "can_post_messages", None)
                    can_delete = getattr(getattr(member, "privileges", None), "can_delete_messages", None)
                    logger.info(f"[preflight] {chat.title} ({chat.id}) -> can_post={can_post} can_delete={can_delete}")
                except Exception as e:
                    logger.warning(f"[preflight] Impossible de lire les droits sur {chat.id}: {e}")
            except Exception as e:
                logger.warning(f"[preflight] Accès impossible à {raw}: {e}")
    except Exception as e:
        logger.warning(f"[preflight] Erreur globale: {e}")

# ---------------- Main (Pyrogram v2) ----------------
async def main():
    await app_1.start()

    # Préflight immédiat
    await _preflight_check()

    # Lancer un worker par post
    for post_cfg in MESSAGES:
        asyncio.create_task(_autopost_worker(post_cfg))

    # Lancer le worker de suppression
    asyncio.create_task(_autodelete_worker())

    # Log de sanity check statique
    try:
        for p in MESSAGES:
            day, hm = getattr(config, p["schedule_var"])
            logger.info(f"[startup] {p['name']} -> {day} {hm}")
        logger.info(f"[startup] CHANNEL_IDS = {getattr(config, 'CHANNEL_IDS', [])}")
    except Exception:
        pass

    await idle()
    await app_1.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
