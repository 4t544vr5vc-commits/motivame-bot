import os
import json
import logging
from pathlib import Path
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from openai import OpenAI

# --- Config ---
TELEGRAM_TOKEN = os.environ["MOTIVAME_TELEGRAM_TOKEN"]
OPENAI_KEY = os.environ["MOTIVAME_OPENAI_KEY"]
USERS_FILE = Path(__file__).parent / "users.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

openai_client = OpenAI(api_key=OPENAI_KEY)

# --- Conversation states ---
NOME, OBIETTIVO = range(2)

# --- User persistence ---

def load_users() -> dict:
    if USERS_FILE.exists():
        return json.loads(USERS_FILE.read_text())
    return {}


def save_users(users: dict):
    USERS_FILE.write_text(json.dumps(users, indent=2, ensure_ascii=False))


def get_user(user_id: str) -> dict:
    users = load_users()
    return users.get(str(user_id), {})


def set_user(user_id: str, data: dict):
    users = load_users()
    users[str(user_id)] = data
    save_users(users)


# --- OpenAI helper ---

SYSTEM_PROMPT = """Sei MotivaMe, un coach di allenamento e alimentazione italiano.
Personalità:
- Amichevole ma diretto, prendi in giro bonariamente la pigrizia
- Usi emoji abbondanti
- Sei come un amico che spinge senza pietà 😄
- Non accetti scuse, rispondi sempre con energia e motivazione
- Parli SOLO in italiano
- Sei esperto di corsa, fitness e nutrizione"""


def ask_gpt(prompt: str, user_context: str = "") -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if user_context:
        messages.append({"role": "system", "content": f"Contesto utente: {user_context}"})
    messages.append({"role": "user", "content": prompt})
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=600,
            temperature=0.9,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        return "⚠️ Il mio cervello ha fatto un cramp! Riprova tra un attimo 💪"


# --- /start conversation ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "🏃‍♂️ Ciao! Sono *MotivaMe*, il tuo coach che non accetta scuse!\n\n"
        "Dimmi, come ti chiami? 👇",
        parse_mode="Markdown",
    )
    return NOME


async def ricevi_nome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    nome = update.message.text.strip()
    context.user_data["nome"] = nome
    await update.message.reply_text(
        f"Piacere {nome}! 💪\n\n"
        "Qual è il tuo obiettivo principale?\n"
        "1️⃣ Correre (iniziare o migliorare)\n"
        "2️⃣ Perdere peso\n"
        "3️⃣ Mangiare meglio\n\n"
        "Scrivi il numero o descrivilo a parole!"
    )
    return OBIETTIVO


