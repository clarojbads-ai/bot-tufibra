import os
import re
import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple

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

# Opcional: canal “caja fuerte” donde el bot reenvía/copía evidencias (para que no las borren)
EVIDENCE_CHANNEL_ID = os.getenv("EVIDENCE_CHANNEL_ID", "").strip()

# Validación del código de cliente
CLIENT_CODE_RE = re.compile(r"^\d{6,12}$")  # 6-12 dígitos

# Pasos (1 es código; 2..12 son fotos)
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

MAX_PHOTOS_PER_STEP = 4


# =========================
# DB helpers (SQLite)
# =========================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                status TEXT NOT NULL,         -- OPEN / CLOSED / CANCELLED
                client_code TEXT,
                step_index INTEGER NOT NULL   -- -1 esperando código; 0..10 pasos de fotos (2..12)
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cases_open ON cases(chat_id, user_id, status);")

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

        # Lotes por paso (una revisión por paso; puede haber reintentos)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS step_batches (
                batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                step_index INTEGER NOT NULL,     -- 0..10 (pasos 2..12)
                attempt INTEGER NOT NULL,        -- 1..n
                created_at TEXT NOT NULL,
                submitted INTEGER NOT NULL DEFAULT 0,
                submitted_at TEXT,
                approved INTEGER,                -- NULL pendiente, 1 ok, 0 mal
                reviewed_by INTEGER,
                reviewed_at TEXT,
                FOREIGN KEY(case_id) REFERENCES cases(case_id)
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_batches_case_step ON step_batches(case_id, step_index, attempt);")

        # Fotos (NO se guardan al disco, solo file_id + message_id)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS photos (
                photo_id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                file_unique_id TEXT NOT NULL,
                tg_message_id INTEGER NOT NULL,
                meta_json TEXT,
                FOREIGN KEY(batch_id) REFERENCES step_batches(batch_id)
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_batch ON photos(batch_id);")

        conn.commit()


def get_open_case(chat_id: int, user_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM cases WHERE chat_id=? AND user_id=? AND status='OPEN' ORDER BY case_id DESC LIMIT 1",
            (chat_id, user_id),
        ).fetchone()


def create_case(chat_id: int, user_id: int, username: str) -> sqlite3.Row:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO cases(chat_id, user_id, username, created_at, status, client_code, step_index)
            VALUES(?,?,?,?, 'OPEN', NULL, -1)
            """,
            (chat_id, user_id, username, now_utc()),
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


def set_approval_required(chat_id: int, required: bool):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO chat_config(chat_id, approval_required, updated_at)
            VALUES(?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET approval_required=excluded.approval_required, updated_at=excluded.updated_at
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


def get_case(case_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()


def _next_attempt(case_id: int, step_index: int) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT MAX(attempt) AS mx FROM step_batches WHERE case_id=? AND step_index=?",
            (case_id, step_index),
        ).fetchone()
        mx = row["mx"] if row and row["mx"] is not None else 0
        return int(mx) + 1


def get_current_batch(case_id: int, step_index: int) -> sqlite3.Row:
    """
    Devuelve el batch “activo” para el paso actual:
    - el último batch de ese paso con submitted=0 (si existe)
    - si no existe, crea uno nuevo con attempt++ y submitted=0
    """
    with db() as conn:
        row = conn.execute(
            """
            SELECT * FROM step_batches
            WHERE case_id=? AND step_index=? AND submitted=0
            ORDER BY attempt DESC LIMIT 1
            """,
            (case_id, step_index),
        ).fetchone()

        if row:
            return row

        attempt = _next_attempt(case_id, step_index)
        conn.execute(
            """
            INSERT INTO step_batches(case_id, step_index, attempt, created_at, submitted)
            VALUES(?,?,?,?,0)
            """,
            (case_id, step_index, attempt, now_utc()),
        )
        conn.commit()
        return conn.execute(
            """
            SELECT * FROM step_batches
            WHERE case_id=? AND step_index=? AND attempt=?
            """,
            (case_id, step_index, attempt),
        ).fetchone()


def batch_photo_count(batch_id: int) -> int:
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM photos WHERE batch_id=?", (batch_id,)).fetchone()
        return int(row["c"]) if row else 0


def add_photo_to_batch(batch_id: int, file_id: str, file_unique_id: str, tg_message_id: int, meta: Dict[str, Any]) -> int:
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO photos(batch_id, file_id, file_unique_id, tg_message_id, meta_json)
            VALUES(?,?,?,?,?)
            """,
            (batch_id, file_id, file_unique_id, tg_message_id, json.dumps(meta, ensure_ascii=False)),
        )
        conn.commit()
        return cur.lastrowid


