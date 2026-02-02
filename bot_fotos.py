# bot_fotos.py
# Requisitos:
#   pip install -U python-telegram-bot==21.6
#
# PowerShell:
#   cd "C:\Users\Diego_Siancas\Desktop\BOT TuFibra"
#   $env:BOT_TOKEN="TU_TOKEN"
#   $env:ROUTING_JSON='{"-100....":{"evidence":"-5....","summary":"-5...."}}'
#   python bot_fotos.py

import os
import json
import sqlite3
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot_fotos.sqlite3")
ROUTING_JSON = os.getenv("ROUTING_JSON", "").strip()

MAX_MEDIA_PER_STEP = 8
STEP_FIRST_MEDIA = 5
STEP_LAST_MEDIA = 15

TECHNICIANS = [
    "FLORO FERNANDEZ VASQUEZ",
    "ANTONY SALVADOR CORONADO",
    "DANIEL EDUARDO LUCENA PIÑANGO",
    "TELMER ROMUALDO RODRIGUEZ",
    "LUIS OMAR EPEQUIN ZAPATA",
    "CESAR ABRAHAM VASQUEZ MEZA",
]

SERVICE_TYPES = ["ALTA NUEVA", "POSTVENTA", "AVERIAS"]

# PASOS 5..15
STEP_MEDIA_DEFS = {
    5:  ("PASO 5 - FOTO DE FACHADA", "Envía foto de Fachada con placa de dirección y/o suministro eléctrico"),
    6:  ("PASO 6 - FOTO DE CTO", "Envía foto panorámica de la CTO o FAT rotulada"),
    7:  ("PASO 7 - POTENCIA EN CTO", "Envía la foto de la medida de potencia del puerto a utilizar"),
    8:  ("PASO 8 - PRECINTO ROTULADOR", "Envía la foto del cintillo rotulado identificando al cliente (DNI o CE y nro de puerto)"),
    9:  ("PASO 9 - FALSO TRAMO", "Envía foto del tramo de ingreso al domicilio"),
    10: ("PASO 10 - ANCLAJE", "Envía foto del punto de anclaje de la fibra drop en el domicilio"),
    11: ("PASO 11 - ROSETA + MEDICION POTENCIA", "Envía foto de la roseta abierta y medición de potencia"),
    12: ("PASO 12 - MAC ONT", "Envía foto de la MAC (Etiqueta) de la ONT y/o equipos usados"),
    13: ("PASO 13 - ONT", "Envía foto panorámica de la ONT operativa"),
    14: ("PASO 14 - TEST DE VELOCIDAD", "Envía foto del test de velocidad App Speedtest mostrar ID y fecha claramente"),
    15: ("PASO 15 - ACTA DE INSTALACION", "Envía foto del acta de instalación completa con la firma de cliente y datos llenos"),
}

# =========================
# Logging
# =========================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("tufibra_bot")


