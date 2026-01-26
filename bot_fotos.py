import os
import re
import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot_fotos.sqlite3")

# Validación del código de cliente (ajusta si usas letras, guiones, etc.)
CLIENT_CODE_RE = re.compile(r"^\d{6,12}$")  # 6-12 dígitos

# Pasos (1 es el código; 2..12 son fotos)
STEPS = [
    "Ingresa Código de Cliente",  # paso 1 (texto)
    "Envía foto de Fachada con placa de dirección y/o suministro eléctrico",
    "Envía foto panorámica de la CTO o FAT rotulada",
    "Envía la foto de la medida de potencia del puerto a utilizar",
    "Envía la foto del cintillo rotulado identificando al cliente (DNI o CE y nro de puerto)",
    "Envía foto del tramo de ingreso al domicilio",
    "Envía foto del punto de anclaje de la fibra drop en el domicilio",
    "Envía foto de la roseta abierta y medición de potencia",
    "Envía foto de la MAC de la ONT y/o equipos usados",
    "Envía foto panorámica de la ONT operativa",
    "Envía foto del test de velocidad",
    "Envía foto del acta de instalación",
]


# =========================
# DB helpers (SQLite)
# =========================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        # Casos por técnico (OPEN/CLOSED)
        conn.execute(
            """
        CREATE TABLE IF NOT EXISTS cases (
            case_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,         -- OPEN / CLOSED
            client_code TEXT,
            step_index INTEGER NOT NULL,  -- -1 esperando código; 0..10 pasos de fotos (2..12)
            UNIQUE(chat_id, user_id, status) ON CONFLICT IGNORE
        );
        """
        )

        # Fotos: guardamos file_id, message_id, y estado de aprobación
        conn.execute(
            """
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL,
            step_index INTEGER NOT NULL,      -- 0..10 (foto paso 2..12)
            file_id TEXT NOT NULL,
            file_unique_id TEXT NOT NULL,
            tg_message_id INTEGER NOT NULL,
            approved INTEGER,                 -- NULL pendiente, 1 ok, 0 mal
            reviewed_by INTEGER,
            reviewed_at TEXT,
            meta_json TEXT,
            FOREIGN KEY(case_id) REFERENCES cases(case_id)
        );
        """
        )

        # Config por chat (aprobación ON/OFF)
        conn.execute(
            """
        CREATE TABLE IF NOT EXISTS chat_config (
            chat_id INTEGER PRIMARY KEY,
            approval_required INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT
        );
        """
        )

        conn.execute("CREATE INDEX IF NOT EXISTS idx_cases_open ON cases(chat_id, user_id, status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_pending ON photos(case_id, step_index, approved);")
        conn.commit()


def get_open_case(chat_id: int, user_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        cur = conn.execute(
            "SELECT * FROM cases WHERE chat_id=? AND user_id=? AND status='OPEN' LIMIT 1",
            (chat_id, user_id),
        )
        return cur.fetchone()


def create_case(chat_id: int, user_id: int, username: str) -> sqlite3.Row:
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO cases(chat_id, user_id, username, created_at, status, client_code, step_index)
            VALUES(?,?,?,?, 'OPEN', NULL, -1)
            """,
            (chat_id, user_id, username, now),
        )
        conn.commit()
    return get_open_case(chat_id, user_id)


def update_case(case_id: int, **fields):
    keys = list(fields.keys())
    vals = [fields[k] for k in keys]
    sets = ", ".join([f"{k}=?" for k in keys])
    with db() as conn:
        conn.execute(f"UPDATE cases SET {sets} WHERE case_id=?", (*vals, case_id))
        conn.commit()


def add_photo(case_id: int, step_index: int, file_id: str, file_unique_id: str, tg_message_id: int, meta: Dict[str, Any]) -> int:
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO photos(case_id, step_index, file_id, file_unique_id, tg_message_id, meta_json)
            VALUES(?,?,?,?,?,?)
            """,
            (case_id, step_index, file_id, file_unique_id, tg_message_id, json.dumps(meta, ensure_ascii=False)),
        )
        conn.commit()
        return cur.lastrowid


def get_pending_photo(case_id: int, step_index: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        cur = conn.execute(
            """
            SELECT * FROM photos
            WHERE case_id=? AND step_index=? AND approved IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (case_id, step_index),
        )
        return cur.fetchone()


def set_photo_review(photo_row_id: int, approved: int, reviewed_by: int):
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            """
            UPDATE photos
            SET approved=?, reviewed_by=?, reviewed_at=?
            WHERE id=?
            """,
            (approved, reviewed_by, now, photo_row_id),
        )
        conn.commit()


def get_case(case_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()


def get_photo(photo_row_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM photos WHERE id=?", (photo_row_id,)).fetchone()


def get_approval_required(chat_id: int) -> bool:
    with db() as conn:
        row = conn.execute("SELECT approval_required FROM chat_config WHERE chat_id=?", (chat_id,)).fetchone()
        if row is None:
            # default ON
            conn.execute(
                "INSERT OR IGNORE INTO chat_config(chat_id, approval_required, updated_at) VALUES(?,?,?)",
                (chat_id, 1, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            return True
        return bool(row["approval_required"])


def set_approval_required(chat_id: int, required: bool):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO chat_config(chat_id, approval_required, updated_at)
            VALUES(?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET approval_required=excluded.approval_required, updated_at=excluded.updated_at
            """,
            (chat_id, 1 if required else 0, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


# =========================
# Helpers
# =========================
def review_keyboard(case_id: int, step_index: int, photo_row_id: int) -> InlineKeyboardMarkup:
    ok = f"OK|{case_id}|{step_index}|{photo_row_id}"
    bad = f"BAD|{case_id}|{step_index}|{photo_row_id}"
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Conforme", callback_data=ok),
            InlineKeyboardButton("❌ Foto mal", callback_data=bad),
        ]]
    )