def get_batch(batch_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM step_batches WHERE batch_id=?", (batch_id,)).fetchone()


def submit_batch(batch_id: int):
    with db() as conn:
        conn.execute(
            "UPDATE step_batches SET submitted=1, submitted_at=? WHERE batch_id=?",
            (now_utc(), batch_id),
        )
        conn.commit()


def set_batch_review(batch_id: int, approved: int, reviewed_by: int):
    with db() as conn:
        conn.execute(
            """
            UPDATE step_batches
            SET approved=?, reviewed_by=?, reviewed_at=?
            WHERE batch_id=?
            """,
            (approved, reviewed_by, now_utc(), batch_id),
        )
        conn.commit()


def get_photos_in_batch(batch_id: int) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM photos WHERE batch_id=? ORDER BY photo_id ASC",
            (batch_id,),
        ).fetchall()


# =========================
# Telegram helpers
# =========================
def prompt_for_step(step_index: int) -> str:
    if step_index == -1:
        return "1) Ingresa **Código de Cliente** (solo números, 6 a 12 dígitos)."
    paso_real = step_index + 2
    return (
        f"{paso_real}) {STEPS[paso_real - 1]}\n\n"
        f"📸 Envía **1 a {MAX_PHOTOS_PER_STEP} fotos** para este paso.\n"
        f"✅ Cuando termines escribe **/listo**."
    )


def step_review_keyboard(batch_id: int) -> InlineKeyboardMarkup:
    ok = f"STEP_OK|{batch_id}"
    bad = f"STEP_BAD|{batch_id}"
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Conforme (Paso completo)", callback_data=ok),
            InlineKeyboardButton("❌ Rechazar paso", callback_data=bad),
        ]]
    )


async def is_admin_of_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        return any(a.user and a.user.id == user_id for a in admins)
    except Exception:
        return False


async def maybe_copy_to_channel(context: ContextTypes.DEFAULT_TYPE, photo_file_id: str, caption: str):
    if not EVIDENCE_CHANNEL_ID:
        return
    try:
        await context.bot.send_photo(
            chat_id=int(EVIDENCE_CHANNEL_ID),
            photo=photo_file_id,
            caption=caption[:1024],  # límite Telegram
        )
    except Exception as e:
        print(f"[WARN] No pude enviar evidencia al canal: {e}")


# =========================
# Commands
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(
        "Comandos:\n"
        "• /inicio  → iniciar caso\n"
        "• /estado  → ver paso actual\n"
        "• /listo   → enviar a revisión el paso actual\n"
        "• /cancelar → cancelar y desestimar el caso\n"
        "• /aprobacion on|off → (admins) activar/desactivar conformidad\n"
    )


async def inicio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None or msg.from_user is None:
        return

    chat_id = msg.chat_id
    user_id = msg.from_user.id
    username = msg.from_user.username or msg.from_user.full_name

    # Si ya tenía caso abierto, lo reiniciamos (desde el código)
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
    msg = update.effective_message
    if msg is None or msg.from_user is None:
        return

    case_row = get_open_case(msg.chat_id, msg.from_user.id)
    if not case_row:
        await msg.reply_text("No tienes un caso abierto.")
        return

    update_case(case_row["case_id"], status="CANCELLED")
    await msg.reply_text("🧾 Caso cancelado y desestimado. Puedes iniciar otro con /inicio.")