# =========================
# DB helpers
# =========================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _col_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == col for r in rows)


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cases (
                case_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,         -- OPEN / CLOSED / CANCELLED
                step_index INTEGER NOT NULL,  -- 0..14 (paso 1..15 mapeado)
                phase TEXT,
                pending_step_no INTEGER,      -- para autorización / control del paso actual (5..15)
                technician_name TEXT,
                service_type TEXT,
                abonado_code TEXT,
                location_lat REAL,
                location_lon REAL,
                location_at TEXT
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cases_open ON cases(chat_id, user_id, status);")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_config (
                chat_id INTEGER PRIMARY KEY,
                approval_required INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT
            );
            """
        )

        # Estados por paso / autorización (step_no positivo: evidencias; step_no negativo: autorizaciones)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS step_state (
                case_id INTEGER NOT NULL,
                step_no INTEGER NOT NULL,     -- +5..+15 evidencias; -5..-15 autorizaciones multimedia/texto
                attempt INTEGER NOT NULL DEFAULT 1,
                submitted INTEGER NOT NULL DEFAULT 0,
                approved INTEGER,             -- NULL=pending, 1=ok, 0=bad
                reviewed_by INTEGER,
                reviewed_at TEXT,
                created_at TEXT NOT NULL,
                reject_reason TEXT,
                reject_reason_by INTEGER,
                reject_reason_at TEXT,
                PRIMARY KEY(case_id, step_no, attempt),
                FOREIGN KEY(case_id) REFERENCES cases(case_id)
            );
            """
        )

        # Multimedia (evidencias y autorizaciones multimedia)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media (
                media_id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                step_no INTEGER NOT NULL,     -- +5..+15 evidencias; -5..-15 autorización multimedia
                attempt INTEGER NOT NULL,
                file_type TEXT NOT NULL,      -- photo|video
                file_id TEXT NOT NULL,
                file_unique_id TEXT,
                tg_message_id INTEGER NOT NULL,
                meta_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(case_id) REFERENCES cases(case_id)
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_media_case_step ON media(case_id, step_no, attempt);")

        # Autorización solo texto
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_text (
                auth_id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                step_no INTEGER NOT NULL,     -- +5..+15 (a qué paso corresponde)
                attempt INTEGER NOT NULL,
                text TEXT NOT NULL,
                tg_message_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(case_id) REFERENCES cases(case_id)
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_text_case_step ON auth_text(case_id, step_no, attempt);")

        # Pendientes de input para admins
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_inputs (
                pending_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,          -- AUTH_REJECT_REASON
                case_id INTEGER NOT NULL,
                step_no INTEGER NOT NULL,    -- paso positivo (5..15)
                attempt INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                reply_to_message_id INTEGER,
                tech_user_id INTEGER
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_inputs ON pending_inputs(chat_id, user_id, kind);")

        # Soft migrations (por si ya existía el sqlite)
        for col, ddl in [
            ("finished_at", "TEXT"),
            ("phase", "TEXT"),
            ("pending_step_no", "INTEGER"),
            ("technician_name", "TEXT"),
            ("service_type", "TEXT"),
            ("abonado_code", "TEXT"),
            ("location_lat", "REAL"),
            ("location_lon", "REAL"),
            ("location_at", "TEXT"),
        ]:
            if not _col_exists(conn, "cases", col):
                conn.execute(f"ALTER TABLE cases ADD COLUMN {col} {ddl};")

        # step_state migrations
        for col, ddl in [
            ("reject_reason", "TEXT"),
            ("reject_reason_by", "INTEGER"),
            ("reject_reason_at", "TEXT"),
        ]:
            if not _col_exists(conn, "step_state", col):
                conn.execute(f"ALTER TABLE step_state ADD COLUMN {col} {ddl};")

        # pending_inputs migrations
        for col, ddl in [
            ("reply_to_message_id", "INTEGER"),
            ("tech_user_id", "INTEGER"),
        ]:
            if not _col_exists(conn, "pending_inputs", col):
                conn.execute(f"ALTER TABLE pending_inputs ADD COLUMN {col} {ddl};")

        conn.commit()


def set_approval_required(chat_id: int, required: bool):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO chat_config(chat_id, approval_required, updated_at)
            VALUES(?,?,?)
            ON CONFLICT(chat_id) DO UPDATE
              SET approval_required=excluded.approval_required, updated_at=excluded.updated_at
            """,
            (chat_id, 1 if required else 0, now_utc()),
        )
        conn.commit()


def get_approval_required(chat_id: int) -> bool:
    with db() as conn:
        row = conn.execute("SELECT approval_required FROM chat_config WHERE chat_id=?", (chat_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT OR IGNORE INTO chat_config(chat_id, approval_required, updated_at) VALUES(?,?,?)",
                (chat_id, 1, now_utc()),
            )
            conn.commit()
            return True
        return bool(row["approval_required"])


def get_open_case(chat_id: int, user_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM cases WHERE chat_id=? AND user_id=? AND status='OPEN' ORDER BY case_id DESC LIMIT 1",
            (chat_id, user_id),
        ).fetchone()


def get_case(case_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()


def update_case(case_id: int, **fields):
    if not fields:
        return
    keys = list(fields.keys())
    vals = [fields[k] for k in keys]
    sets = ", ".join([f"{k}=?" for k in keys])
    with db() as conn:
        conn.execute(f"UPDATE cases SET {sets} WHERE case_id=?", (*vals, case_id))
        conn.commit()


def create_or_reset_case(chat_id: int, user_id: int, username: str) -> sqlite3.Row:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM cases WHERE chat_id=? AND user_id=? AND status='OPEN' ORDER BY case_id DESC LIMIT 1",
            (chat_id, user_id),
        ).fetchone()

        if row:
            conn.execute(
                """
                UPDATE cases
                SET created_at=?,
                    finished_at=NULL,
                    status='OPEN',
                    step_index=0,
                    phase='WAIT_TECHNICIAN',
                    pending_step_no=NULL,
                    technician_name=NULL,
                    service_type=NULL,
                    abonado_code=NULL,
                    location_lat=NULL,
                    location_lon=NULL,
                    location_at=NULL
                WHERE case_id=?
                """,
                (now_utc(), row["case_id"]),
            )
            conn.commit()
            return get_case(int(row["case_id"]))

        conn.execute(
            """
            INSERT INTO cases(chat_id, user_id, username, created_at, finished_at, status, step_index, phase, pending_step_no)
            VALUES(?,?,?,?,NULL,'OPEN',0,'WAIT_TECHNICIAN',NULL)
            """,
            (chat_id, user_id, username, now_utc()),
        )
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        return get_case(int(new_id))


# =========================
# Routing
# =========================
def get_route_for_chat(origin_chat_id: int) -> Dict[str, Optional[int]]:
    if not ROUTING_JSON:
        return {"evidence": None, "summary": None}
    try:
        mapping = json.loads(ROUTING_JSON)
        cfg = mapping.get(str(origin_chat_id)) or {}
        ev = cfg.get("evidence")
        sm = cfg.get("summary")
        return {
            "evidence": int(ev) if ev else None,
            "summary": int(sm) if sm else None,
        }
    except Exception as e:
        log.warning(f"ROUTING_JSON inválido: {e}")
        return {"evidence": None, "summary": None}


async def maybe_copy_to_group(
    context: ContextTypes.DEFAULT_TYPE,
    dest_chat_id: Optional[int],
    file_type: str,
    file_id: str,
    caption: str,
):
    if not dest_chat_id:
        return
    try:
        if file_type == "photo":
            await context.bot.send_photo(chat_id=dest_chat_id, photo=file_id, caption=caption[:1024])
        elif file_type == "video":
            await context.bot.send_video(chat_id=dest_chat_id, video=file_id, caption=caption[:1024])
    except Exception as e:
        log.warning(f"No pude copiar evidencia a destino {dest_chat_id}: {e}")


# =========================
# step_state helpers
# =========================
def _max_attempt(case_id: int, step_no: int) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT MAX(attempt) AS mx FROM step_state WHERE case_id=? AND step_no=?",
            (case_id, step_no),
        ).fetchone()
        mx = row["mx"] if row and row["mx"] is not None else 0
        return int(mx) if mx else 0


def ensure_step_state(case_id: int, step_no: int) -> sqlite3.Row:
    with db() as conn:
        row = conn.execute(
            """
            SELECT * FROM step_state
            WHERE case_id=? AND step_no=? AND submitted=0
            ORDER BY attempt DESC LIMIT 1
            """,
            (case_id, step_no),
        ).fetchone()
        if row:
            return row

        attempt = _max_attempt(case_id, step_no) + 1
        conn.execute(
            """
            INSERT INTO step_state(case_id, step_no, attempt, submitted, approved, reviewed_by, reviewed_at, created_at, reject_reason, reject_reason_by, reject_reason_at)
            VALUES(?,?,?,0,NULL,NULL,NULL,?,NULL,NULL,NULL)
            """,
            (case_id, step_no, attempt, now_utc()),
        )
        conn.commit()
        return conn.execute(
            "SELECT * FROM step_state WHERE case_id=? AND step_no=? AND attempt=?",
            (case_id, step_no, attempt),
        ).fetchone()


def media_count(case_id: int, step_no: int, attempt: int) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM media WHERE case_id=? AND step_no=? AND attempt=?",
            (case_id, step_no, attempt),
        ).fetchone()
        return int(row["c"]) if row else 0


def add_media(
    case_id: int,
    step_no: int,
    attempt: int,
    file_type: str,
    file_id: str,
    file_unique_id: Optional[str],
    tg_message_id: int,
    meta: Dict[str, Any],
):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO media(case_id, step_no, attempt, file_type, file_id, file_unique_id, tg_message_id, meta_json, created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                case_id,
                step_no,
                attempt,
                file_type,
                file_id,
                file_unique_id or "",
                tg_message_id,
                json.dumps(meta, ensure_ascii=False),
                now_utc(),
            ),
        )
        conn.commit()


def mark_submitted(case_id: int, step_no: int, attempt: int):
    with db() as conn:
        conn.execute(
            "UPDATE step_state SET submitted=1 WHERE case_id=? AND step_no=? AND attempt=?",
            (case_id, step_no, attempt),
        )
        conn.commit()


def set_review(case_id: int, step_no: int, attempt: int, approved: int, reviewer_id: int):
    with db() as conn:
        conn.execute(
            """
            UPDATE step_state
            SET approved=?, reviewed_by=?, reviewed_at=?
            WHERE case_id=? AND step_no=? AND attempt=?
            """,
            (approved, reviewer_id, now_utc(), case_id, step_no, attempt),
        )
        conn.commit()


def set_reject_reason(case_id: int, step_no: int, attempt: int, reason: str, reviewer_id: int):
    with db() as conn:
        conn.execute(
            """
            UPDATE step_state
            SET reject_reason=?, reject_reason_by=?, reject_reason_at=?
            WHERE case_id=? AND step_no=? AND attempt=?
            """,
            (reason, reviewer_id, now_utc(), case_id, step_no, attempt),
        )
        conn.commit()


def get_media_rows(case_id: int, step_no: int, attempt: int) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM media
            WHERE case_id=? AND step_no=? AND attempt=?
            ORDER BY media_id ASC
            """,
            (case_id, step_no, attempt),
        ).fetchall()


def delete_media_rows(case_id: int, step_no: int, attempt: int):
    with db() as conn:
        conn.execute(
            "DELETE FROM media WHERE case_id=? AND step_no=? AND attempt=?",
            (case_id, step_no, attempt),
        )
        conn.commit()


def save_auth_text(case_id: int, step_no: int, attempt: int, text: str, tg_message_id: int):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO auth_text(case_id, step_no, attempt, text, tg_message_id, created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (case_id, step_no, attempt, text, tg_message_id, now_utc()),
        )
        conn.commit()


def set_pending_input(
    chat_id: int,
    user_id: int,
    kind: str,
    case_id: int,
    step_no: int,
    attempt: int,
    reply_to_message_id: Optional[int] = None,
    tech_user_id: Optional[int] = None,
):
    with db() as conn:
        conn.execute("DELETE FROM pending_inputs WHERE chat_id=? AND user_id=? AND kind=?", (chat_id, user_id, kind))
        conn.execute(
            """
            INSERT INTO pending_inputs(chat_id, user_id, kind, case_id, step_no, attempt, created_at, reply_to_message_id, tech_user_id)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (chat_id, user_id, kind, case_id, step_no, attempt, now_utc(), reply_to_message_id, tech_user_id),
        )
        conn.commit()


def pop_pending_input(chat_id: int, user_id: int, kind: str) -> Optional[sqlite3.Row]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM pending_inputs WHERE chat_id=? AND user_id=? AND kind=? ORDER BY pending_id DESC LIMIT 1",
            (chat_id, user_id, kind),
        ).fetchone()
        if row:
            conn.execute("DELETE FROM pending_inputs WHERE pending_id=?", (row["pending_id"],))
            conn.commit()
        return row


# =========================
# Admin helper
# =========================
async def is_admin_of_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        return any(a.user and a.user.id == user_id for a in admins)
    except Exception:
        return False


# =========================
# Keyboards
# =========================
def kb_technicians() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(name, callback_data=f"TECH|{name}")] for name in TECHNICIANS]
    return InlineKeyboardMarkup(rows)


def kb_services() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(s, callback_data=f"SERV|{s}")] for s in SERVICE_TYPES]
    return InlineKeyboardMarkup(rows)


def kb_auth_ask(case_id: int, step_no: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("SI", callback_data=f"AUTH_ASK|{case_id}|{step_no}|YES"),
            InlineKeyboardButton("NO", callback_data=f"AUTH_ASK|{case_id}|{step_no}|NO"),
        ]]
    )


def kb_auth_mode(case_id: int, step_no: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Solo texto", callback_data=f"AUTH_MODE|{case_id}|{step_no}|TEXT"),
            InlineKeyboardButton("Multimedia", callback_data=f"AUTH_MODE|{case_id}|{step_no}|MEDIA"),
        ]]
    )


def kb_auth_media_controls(case_id: int, step_no: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("➕ CARGAR MAS", callback_data=f"AUTH_MORE|{case_id}|{step_no}"),
            InlineKeyboardButton("✅ EVIDENCIAS COMPLETAS", callback_data=f"AUTH_DONE|{case_id}|{step_no}"),
        ]]
    )


def kb_auth_review(case_id: int, step_no: int, attempt: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ AUTORIZADO", callback_data=f"AUT_OK|{case_id}|{step_no}|{attempt}"),
            InlineKeyboardButton("❌ RECHAZO", callback_data=f"AUT_BAD|{case_id}|{step_no}|{attempt}"),
        ]]
    )


def kb_media_controls(case_id: int, step_no: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("➕ CARGAR MAS", callback_data=f"MEDIA_MORE|{case_id}|{step_no}"),
            InlineKeyboardButton("✅ EVIDENCIAS COMPLETAS", callback_data=f"MEDIA_DONE|{case_id}|{step_no}"),
        ]]
    )


def kb_review_step(case_id: int, step_no: int, attempt: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ CONFORME", callback_data=f"REV_OK|{case_id}|{step_no}|{attempt}"),
            InlineKeyboardButton("❌ RECHAZO", callback_data=f"REV_BAD|{case_id}|{step_no}|{attempt}"),
        ]]
    )


# =========================
# Prompts
# =========================
def prompt_step3() -> str:
    return (
        "PASO 3 - INGRESA CÓDIGO DE ABONADO\n"
        "✅ Envía el código como texto (puede incluir letras, números o caracteres)."
    )


def prompt_step4() -> str:
    return (
        "PASO 4 - REPORTA TU UBICACIÓN\n"
        "📌 En grupos, Telegram no permite solicitar ubicación con botón.\n"
        "✅ Envía tu ubicación así:\n"
        "1) Pulsa el clip 📎\n"
        "2) Ubicación\n"
        "3) Enviar ubicación actual"
    )


def prompt_media_step(step_no: int) -> str:
    title, desc = STEP_MEDIA_DEFS.get(step_no, (f"PASO {step_no}", "Envía evidencias"))
    return (
        f"{title}\n"
        f"{desc}\n"
        f"📸🎥 Carga entre 1 a {MAX_MEDIA_PER_STEP} archivos (fotos o videos)."
    )


def prompt_auth_question(step_no: int) -> str:
    return (
        f"Antes de iniciar {STEP_MEDIA_DEFS.get(step_no, (f'PASO {step_no}',))[0]}:\n\n"
        "Quieres solicitar alguna autorizacion"
    )


async def ask_authorization(chat_id: int, context: ContextTypes.DEFAULT_TYPE, case_row: sqlite3.Row, step_no: int):
    update_case(int(case_row["case_id"]), phase="AUTH_ASK", pending_step_no=step_no)
    await context.bot.send_message(
        chat_id=chat_id,
        text=prompt_auth_question(step_no),
        reply_markup=kb_auth_ask(int(case_row["case_id"]), step_no),
    )


# =========================
# Commands
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    await context.bot.send_message(
        chat_id=msg.chat_id,
        text=(
            "Comandos:\n"
            "• /inicio  → iniciar caso\n"
            "• /estado  → ver estado\n"
            "• /cancelar → cancelar caso\n"
            "• /id → ver chat_id del grupo\n"
            "• /aprobacion on|off → activar/desactivar validaciones\n"
        ),
    )


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    title = msg.chat.title if msg.chat else "-"
    await context.bot.send_message(chat_id=msg.chat_id, text=f"Chat ID: {msg.chat_id}\nTitle: {title}")


async def inicio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None or msg.from_user is None:
        return

    chat_id = msg.chat_id
    user_id = msg.from_user.id
    username = msg.from_user.username or msg.from_user.full_name

    create_or_reset_case(chat_id, user_id, username)

    approval_required = get_approval_required(chat_id)
    extra = "✅ Aprobación: ON (requiere admin)" if approval_required else "⚠️ Aprobación: OFF (modo libre)"

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ Caso iniciado.\n{extra}\n\nPASO 1 - NOMBRE DEL TECNICO",
        reply_markup=kb_technicians(),
    )


async def cancelar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None or msg.from_user is None:
        return

    case_row = get_open_case(msg.chat_id, msg.from_user.id)
    if not case_row:
        await context.bot.send_message(chat_id=msg.chat_id, text="No tienes un caso abierto.")
        return

    update_case(int(case_row["case_id"]), status="CANCELLED", phase="CANCELLED", finished_at=now_utc())
    await context.bot.send_message(chat_id=msg.chat_id, text="🧾 Caso cancelado. Puedes iniciar otro con /inicio.")


async def estado_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None or msg.from_user is None:
        return

    case_row = get_open_case(msg.chat_id, msg.from_user.id)
    if not case_row:
        await context.bot.send_message(chat_id=msg.chat_id, text="No tienes un caso abierto. Usa /inicio.")
        return

    approval_required = get_approval_required(msg.chat_id)
    approval_txt = "ON ✅" if approval_required else "OFF ⚠️"

    await context.bot.send_message(
        chat_id=msg.chat_id,
        text=(
            f"📌 Caso abierto\n"
            f"• Aprobación: {approval_txt}\n"
            f"• step_index: {int(case_row['step_index'])}\n"
            f"• phase: {case_row['phase']}\n"
            f"• pending_step_no: {case_row['pending_step_no']}\n"
            f"• Técnico: {case_row['technician_name'] or '(pendiente)'}\n"
            f"• Servicio: {case_row['service_type'] or '(pendiente)'}\n"
            f"• Abonado: {case_row['abonado_code'] or '(pendiente)'}\n"
        ),
    )


async def aprobacion_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None or msg.from_user is None:
        return

    args = context.args or []
    if not args:
        state = "ON ✅" if get_approval_required(msg.chat_id) else "OFF ⚠️"
        await context.bot.send_message(chat_id=msg.chat_id, text=f"Estado de aprobación: {state}")
        return

    val = args[0].strip().lower()
    if val in ("on", "1", "true", "si", "sí", "activar"):
        set_approval_required(msg.chat_id, True)
        await context.bot.send_message(chat_id=msg.chat_id, text="✅ Aprobación ENCENDIDA.")
    elif val in ("off", "0", "false", "no", "desactivar"):
        set_approval_required(msg.chat_id, False)
        await context.bot.send_message(chat_id=msg.chat_id, text="⚠️ Aprobación APAGADA.")
    else:
        await context.bot.send_message(chat_id=msg.chat_id, text="Uso: /aprobacion on  o  /aprobacion off")


# =========================
# Callbacks
# =========================
async def on_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q is None or q.message is None or q.from_user is None:
        return

    chat_id = q.message.chat_id
    user_id = q.from_user.id
    data = (q.data or "").strip()

    log.info(f"CALLBACK data={data} chat_id={chat_id} user_id={user_id}")

    # ========= PASO 1 =========
    if data.startswith("TECH|"):
        case_row = get_open_case(chat_id, user_id)
        if not case_row:
            await q.answer("No tienes un caso abierto. Usa /inicio.", show_alert=True)
            return
        if int(case_row["step_index"]) != 0:
            await q.answer("Este paso ya fue atendido.", show_alert=False)
            return

        name = data.split("|", 1)[1]
        update_case(int(case_row["case_id"]), technician_name=name, step_index=1, phase="WAIT_SERVICE")
        await q.answer("✅ Técnico registrado")
        await context.bot.send_message(chat_id=chat_id, text="PASO 2 - TIPO DE SERVICIO", reply_markup=kb_services())
        return

    # ========= PASO 2 =========
    if data.startswith("SERV|"):
        case_row = get_open_case(chat_id, user_id)
        if not case_row:
            await q.answer("No tienes un caso abierto. Usa /inicio.", show_alert=True)
            return
        if int(case_row["step_index"]) != 1:
            await q.answer("Este paso ya fue atendido.", show_alert=False)
            return

        service = data.split("|", 1)[1]
        update_case(int(case_row["case_id"]), service_type=service, step_index=2, phase="WAIT_ABONADO")
        await q.answer("✅ Servicio registrado")
        await context.bot.send_message(chat_id=chat_id, text=prompt_step3())
        return

    # ========= AUTORIZACIÓN: SI/NO =========
    if data.startswith("AUTH_ASK|"):
        try:
            _, case_id_s, step_no_s, yn = data.split("|", 3)
            case_id = int(case_id_s)
            step_no = int(step_no_s)
        except Exception:
            await q.answer("Callback inválido", show_alert=True)
            return

        case_row = get_case(case_id)
        if not case_row or case_row["status"] != "OPEN":
            await q.answer("Caso no válido o cerrado.", show_alert=True)
            return
        if int(case_row["user_id"]) != user_id:
            await q.answer("Solo el técnico del caso puede responder esta pregunta.", show_alert=True)
            return

        if yn == "NO":
            update_case(case_id, phase="STEP_MEDIA", pending_step_no=step_no)
            await q.answer("Continuando…")
            await context.bot.send_message(chat_id=chat_id, text=prompt_media_step(step_no))
            return

        update_case(case_id, phase="AUTH_MODE", pending_step_no=step_no)
        await q.answer("Elige tipo…")
        await context.bot.send_message(chat_id=chat_id, text="Autorización: elige el tipo", reply_markup=kb_auth_mode(case_id, step_no))
        return

    # ========= AUTORIZACIÓN: modo =========
    if data.startswith("AUTH_MODE|"):
        try:
            _, case_id_s, step_no_s, mode = data.split("|", 3)
            case_id = int(case_id_s)
            step_no = int(step_no_s)
        except Exception:
            await q.answer("Callback inválido", show_alert=True)
            return

        case_row = get_case(case_id)
        if not case_row or case_row["status"] != "OPEN":
            await q.answer("Caso no válido o cerrado.", show_alert=True)
            return
        if int(case_row["user_id"]) != user_id:
            await q.answer("Solo el técnico del caso puede elegir.", show_alert=True)
            return

        if mode == "TEXT":
            update_case(case_id, phase="AUTH_TEXT_WAIT", pending_step_no=step_no)
            await q.answer("Envía el texto…")
            await context.bot.send_message(chat_id=chat_id, text="Envía el texto de la autorización (Telegram limita el tamaño del mensaje).")
            return

        if mode == "MEDIA":
            update_case(case_id, phase="AUTH_MEDIA", pending_step_no=step_no)
            await q.answer("Carga evidencias…")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Autorización multimedia para {STEP_MEDIA_DEFS.get(step_no, (f'PASO {step_no}',))[0]}\n"
                     f"📸🎥 Carga entre 1 a {MAX_MEDIA_PER_STEP} archivos (fotos o videos).",
            )
            return

        await q.answer("Modo inválido", show_alert=True)
        return

    # ========= AUTORIZACIÓN multimedia: botones =========
    if data.startswith("AUTH_MORE|"):
        await q.answer("Puedes seguir cargando.", show_alert=False)
        return

    if data.startswith("AUTH_DONE|"):
        try:
            _, case_id_s, step_no_s = data.split("|", 2)
            case_id = int(case_id_s)
            step_no = int(step_no_s)
        except Exception:
            await q.answer("Callback inválido", show_alert=True)
            return

        case_row = get_case(case_id)
        if not case_row or case_row["status"] != "OPEN":
            await q.answer("Caso no válido o cerrado.", show_alert=True)
            return
        if int(case_row["user_id"]) != user_id:
            await q.answer("Solo el técnico del caso puede marcar evidencias completas.", show_alert=True)
            return

        auth_step_no = -step_no
        st = ensure_step_state(case_id, auth_step_no)
        attempt = int(st["attempt"])

        if int(st["submitted"]) == 1:
            await q.answer("Esta autorización ya fue enviada a revisión.", show_alert=True)
            return

        count = media_count(case_id, auth_step_no, attempt)
        if count <= 0:
            await q.answer("Aún no hay archivos cargados.", show_alert=True)
            return

        mark_submitted(case_id, auth_step_no, attempt)
        await q.answer("📨 Enviado a revisión")

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔐 **Revisión de AUTORIZACIÓN (multimedia)**\n"
                f"Para: {STEP_MEDIA_DEFS.get(step_no, (f'PASO {step_no}',))[0]}\n"
                f"Técnico: {case_row['technician_name'] or '-'}\n"
                f"Servicio: {case_row['service_type'] or '-'}\n"
                f"Abonado: {case_row['abonado_code'] or '-'}\n"
                f"Archivos: {count}\n\n"
                "Admins: validar con ✅/❌"
            ),
            parse_mode="Markdown",
            reply_markup=kb_auth_review(case_id, step_no, attempt),
        )
        return

    # ========= AUTORIZACIÓN: revisión admin =========
    if data.startswith("AUT_OK|") or data.startswith("AUT_BAD|"):
        try:
            action, case_id_s, step_no_s, attempt_s = data.split("|", 3)
            case_id = int(case_id_s)
            step_no = int(step_no_s)      # positivo (paso destino)
            attempt = int(attempt_s)
        except Exception:
            await q.answer("Callback inválido", show_alert=True)
            return

        # Solo admins
        if not await is_admin_of_chat(context, chat_id, user_id):
            await q.answer("Solo Administradores del grupo pueden validar", show_alert=True)
            return

        case_row = get_case(case_id)
        if not case_row or case_row["status"] != "OPEN":
            await q.answer("Caso no válido o cerrado.", show_alert=True)
            return

        auth_step_no = -step_no

        # Evitar doble revisión
        with db() as conn:
            row = conn.execute(
                "SELECT approved FROM step_state WHERE case_id=? AND step_no=? AND attempt=?",
                (case_id, auth_step_no, attempt),
            ).fetchone()
        if not row:
            await q.answer("No encontré la autorización para revisar.", show_alert=True)
            return
        if row["approved"] is not None:
            await q.answer("Esta autorización ya fue revisada.", show_alert=True)
            return

        if action == "AUT_OK":
            set_review(case_id, auth_step_no, attempt, approved=1, reviewer_id=user_id)
            await q.answer("✅ Autorizado")
            await q.edit_message_text("✅ Autorizado. Continuando al paso…")

            update_case(case_id, phase="STEP_MEDIA", pending_step_no=step_no)
            await context.bot.send_message(chat_id=chat_id, text=prompt_media_step(step_no))
            return

        # AUT_BAD -> pedir motivo al admin (y luego aplicar rechazo)
        await q.answer("Escribe el motivo del rechazo.", show_alert=False)

        set_pending_input(
            chat_id=chat_id,
            user_id=user_id,
            kind="AUTH_REJECT_REASON",
            case_id=case_id,
            step_no=step_no,
            attempt=attempt,
            reply_to_message_id=q.message.message_id,   # reply al mensaje de revisión
            tech_user_id=int(case_row["user_id"]),      # mencionar al técnico
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "❌ Rechazo de autorización registrado.\n"
                "✍️ Admin: por favor escribe el *motivo del rechazo* en un solo mensaje.\n\n"
                f"Paso: {STEP_MEDIA_DEFS.get(step_no, (f'PASO {step_no}',))[0]}\n"
                f"Técnico: {case_row['technician_name'] or '-'}"
            ),
            parse_mode="Markdown",
        )
        return

    # ========= EVIDENCIAS: botones =========
    if data.startswith("MEDIA_MORE|"):
        await q.answer("Puedes seguir cargando evidencias.", show_alert=False)
        return

    if data.startswith("MEDIA_DONE|"):
        try:
            _, case_id_s, step_no_s = data.split("|", 2)
            case_id = int(case_id_s)
            step_no = int(step_no_s)
        except Exception:
            await q.answer("Callback inválido", show_alert=True)
            return

        case_row = get_case(case_id)
        if not case_row or case_row["status"] != "OPEN":
            await q.answer("Caso no válido o cerrado.", show_alert=True)
            return

        if int(case_row["user_id"]) != user_id:
            await q.answer("Solo el técnico del caso puede marcar evidencias completas.", show_alert=True)
            return

        st = ensure_step_state(case_id, step_no)
        attempt = int(st["attempt"])

        if int(st["submitted"]) == 1:
            await q.answer("Este paso ya fue enviado a revisión.", show_alert=True)
            return

        count = media_count(case_id, step_no, attempt)
        if count <= 0:
            await q.answer("Aún no hay evidencias cargadas.", show_alert=True)
            return

        mark_submitted(case_id, step_no, attempt)
        await q.answer("📨 Enviado a revisión")

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔎 **Revisión requerida ({STEP_MEDIA_DEFS.get(step_no, (f'PASO {step_no}',))[0]})**\n"
                f"Técnico: {case_row['technician_name'] or '-'}\n"
                f"Servicio: {case_row['service_type'] or '-'}\n"
                f"Abonado: {case_row['abonado_code'] or '-'}\n"
                f"Evidencias: {count}\n\n"
                f"Admins: validar con ✅/❌"
            ),
            parse_mode="Markdown",
            reply_markup=kb_review_step(case_id, step_no, attempt),
        )
        return

    # ========= EVIDENCIAS: revisión admin =========
    if data.startswith("REV_OK|") or data.startswith("REV_BAD|"):
        try:
            action, case_id_s, step_no_s, attempt_s = data.split("|", 3)
            case_id = int(case_id_s)
            step_no = int(step_no_s)
            attempt = int(attempt_s)
        except Exception:
            await q.answer("Callback inválido", show_alert=True)
            return

        if not await is_admin_of_chat(context, chat_id, user_id):
            await q.answer("Solo Administradores del grupo pueden validar", show_alert=True)
            return

        case_row = get_case(case_id)
        if not case_row or case_row["status"] != "OPEN":
            await q.answer("Caso no válido o cerrado.", show_alert=True)
            return

        # Evitar doble revisión
        with db() as conn:
            row = conn.execute(
                "SELECT approved FROM step_state WHERE case_id=? AND step_no=? AND attempt=?",
                (case_id, step_no, attempt),
            ).fetchone()
        if not row:
            await q.answer("No encontré el paso para revisar.", show_alert=True)
            return
        if row["approved"] is not None:
            await q.answer("Este paso ya fue revisado.", show_alert=True)
            return

        if action == "REV_OK":
            set_review(case_id, step_no, attempt, approved=1, reviewer_id=user_id)
            await q.answer("✅ Conforme")

            if step_no >= STEP_LAST_MEDIA:
                finished_at = now_utc()
                update_case(case_id, status="CLOSED", phase="CLOSED", finished_at=finished_at, pending_step_no=None)
                await q.edit_message_text("✅ Conforme. 🧾 Caso COMPLETADO y cerrado.")

                route = get_route_for_chat(int(case_row["chat_id"]))
                dest_summary = route.get("summary")
                if dest_summary:
                    created_at = case_row["created_at"] or "-"
                    await context.bot.send_message(
                        chat_id=dest_summary,
                        text=(
                            "🧾 **RESUMEN DE CASO (CERRADO)**\n"
                            f"Fecha: {created_at[:10]}\n"
                            f"Hora de Inicio: {created_at[11:19] if len(created_at) >= 19 else created_at}\n"
                            f"Hora de Final: {finished_at[11:19] if len(finished_at) >= 19 else finished_at}\n"
                            f"Técnico: {case_row['technician_name'] or '-'}\n"
                            f"Tipo servicio: {case_row['service_type'] or '-'}\n"
                            f"Código abonado: {case_row['abonado_code'] or '-'}\n"
                            f"Grupo origen: {case_row['chat_id']}\n"
                        ),
                        parse_mode="Markdown",
                    )
                return

            await q.edit_message_text("✅ Conforme. Continuando…")

            next_step_no = step_no + 1
            case_row2 = get_case(case_id)
            await ask_authorization(chat_id, context, case_row2, next_step_no)
            return

        # RECHAZO evidencia
        set_review(case_id, step_no, attempt, approved=0, reviewer_id=user_id)
        media_rows = get_media_rows(case_id, step_no, attempt)

        deleted_any = False
        for m in media_rows:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=int(m["tg_message_id"]))
                deleted_any = True
            except Exception:
                pass

        delete_media_rows(case_id, step_no, attempt)

        await q.edit_message_text(
            "❌ Paso rechazado. "
            + ("(Se borraron fotos/videos del grupo) " if deleted_any else "(No pude borrar: revisa permisos del bot) ")
            + "Técnico debe reenviar este paso."
        )

        update_case(case_id, phase="STEP_MEDIA", pending_step_no=step_no)
        await context.bot.send_message(chat_id=chat_id, text=prompt_media_step(step_no))
        return

    await q.answer("Acción no válida.", show_alert=True)


# =========================
# Text handler (PASO 3 + AUTH_TEXT + motivo rechazo)
# =========================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg: Message = update.effective_message
    if msg is None or msg.from_user is None:
        return

    # 1) Si el admin tiene pendiente el motivo de rechazo
    pending = pop_pending_input(msg.chat_id, msg.from_user.id, "AUTH_REJECT_REASON")
    if pending:
        reason = (msg.text or "").strip()
        if not reason:
            await context.bot.send_message(chat_id=msg.chat_id, text="⚠️ Envía un texto válido como motivo.")
            # Reinsertar pendiente
            set_pending_input(
                chat_id=msg.chat_id,
                user_id=msg.from_user.id,
                kind="AUTH_REJECT_REASON",
                case_id=int(pending["case_id"]),
                step_no=int(pending["step_no"]),
                attempt=int(pending["attempt"]),
                reply_to_message_id=int(pending["reply_to_message_id"]) if pending["reply_to_message_id"] is not None else None,
                tech_user_id=int(pending["tech_user_id"]) if pending["tech_user_id"] is not None else None,
            )
            return

        case_id = int(pending["case_id"])
        step_no = int(pending["step_no"])      # paso positivo 5..15
        attempt = int(pending["attempt"])
        auth_step_no = -step_no

        case_db = get_case(case_id)
        if not case_db or case_db["status"] != "OPEN":
            await context.bot.send_message(chat_id=msg.chat_id, text="⚠️ Caso no válido o ya cerrado.")
            return

        # Marcar rechazo + guardar motivo
        set_review(case_id, auth_step_no, attempt, approved=0, reviewer_id=msg.from_user.id)
        set_reject_reason(case_id, auth_step_no, attempt, reason, msg.from_user.id)

        deleted_any = False

        # Borrar multimedia de autorización (si existe)
        media_rows = get_media_rows(case_id, auth_step_no, attempt)
        for m in media_rows:
            try:
                await context.bot.delete_message(chat_id=msg.chat_id, message_id=int(m["tg_message_id"]))
                deleted_any = True
            except Exception:
                pass
        if media_rows:
            delete_media_rows(case_id, auth_step_no, attempt)

        # Borrar texto de autorización (si existiera)
        with db() as conn:
            trow = conn.execute(
                "SELECT tg_message_id FROM auth_text WHERE case_id=? AND step_no=? AND attempt=? ORDER BY auth_id DESC LIMIT 1",
                (case_id, step_no, attempt),
            ).fetchone()
        if trow:
            try:
                await context.bot.delete_message(chat_id=msg.chat_id, message_id=int(trow["tg_message_id"]))
                deleted_any = True
            except Exception:
                pass

        tech_id = int(pending["tech_user_id"]) if pending["tech_user_id"] is not None else None
        mention = f'<a href="tg://user?id={tech_id}">Técnico</a>' if tech_id else "Técnico"
        reply_to = int(pending["reply_to_message_id"]) if pending["reply_to_message_id"] is not None else None

        await context.bot.send_message(
            chat_id=msg.chat_id,
            text=(
                f"❌ Autorización rechazada ({mention}).\n"
                + ("(Se borraron evidencias del grupo)\n" if deleted_any else "(No pude borrar: revisa permisos del bot)\n")
                + f"📝 Motivo: {reason}\n\n"
                "El técnico debe solicitar nuevamente autorización o continuar sin ella."
            ),
            parse_mode="HTML",
            reply_to_message_id=reply_to if reply_to else None,
        )

        # Volver a preguntar autorización para el mismo paso
        update_case(case_id, phase="AUTH_ASK", pending_step_no=step_no)
        await context.bot.send_message(
            chat_id=msg.chat_id,
            text=prompt_auth_question(step_no),
            reply_markup=kb_auth_ask(case_id, step_no),
        )
        return

    # 2) Flujo normal: identificar caso del técnico
    case_row = get_open_case(msg.chat_id, msg.from_user.id)
    if not case_row:
        return

    # AUTORIZACIÓN solo texto
    if (case_row["phase"] or "") == "AUTH_TEXT_WAIT":
        step_no = int(case_row["pending_step_no"] or 0)
        if step_no < STEP_FIRST_MEDIA or step_no > STEP_LAST_MEDIA:
            return

        text = (msg.text or "").strip()
        if not text:
            await context.bot.send_message(chat_id=msg.chat_id, text="⚠️ Envía el texto de autorización.")
            return

        case_id = int(case_row["case_id"])
        auth_step_no = -step_no
        st = ensure_step_state(case_id, auth_step_no)
        attempt = int(st["attempt"])

        # Guardar texto
        save_auth_text(case_id, step_no, attempt, text, msg.message_id)

        # Marcar submitted y pedir revisión admins
        mark_submitted(case_id, auth_step_no, attempt)
        update_case(case_id, phase="AUTH_REVIEW", pending_step_no=step_no)

        await context.bot.send_message(
            chat_id=msg.chat_id,
            text=(
                f"🔐 **Revisión de AUTORIZACIÓN (solo texto)**\n"
                f"Para: {STEP_MEDIA_DEFS.get(step_no, (f'PASO {step_no}',))[0]}\n"
                f"Técnico: {case_row['technician_name'] or '-'}\n"
                f"Servicio: {case_row['service_type'] or '-'}\n"
                f"Abonado: {case_row['abonado_code'] or '-'}\n\n"
                f"Texto:\n{text}"
            ),
            parse_mode="Markdown",
            reply_markup=kb_auth_review(case_id, step_no, attempt),
        )
        return

    # PASO 3 normal
    if int(case_row["step_index"]) != 2:
        return

    text = (msg.text or "").strip()
    if not text:
        await context.bot.send_message(chat_id=msg.chat_id, text="⚠️ Envía el código de abonado como texto.")
        return

    update_case(int(case_row["case_id"]), abonado_code=text, step_index=3, phase="WAIT_LOCATION")
    await context.bot.send_message(chat_id=msg.chat_id, text=f"✅ Código de abonado registrado: {text}\n\n{prompt_step4()}")


# =========================
# PASO 4: Ubicación
# =========================
async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg: Message = update.effective_message
    if msg is None or msg.from_user is None:
        return

    case_row = get_open_case(msg.chat_id, msg.from_user.id)
    if not case_row:
        return

    if int(case_row["step_index"]) != 3:
        return

    if not msg.location:
        await context.bot.send_message(chat_id=msg.chat_id, text="⚠️ Envía tu ubicación usando 📎 → Ubicación → ubicación actual.")
        return

    update_case(
        int(case_row["case_id"]),
        location_lat=msg.location.latitude,
        location_lon=msg.location.longitude,
        location_at=now_utc(),
        step_index=4,
        phase="AUTH_ASK",
        pending_step_no=5,
    )

    case_row2 = get_case(int(case_row["case_id"]))
    await ask_authorization(msg.chat_id, context, case_row2, 5)


# =========================
# PASO 5..15 evidencias + autorización multimedia
# =========================
async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg: Message = update.effective_message
    if msg is None or msg.from_user is None:
        return

    case_row = get_open_case(msg.chat_id, msg.from_user.id)
    if not case_row:
        return

    case_id = int(case_row["case_id"])
    pending_step_no = int(case_row["pending_step_no"] or 0)

    phase = (case_row["phase"] or "")
    if phase not in ("AUTH_MEDIA", "STEP_MEDIA"):
        return

    if pending_step_no < STEP_FIRST_MEDIA or pending_step_no > STEP_LAST_MEDIA:
        return

    if phase == "AUTH_MEDIA":
        step_no_to_store = -pending_step_no
        controls_kb = kb_auth_media_controls(case_id, pending_step_no)
        label = "AUTORIZACIÓN"
    else:
        step_no_to_store = pending_step_no
        controls_kb = kb_media_controls(case_id, pending_step_no)
        label = "EVIDENCIA"

    st = ensure_step_state(case_id, step_no_to_store)
    attempt = int(st["attempt"])

    if int(st["submitted"]) == 1:
        await context.bot.send_message(chat_id=msg.chat_id, text="⏳ Ya está en revisión. Espera validación del administrador.")
        return

    current = media_count(case_id, step_no_to_store, attempt)
    if current >= MAX_MEDIA_PER_STEP:
        await context.bot.send_message(chat_id=msg.chat_id, text=f"⚠️ Ya llegaste al máximo de {MAX_MEDIA_PER_STEP}. Presiona ✅ EVIDENCIAS COMPLETAS.")
        await context.bot.send_message(chat_id=msg.chat_id, text="Controles:", reply_markup=controls_kb)
        return

    file_type = None
    file_id = None
    file_unique_id = None

    if msg.photo:
        ph = msg.photo[-1]
        file_type = "photo"
        file_id = ph.file_id
        file_unique_id = ph.file_unique_id
    elif msg.video:
        vd = msg.video
        file_type = "video"
        file_id = vd.file_id
        file_unique_id = vd.file_unique_id
    else:
        return

    meta = {
        "from_user_id": msg.from_user.id,
        "from_username": msg.from_user.username,
        "from_name": msg.from_user.full_name,
        "date": msg.date.isoformat() if msg.date else None,
        "caption": msg.caption,
        "phase": phase,
        "step_pending": pending_step_no,
    }

    add_media(
        case_id=case_id,
        step_no=step_no_to_store,
        attempt=attempt,
        file_type=file_type,
        file_id=file_id,
        file_unique_id=file_unique_id,
        tg_message_id=msg.message_id,
        meta=meta,
    )

    route = get_route_for_chat(msg.chat_id)
    caption = (
        f"📌 {label} ({STEP_MEDIA_DEFS.get(pending_step_no, (f'PASO {pending_step_no}',))[0]})\n"
        f"Técnico: {case_row['technician_name'] or '-'}\n"
        f"Servicio: {case_row['service_type'] or '-'}\n"
        f"Abonado: {case_row['abonado_code'] or '-'}"
    )
    await maybe_copy_to_group(context, route.get("evidence"), file_type, file_id, caption)

    new_count = current + 1
    await context.bot.send_message(
        chat_id=msg.chat_id,
        text=f"✅ Guardado ({new_count}/{MAX_MEDIA_PER_STEP}).",
        reply_markup=controls_kb,
    )


# =========================
# Error handler
# =========================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Error no manejado:", exc_info=context.error)


# =========================
# Main
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Falta BOT_TOKEN. Configura la variable BOT_TOKEN con el token de BotFather.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("inicio", inicio_cmd))
    app.add_handler(CommandHandler("cancelar", cancelar_cmd))
    app.add_handler(CommandHandler("estado", estado_cmd))
    app.add_handler(CommandHandler("aprobacion", aprobacion_cmd))

    # Callbacks
    app.add_handler(CallbackQueryHandler(on_callbacks))

    # Handlers
    app.add_handler(MessageHandler(filters.LOCATION, on_location))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, on_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_error_handler(error_handler)

    log.info("Bot corriendo...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()