async def is_admin_of_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        return any(a.user and a.user.id == user_id for a in admins)
    except Exception:
        return False


def prompt_for_step(step_index: int) -> str:
    if step_index == -1:
        return "1) Ingresa **Código de Cliente** (solo números, 6 a 12 dígitos)."
    paso_real = step_index + 2
    return f"{paso_real}) {STEPS[paso_real - 1]}"


# =========================
# Commands
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hola. Comandos:\n"
        "• /inicio  → comenzar flujo de fotos\n"
        "• /cancelar → cerrar el caso actual\n"
        "• /aprobacion  → ver estado (solo admins)\n"
        "• /aprobacion on|off → activar/desactivar aprobación (solo admins)\n"
    )


async def inicio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = msg.chat_id
    user_id = msg.from_user.id
    username = msg.from_user.username or msg.from_user.full_name

    case_row = get_open_case(chat_id, user_id)
    if not case_row:
        case_row = create_case(chat_id, user_id, username)
    else:
        update_case(case_row["case_id"], client_code=None, step_index=-1)

    approval_required = get_approval_required(chat_id)
    extra = "✅ **Aprobación:** ON (requiere admin)\n\n" if approval_required else "⚠️ **Aprobación:** OFF (modo libre)\n\n"

    await msg.reply_text(
        "✅ Caso iniciado.\n" + extra + prompt_for_step(-1),
        parse_mode="Markdown"
    )


async def cancelar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    case_row = get_open_case(msg.chat_id, msg.from_user.id)
    if not case_row:
        await msg.reply_text("No tienes un caso abierto.")
        return
    update_case(case_row["case_id"], status="CLOSED")
    await msg.reply_text("🧾 Caso cerrado/cancelado.")