async def estado_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None or msg.from_user is None:
        return

    case_row = get_open_case(msg.chat_id, msg.from_user.id)
    if not case_row:
        await msg.reply_text("No tienes un caso abierto. Usa /inicio.")
        return

    step_index = int(case_row["step_index"])
    approval_required = get_approval_required(msg.chat_id)
    approval_txt = "ON ✅" if approval_required else "OFF ⚠️"

    if step_index == -1:
        await msg.reply_text(
            f"📌 Caso abierto\n"
            f"• Código cliente: (pendiente)\n"
            f"• Aprobación: {approval_txt}\n"
            f"• Estado: esperando código (paso 1)\n\n"
            f"{prompt_for_step(-1)}",
            parse_mode="Markdown"
        )
        return

    batch = get_current_batch(case_row["case_id"], step_index)
    count = batch_photo_count(batch["batch_id"])

    paso_real = step_index + 2
    await msg.reply_text(
        f"📌 Caso abierto\n"
        f"• Código cliente: {case_row['client_code']}\n"
        f"• Aprobación: {approval_txt}\n"
        f"• Paso actual: {paso_real}/12\n"
        f"• Fotos cargadas en este paso: {count}/{MAX_PHOTOS_PER_STEP}\n\n"
        f"{prompt_for_step(step_index)}",
        parse_mode="Markdown"
    )