async def ricevi_obiettivo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    testo = update.message.text.strip().lower()
    obiettivo_map = {
        "1": "correre",
        "2": "perdere peso",
        "3": "mangiare meglio",
    }
    obiettivo = obiettivo_map.get(testo, testo)
    user_id = str(update.effective_user.id)
    user_data = {
        "nome": context.user_data.get("nome", "Amico"),
        "obiettivo": obiettivo,
        "livello": "principiante",
        "progressi": [],
        "created": datetime.now().isoformat(),
    }
    set_user(user_id, user_data)

    await update.message.reply_text(
        f"Perfetto {user_data['nome']}! 🎯\n\n"
        f"Obiettivo registrato: *{obiettivo}*\n\n"
        "Ecco cosa posso fare per te:\n"
        "🏃 /allenamento - Piano settimanale personalizzato\n"
        "🥗 /alimentazione - Consigli nutrizionali\n"
        "🔥 /motivami - Dose di motivazione pura\n"
        "📊 /progressi - Registra i tuoi risultati\n\n"
        "Oppure scrivimi qualsiasi cosa, rispondo come il tuo coach personale! 😤💪",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Ok, quando vuoi ricominciare scrivi /start 👋")
    return ConversationHandler.END


# --- /allenamento ---

async def allenamento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        await update.message.reply_text("Prima presentati! Scrivi /start 😉")
        return

    prompt = (
        f"Crea un piano di allenamento settimanale di corsa per {user['nome']}. "
        f"Livello: {user.get('livello', 'principiante')}. "
        f"Obiettivo: {user.get('obiettivo', 'correre')}. "
        "Includi giorni, distanze, ritmi e un giorno di riposo. "
        "Sii specifico con i numeri ma anche divertente e motivante."
    )
    risposta = ask_gpt(prompt, json.dumps(user, ensure_ascii=False))
    await update.message.reply_text(f"🏃‍♂️ *Piano Allenamento per {user['nome']}*\n\n{risposta}", parse_mode="Markdown")


# --- /alimentazione ---

async def alimentazione(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        await update.message.reply_text("Prima presentati! Scrivi /start 😉")
        return

    prompt = (
        f"Dai consigli nutrizionali personalizzati a {user['nome']}. "
        f"Obiettivo: {user.get('obiettivo', 'mangiare meglio')}. "
        "Includi suggerimenti per colazione, pranzo, cena e snack. "
        "Sii pratico, con esempi italiani (pasta, pesce, verdure). "
        "Motivalo a seguire il piano!"
    )
    risposta = ask_gpt(prompt, json.dumps(user, ensure_ascii=False))
    await update.message.reply_text(f"🥗 *Consigli Alimentari per {user['nome']}*\n\n{risposta}", parse_mode="Markdown")


# --- /motivami ---

async def motivami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    nome = user.get("nome", "campione") if user else "campione"

    prompt = (
        f"Scrivi un messaggio motivazionale potente e divertente per {nome}. "
        "Deve essere breve (max 3 frasi), con emoji, "
        "che faccia ridere ma anche venire voglia di alzarsi dal divano SUBITO. "
        "Prendi in giro bonariamente la pigrizia."
    )
    risposta = ask_gpt(prompt)
    await update.message.reply_text(f"🔥 {risposta}")


# --- /progressi ---

async def progressi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        await update.message.reply_text("Prima presentati! Scrivi /start 😉")
        return

    args = context.args
    if not args:
        # Show progress history
        prog = user.get("progressi", [])
        if not prog:
            await update.message.reply_text(
                "📊 Non hai ancora registrato progressi!\n\n"
                "Usa: /progressi <km> [peso] [nota]\n"
                "Esempio: /progressi 5.2 78 Mi sento bene!"
            )
            return
        lines = []
        for p in prog[-10:]:
            line = f"📅 {p['data']} - 🏃 {p.get('km', '-')}km"
            if p.get("peso"):
                line += f" ⚖️ {p['peso']}kg"
            if p.get("nota"):
                line += f" 📝 {p['nota']}"
            lines.append(line)
        await update.message.reply_text(
            f"📊 *Ultimi progressi di {user['nome']}*\n\n" + "\n".join(lines),
            parse_mode="Markdown",
        )
        return

    # Parse: /progressi <km> [peso] [nota...]
    km = args[0] if len(args) > 0 else None
    peso = args[1] if len(args) > 1 else None
    nota = " ".join(args[2:]) if len(args) > 2 else None

    entry = {
        "data": datetime.now().strftime("%Y-%m-%d"),
        "km": km,
        "peso": peso,
        "nota": nota,
    }
    if "progressi" not in user:
        user["progressi"] = []
    user["progressi"].append(entry)
    set_user(user_id, user)

    await update.message.reply_text(
        f"✅ Progresso registrato!\n"
        f"🏃 {km} km | ⚖️ {peso or '-'} kg\n"
        f"📝 {nota or 'Nessuna nota'}\n\n"
        f"Bravo {user['nome']}! Continua così! 💪🔥"
    )


# --- Free text (coach mode) ---

async def risposta_libera(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    nome = user.get("nome", "amico") if user else "amico"
    user_context = json.dumps(user, ensure_ascii=False) if user else ""

    prompt = (
        f"{nome} ti scrive: \"{update.message.text}\"\n\n"
        "Rispondi come il suo coach personale. Se cerca scuse, non accettarle! "
        "Se ha dubbi, dai risposte concrete. Massimo 4-5 frasi."
    )
    risposta = ask_gpt(prompt, user_context)
    await update.message.reply_text(risposta)


# --- Main ---

def main():
    logger.info("🚀 MotivaMe bot starting...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Conversation handler for /start
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            NOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_nome)],
            OBIETTIVO: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_obiettivo)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("allenamento", allenamento))
    app.add_handler(CommandHandler("alimentazione", alimentazione))
    app.add_handler(CommandHandler("motivami", motivami))
    app.add_handler(CommandHandler("progressi", progressi))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, risposta_libera))

    logger.info("✅ MotivaMe bot is running! Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