async def aprobacion_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /aprobacion
    /aprobacion on
    /aprobacion off
    Solo admins del grupo.
    """
    msg = update.message
    chat_id = msg.chat_id
    user_id = msg.from_user.id

    if not await is_admin_of_chat(context, chat_id, user_id):
        await msg.reply_text("⛔ Solo administradores del grupo pueden usar este comando.")
        return

    args = context.args or []
    if not args:
        state = "ON ✅ (requiere conformidad)" if get_approval_required(chat_id) else "OFF ⚠️ (modo libre)"
        await msg.reply_text(f"Estado de aprobación: **{state}**", parse_mode="Markdown")
        return

    val = args[0].strip().lower()
    if val in ("on", "encender", "activar", "1", "true", "si", "sí"):
        set_approval_required(chat_id, True)
        await msg.reply_text("✅ Aprobación **ENCENDIDA**. Desde ahora cada foto requiere conformidad de admin.", parse_mode="Markdown")
    elif val in ("off", "apagar", "desactivar", "0", "false", "no"):
        set_approval_required(chat_id, False)
        await msg.reply_text("⚠️ Aprobación **APAGADA**. Modo libre: el técnico avanzará sin conformidad.", parse_mode="Markdown")
    else:
        await msg.reply_text("Uso: /aprobacion on  o  /aprobacion off")


# =========================
# Text handler (client code)
# =========================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    case_row = get_open_case(msg.chat_id, msg.from_user.id)
    if not case_row:
        return

    if case_row["step_index"] != -1:
        return

    code = (msg.text or "").strip()
    if not CLIENT_CODE_RE.match(code):
        await msg.reply_text("❌ Código inválido. Debe ser solo números (6 a 12 dígitos). Intenta otra vez.")
        return

    update_case(case_row["case_id"], client_code=code, step_index=0)
    await msg.reply_text(
        f"✅ Código validado: **{code}**\n\nAhora:\n{prompt_for_step(0)}",
        parse_mode="Markdown"
    )


# =========================
# Photo handler
# =========================
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg: Message = update.message
    chat_id = msg.chat_id
    case_row = get_open_case(chat_id, msg.from_user.id)
    if not case_row:
        return

    if case_row["step_index"] == -1:
        await msg.reply_text("Primero envía el **Código de Cliente** (texto).", parse_mode="Markdown")
        return

    step_index = int(case_row["step_index"])  # 0..10

    approval_required = get_approval_required(chat_id)

    # Si aprobación está ON, no permitir otra foto si hay una pendiente en este paso
    if approval_required:
        pending = get_pending_photo(case_row["case_id"], step_index)
        if pending:
            await msg.reply_text("⏳ Esa foto está pendiente de revisión. Espera validación del administrador.")
            return

    photo = msg.photo[-1]
    meta = {
        "from_user_id": msg.from_user.id,
        "from_username": msg.from_user.username,
        "from_name": msg.from_user.full_name,
        "date": msg.date.isoformat() if msg.date else None,
        "caption": msg.caption,
    }

    photo_row_id = add_photo(
        case_id=case_row["case_id"],
        step_index=step_index,
        file_id=photo.file_id,
        file_unique_id=photo.file_unique_id,
        tg_message_id=msg.message_id,
        meta=meta,
    )

    paso_real = step_index + 2

    if approval_required:
        # Revisión con botones
        await msg.reply_text(
            f"🔎 **Revisión requerida**\n"
            f"Caso: `{case_row['client_code'] or 'SIN_CODIGO'}`\n"
            f"Paso {paso_real}/12: {STEPS[paso_real - 1]}\n"
            f"Técnico: {msg.from_user.full_name}\n\n"
            f"(Admins: validen con ✅/❌)",
            parse_mode="Markdown",
            reply_markup=review_keyboard(case_row["case_id"], step_index, photo_row_id)
        )
    else:
        # Modo libre: auto-aprobar y avanzar
        set_photo_review(photo_row_id, approved=1, reviewed_by=0)  # 0 = sistema / sin reviewer
        if step_index >= 10:
            update_case(case_row["case_id"], status="CLOSED")
            await msg.reply_text("✅ Foto registrada. 🧾 Caso COMPLETADO y cerrado (modo libre).")
            return

        new_step = step_index + 1
        update_case(case_row["case_id"], step_index=new_step)
        await msg.reply_text(f"✅ Foto registrada (modo libre). Siguiente:\n{prompt_for_step(new_step)}", parse_mode="Markdown")


# =========================
# Admin review callbacks
# =========================
async def on_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    reviewer_id = q.from_user.id
    chat_id = q.message.chat_id

    # Permitir a cualquier admin del grupo
    if not await is_admin_of_chat(context, chat_id, reviewer_id):
        await q.answer("Solo administradores del grupo pueden validar.", show_alert=True)
        return

    # Si aprobación está OFF, ignorar validaciones (para evitar líos)
    if not get_approval_required(chat_id):
        await q.answer("Aprobación está OFF (modo libre). No se requiere validar.", show_alert=True)
        return

    await q.answer()

    try:
        action, case_id_s, step_index_s, photo_id_s = q.data.split("|")
        case_id = int(case_id_s)
        step_index = int(step_index_s)
        photo_row_id = int(photo_id_s)
    except Exception:
        await q.edit_message_text("⚠️ Callback inválido.")
        return

    case_row = get_case(case_id)
    if not case_row or case_row["status"] != "OPEN":
        await q.edit_message_text("⚠️ Caso no encontrado o ya cerrado.")
        return

    if int(case_row["step_index"]) != step_index:
        await q.edit_message_text("⚠️ Este caso ya avanzó o cambió de paso. Revisión no aplicada.")
        return

    photo_row = get_photo(photo_row_id)
    if not photo_row:
        await q.edit_message_text("⚠️ Foto no encontrada en BD.")
        return

    if action == "OK":
        set_photo_review(photo_row_id, approved=1, reviewed_by=reviewer_id)

        if step_index >= 10:
            update_case(case_id, status="CLOSED")
            await q.edit_message_text("✅ Conforme. 🧾 Caso COMPLETADO y cerrado.")
            return

        new_step = step_index + 1
        update_case(case_id, step_index=new_step)

        await q.edit_message_text("✅ Conforme. Avanzando al siguiente paso…")

        await context.bot.send_message(
            chat_id=case_row["chat_id"],
            text=f"➡️ Siguiente:\n{prompt_for_step(new_step)}",
            parse_mode="Markdown"
        )

    elif action == "BAD":
        set_photo_review(photo_row_id, approved=0, reviewed_by=reviewer_id)

        deleted = False
        try:
            await context.bot.delete_message(chat_id=case_row["chat_id"], message_id=int(photo_row["tg_message_id"]))
            deleted = True
        except Exception:
            deleted = False

        await q.edit_message_text(
            "❌ Foto mal. "
            + ("(Foto borrada) " if deleted else "(No pude borrar la foto: revisa permisos admin del bot) ")
            + "Vuelve a enviar la foto correcta para este paso."
        )

        await context.bot.send_message(
            chat_id=case_row["chat_id"],
            text=f"🔁 Reintento:\n{prompt_for_step(step_index)}",
            parse_mode="Markdown"
        )


# =========================
# Main
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Falta BOT_TOKEN. Configura la variable de entorno BOT_TOKEN con el token de BotFather.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("inicio", inicio_cmd))
    app.add_handler(CommandHandler("cancelar", cancelar_cmd))
    app.add_handler(CommandHandler("aprobacion", aprobacion_cmd))

    app.add_handler(CallbackQueryHandler(on_review, pattern=r"^(OK|BAD)\|"))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("Bot corriendo...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