async def aprobacion_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /aprobacion
    /aprobacion on
    /aprobacion off
    Solo admins del grupo.
    """
    msg = update.effective_message
    if msg is None or msg.from_user is None:
        return

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
        await msg.reply_text("✅ Aprobación **ENCENDIDA**. Cada paso requiere conformidad de admin.", parse_mode="Markdown")
    elif val in ("off", "apagar", "desactivar", "0", "false", "no"):
        set_approval_required(chat_id, False)
        await msg.reply_text("⚠️ Aprobación **APAGADA**. Modo libre: avanzan sin conformidad.", parse_mode="Markdown")
    else:
        await msg.reply_text("Uso: /aprobacion on  o  /aprobacion off")


# =========================
# Text handler (client code)
# =========================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None or msg.from_user is None:
        return

    case_row = get_open_case(msg.chat_id, msg.from_user.id)
    if not case_row:
        return

    # Solo aceptamos código cuando step_index == -1
    if int(case_row["step_index"]) != -1:
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
# Submit step (/listo)
# =========================
async def listo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None or msg.from_user is None:
        return

    case_row = get_open_case(msg.chat_id, msg.from_user.id)
    if not case_row:
        await msg.reply_text("No tienes un caso abierto. Usa /inicio.")
        return

    if int(case_row["step_index"]) == -1:
        await msg.reply_text("Primero envía el **Código de Cliente**.", parse_mode="Markdown")
        return

    await submit_current_step(update, context, trigger="manual")


async def submit_current_step(update: Update, context: ContextTypes.DEFAULT_TYPE, trigger: str):
    """
    Envía el paso a revisión (si ON) o auto-aprueba (si OFF).
    trigger: 'manual' o 'auto'
    """
    msg = update.effective_message
    if msg is None or msg.from_user is None:
        return

    chat_id = msg.chat_id
    case_row = get_open_case(chat_id, msg.from_user.id)
    if not case_row:
        return

    step_index = int(case_row["step_index"])
    approval_required = get_approval_required(chat_id)

    batch = get_current_batch(case_row["case_id"], step_index)
    batch_id = int(batch["batch_id"])

    # Si ya está submitted, no duplicar
    if int(batch["submitted"]) == 1:
        await msg.reply_text("⏳ Este paso ya está en revisión. Espera validación.")
        return

    count = batch_photo_count(batch_id)
    if count <= 0:
        await msg.reply_text("⚠️ Aún no has enviado fotos para este paso. Envía al menos 1.")
        return

    submit_batch(batch_id)

    paso_real = step_index + 2
    attempt = int(batch["attempt"])

    if approval_required:
        await msg.reply_text(
            "📨 Paso enviado a revisión."
            + (" (Auto)" if trigger == "auto" else ""),
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔎 **Revisión requerida (Paso completo)**\n"
                f"Caso: `{case_row['client_code']}`\n"
                f"Paso {paso_real}/12 (intento {attempt})\n"
                f"Fotos: {count}\n"
                f"Técnico: {msg.from_user.full_name}\n\n"
                f"Admins: validar con botones ✅/❌"
            ),
            parse_mode="Markdown",
            reply_markup=step_review_keyboard(batch_id),
        )
    else:
        # Modo libre: aprobar y avanzar
        set_batch_review(batch_id, approved=1, reviewed_by=0)

        if step_index >= 10:
            update_case(case_row["case_id"], status="CLOSED")
            await msg.reply_text("✅ Paso registrado. 🧾 Caso COMPLETADO y cerrado (modo libre).")
            return

        new_step = step_index + 1
        update_case(case_row["case_id"], step_index=new_step)
        await msg.reply_text(
            "✅ Paso registrado (modo libre).\n\n" + prompt_for_step(new_step),
            parse_mode="Markdown"
        )


# =========================
# Photo handler
# =========================
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg: Message = update.effective_message
    if msg is None or msg.from_user is None:
        return

    chat_id = msg.chat_id
    case_row = get_open_case(chat_id, msg.from_user.id)
    if not case_row:
        return

    if int(case_row["step_index"]) == -1:
        await msg.reply_text("Primero envía el **Código de Cliente** (texto).", parse_mode="Markdown")
        return

    step_index = int(case_row["step_index"])  # 0..10
    approval_required = get_approval_required(chat_id)

    batch = get_current_batch(case_row["case_id"], step_index)
    batch_id = int(batch["batch_id"])

    # Si ya fue enviado a revisión, bloquear nuevas fotos
    if int(batch["submitted"]) == 1:
        await msg.reply_text("⏳ Este paso ya está en revisión. Espera validación del administrador.")
        return

    current_count = batch_photo_count(batch_id)
    if current_count >= MAX_PHOTOS_PER_STEP:
        await msg.reply_text(f"⚠️ Ya llegaste al máximo de {MAX_PHOTOS_PER_STEP} fotos. Escribe /listo para enviar a revisión.")
        return

    photo = msg.photo[-1]
    meta = {
        "from_user_id": msg.from_user.id,
        "from_username": msg.from_user.username,
        "from_name": msg.from_user.full_name,
        "date": msg.date.isoformat() if msg.date else None,
        "caption": msg.caption,
    }

    add_photo_to_batch(
        batch_id=batch_id,
        file_id=photo.file_id,
        file_unique_id=photo.file_unique_id,
        tg_message_id=msg.message_id,
        meta=meta,
    )

    # Copia opcional al canal “caja fuerte”
    paso_real = step_index + 2
    attempt = int(batch["attempt"])
    caption = (
        f"📌 Evidencia\n"
        f"Cliente: {case_row['client_code']}\n"
        f"Paso: {paso_real}/12 (intento {attempt})\n"
        f"Técnico: {msg.from_user.full_name}"
    )
    await maybe_copy_to_channel(context, photo.file_id, caption)

    new_count = current_count + 1
    await msg.reply_text(
        f"✅ Foto registrada ({new_count}/{MAX_PHOTOS_PER_STEP}). "
        f"Cuando termines este paso escribe /listo."
    )

    # Si llegó al máximo, enviar automáticamente a revisión
    if new_count >= MAX_PHOTOS_PER_STEP:
        await submit_current_step(update, context, trigger="auto")


# =========================
# Admin review callbacks (step-level)
# =========================
async def on_step_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q is None or q.message is None or q.from_user is None:
        return

    chat_id = q.message.chat_id
    reviewer_id = q.from_user.id

    # Solo admins del grupo
    if not await is_admin_of_chat(context, chat_id, reviewer_id):
        await q.answer("Solo administradores del grupo pueden validar.", show_alert=True)
        return

    # Si aprobación está OFF, ignorar (evita líos)
    if not get_approval_required(chat_id):
        await q.answer("Aprobación OFF (modo libre). No se requiere validar.", show_alert=True)
        return

    await q.answer()

    try:
        action, batch_id_s = q.data.split("|", 1)
        batch_id = int(batch_id_s)
    except Exception:
        await q.edit_message_text("⚠️ Callback inválido.")
        return

    batch = get_batch(batch_id)
    if not batch:
        await q.edit_message_text("⚠️ No encontré el lote de este paso.")
        return

    # Validar estado
    if int(batch["submitted"]) != 1:
        await q.edit_message_text("⚠️ Este paso aún no fue enviado a revisión.")
        return

    if batch["approved"] is not None:
        await q.edit_message_text("⚠️ Este paso ya fue revisado.")
        return

    case_row = get_case(int(batch["case_id"]))
    if not case_row or case_row["status"] != "OPEN":
        await q.edit_message_text("⚠️ Caso no encontrado o ya cerrado.")
        return

    step_index = int(batch["step_index"])
    paso_real = step_index + 2
    photos = get_photos_in_batch(batch_id)
    count = len(photos)
    attempt = int(batch["attempt"])

    if action == "STEP_OK":
        set_batch_review(batch_id, approved=1, reviewed_by=reviewer_id)

        # Avanzar paso o cerrar caso
        if step_index >= 10:
            update_case(int(case_row["case_id"]), status="CLOSED")
            await q.edit_message_text("✅ Conforme. 🧾 Caso COMPLETADO y cerrado.")
            return

        new_step = step_index + 1
        update_case(int(case_row["case_id"]), step_index=new_step)

        await q.edit_message_text("✅ Conforme. Avanzando al siguiente paso…")

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"➡️ Siguiente:\n{prompt_for_step(new_step)}",
            parse_mode="Markdown"
        )

    elif action == "STEP_BAD":
        set_batch_review(batch_id, approved=0, reviewed_by=reviewer_id)

        # Intentar borrar las fotos del paso en el grupo (si el bot tiene permisos)
        deleted_any = False
        for p in photos:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=int(p["tg_message_id"]))
                deleted_any = True
            except Exception:
                pass

        # Crear un nuevo batch “activo” (submitted=0) para el mismo paso (reintento)
        _ = get_current_batch(int(case_row["case_id"]), step_index)  # no crea si ya hay uno activo
        # Como acabamos de rechazar, el batch rechazado sigue submitted=1; por ende get_current_batch creará uno nuevo.
        with db() as conn:
            # Fuerza creación de nuevo batch (attempt++)
            attempt2 = _next_attempt(int(case_row["case_id"]), step_index)
            conn.execute(
                """
                INSERT INTO step_batches(case_id, step_index, attempt, created_at, submitted)
                VALUES(?,?,?,?,0)
                """,
                (int(case_row["case_id"]), step_index, attempt2, now_utc()),
            )
            conn.commit()

        await q.edit_message_text(
            "❌ Paso rechazado. "
            + ("(Se borraron fotos del grupo) " if deleted_any else "(No pude borrar fotos: revisa permisos del bot) ")
            + "El técnico debe reenviar este paso."
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔁 Reintento paso {paso_real}/12:\n{prompt_for_step(step_index)}",
            parse_mode="Markdown"
        )


# =========================
# Main
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Falta BOT_TOKEN. Configura la variable BOT_TOKEN con el token de BotFather (en Railway/entorno).")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Comandos
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("inicio", inicio_cmd))
    app.add_handler(CommandHandler("cancelar", cancelar_cmd))
    app.add_handler(CommandHandler("estado", estado_cmd))
    app.add_handler(CommandHandler("listo", listo_cmd))
    app.add_handler(CommandHandler("aprobacion", aprobacion_cmd))

    # Callbacks
    app.add_handler(CallbackQueryHandler(on_step_review, pattern=r"^(STEP_OK|STEP_BAD)\|"))

    # Fotos y texto
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("Bot corriendo...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
