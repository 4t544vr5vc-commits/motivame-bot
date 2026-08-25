import os
import json
import logging
import math
from pathlib import Path
from datetime import datetime

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from groq import Groq

# --- Config ---
TELEGRAM_TOKEN = os.environ["MOTIVAME_TELEGRAM_TOKEN"]
GROQ_KEY = os.environ["MOTIVAME_GROQ_KEY"]
USERS_FILE = Path(__file__).parent / "users.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

groq_client = Groq(api_key=GROQ_KEY)

# --- Conversation states (onboarding) ---
(
    NOME,
    ETA,
    SESSO,
    PESO,
    ALTEZZA,
    OBIETTIVO,
    LIVELLO_ATTIVITA,
    PATOLOGIE,
    INTOLLERANZE,
    FREQUENZA,
    ESPERIENZA_CORSA,
) = range(11)

# States for progress registration
PROGRESSI_INPUT = 50

# States for profile update
PROFILO_SCELTA, PROFILO_VALORE = 60, 61

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


def delete_user(user_id: str):
    users = load_users()
    users.pop(str(user_id), None)
    save_users(users)


# --- Utility ---

def calcola_bmi(peso_kg: float, altezza_cm: float) -> float:
    altezza_m = altezza_cm / 100.0
    return round(peso_kg / (altezza_m ** 2), 1)


def categoria_bmi(bmi: float) -> str:
    if bmi < 18.5:
        return "sottopeso"
    elif bmi < 25:
        return "normopeso"
    elif bmi < 30:
        return "sovrappeso"
    elif bmi < 35:
        return "obesita di I grado"
    elif bmi < 40:
        return "obesita di II grado"
    else:
        return "obesita di III grado"


def calcola_bmr(peso_kg: float, altezza_cm: float, eta: int, sesso: str) -> float:
    """Calcola il metabolismo basale con la formula di Mifflin-St Jeor."""
    if sesso.lower() in ("m", "maschio", "uomo"):
        return 10 * peso_kg + 6.25 * altezza_cm - 5 * eta + 5
    else:
        return 10 * peso_kg + 6.25 * altezza_cm - 5 * eta - 161


def calcola_tdee(bmr: float, livello: str) -> float:
    """Calcola il fabbisogno calorico totale giornaliero."""
    fattori = {
        "sedentario": 1.2,
        "leggermente attivo": 1.375,
        "moderatamente attivo": 1.55,
        "molto attivo": 1.725,
    }
    fattore = fattori.get(livello, 1.4)
    return round(bmr * fattore)


def build_user_context(user: dict) -> str:
    """Costruisce il contesto utente per il prompt AI."""
    bmi = user.get("bmi", 0)
    bmr = user.get("bmr", 0)
    tdee = user.get("tdee", 0)

    ctx = (
        f"Nome: {user.get('nome', 'Utente')}\n"
        f"Eta: {user.get('eta', 'N/D')} anni\n"
        f"Sesso: {user.get('sesso', 'N/D')}\n"
        f"Peso: {user.get('peso', 'N/D')} kg\n"
        f"Altezza: {user.get('altezza', 'N/D')} cm\n"
        f"BMI: {bmi} ({categoria_bmi(bmi) if bmi else 'N/D'})\n"
        f"BMR (metabolismo basale): {bmr} kcal/giorno\n"
        f"TDEE (fabbisogno calorico): {tdee} kcal/giorno\n"
        f"Obiettivo: {user.get('obiettivo', 'N/D')}\n"
        f"Livello attivita: {user.get('livello_attivita', 'N/D')}\n"
        f"Patologie/problemi: {user.get('patologie', 'nessuna')}\n"
        f"Intolleranze/allergie: {user.get('intolleranze', 'nessuna')}\n"
        f"Frequenza allenamento: {user.get('frequenza', 'N/D')} volte/settimana\n"
        f"Esperienza corsa: {user.get('esperienza_corsa', 'N/D')}\n"
    )
    return ctx


def ha_patologie_serie(user: dict) -> bool:
    """Verifica se l'utente ha dichiarato patologie che richiedono supervisione medica."""
    patologie = user.get("patologie", "").lower()
    keywords = ["diabete", "cardiaco", "cardiopatia", "cuore", "ipertensione",
                "pressione alta", "aritmia", "infarto", "ictus", "epilessia",
                "renale", "reni", "epatico", "fegato", "tiroide"]
    return any(k in patologie for k in keywords)


def disclaimer_medico(user: dict) -> str:
    """Restituisce un disclaimer medico se necessario."""
    if ha_patologie_serie(user):
        return (
            "\n\n"
            "IMPORTANTE: Hai indicato condizioni di salute che richiedono "
            "supervisione medica. Questo piano e solo indicativo e NON sostituisce "
            "il parere del tuo medico o specialista. Consulta il tuo dottore prima "
            "di iniziare qualsiasi programma di allenamento o alimentazione. "
            "La tua salute viene prima di tutto!\n"
        )
    return ""


async def send_long_message(update: Update, text: str, max_len: int = 4000):
    """Invia messaggi lunghi in blocchi da max 4000 caratteri."""
    if len(text) <= max_len:
        await update.message.reply_text(text)
        return

    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Trova un punto di taglio naturale
        cut = text.rfind("\n", 0, max_len)
        if cut == -1 or cut < max_len // 2:
            cut = text.rfind(". ", 0, max_len)
        if cut == -1 or cut < max_len // 2:
            cut = max_len
        chunks.append(text[:cut + 1])
        text = text[cut + 1:].lstrip()

    for chunk in chunks:
        if chunk.strip():
            await update.message.reply_text(chunk.strip())


# --- AI helper ---

SYSTEM_PROMPT = """Sei MotivaMe, un coach professionista di allenamento e nutrizione sportiva italiano.

Il tuo approccio:
- Sei un professionista preparato in scienze motorie e nutrizione sportiva
- Dai consigli basati su evidenze scientifiche e principi corretti
- Sei amichevole ma diretto, non accetti scuse ma comprendi le difficolta
- Usi emoji con moderazione per rendere i messaggi piu leggibili
- Parli SEMPRE in italiano
- NON usi mai markdown (no asterischi *, no cancelletti #, no underscore per formattazione)
- Usi solo testo normale, emoji e a capo per formattare
- Sei esperto di corsa, fitness, allenamento funzionale e nutrizione
- Per piani alimentari: calcoli basati su BMR, TDEE, bilanciamento macronutrienti (carboidrati 45-55%, proteine 20-30%, grassi 25-30%)
- Per allenamenti: segui progressione graduale, includi sempre riscaldamento, defaticamento e giorni di recupero
- Suggerisci pasti italiani reali e accessibili
- Se l'utente ha patologie serie, SEMPRE raccomanda di consultare il medico

REGOLE DI FORMATTAZIONE:
- Mai usare * per grassetto
- Mai usare # per titoli
- Mai usare _ per corsivo
- Usa emoji come separatori e indicatori
- Usa MAIUSCOLE per enfatizzare parole chiave
- Vai a capo spesso per leggibilita"""


def ask_ai(prompt: str, user_context: str = "", max_tokens: int = 1500) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if user_context:
        messages.append({
            "role": "system",
            "content": f"Dati dell'utente che stai seguendo:\n{user_context}"
        })
    messages.append({"role": "user", "content": prompt})
    try:
        response = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.8,
        )
        text = response.choices[0].message.content
        # Rimuovi eventuale markdown residuo
        text = text.replace("**", "").replace("##", "").replace("# ", "")
        text = text.replace("__", "").replace("```", "")
        return text
    except Exception as e:
        logger.error(f"Groq error: {e}", exc_info=True)
        return "Scusa, ho avuto un problema tecnico. Riprova tra un momento!"


# --- ONBOARDING ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    user = get_user(user_id)

    if user and user.get("onboarding_completo"):
        # Utente gia registrato, mostra menu
        nome = user.get("nome", "")
        menu_text = (
            f"Ciao {nome}! Bentornato/a!\n\n"
            "Ecco cosa posso fare per te:\n\n"
            "  /allenamento - Piano settimanale personalizzato\n"
            "  /alimentazione - Piano nutrizionale su misura\n"
            "  /motivami - Carica motivazionale\n"
            "  /progressi - Registra e visualizza i tuoi progressi\n"
            "  /profilo - Visualizza o aggiorna il tuo profilo\n"
            "  /reset - Ricomincia da capo\n\n"
            "Oppure scrivimi liberamente, sono il tuo coach personale!"
        )
        await update.message.reply_text(menu_text)
        return ConversationHandler.END

    # Nuovo utente, avvia onboarding
    await update.message.reply_text(
        "Ciao! Sono MotivaMe, il tuo coach personale per "
        "allenamento e alimentazione.\n\n"
        "Per darti consigli davvero utili e sicuri, ho bisogno di conoscerti "
        "un po'. Ti faro alcune domande veloci (ci vogliono 2 minuti).\n\n"
        "Iniziamo! Come ti chiami?"
    )
    return NOME


async def ricevi_nome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    nome = update.message.text.strip()
    if len(nome) < 2 or len(nome) > 50:
        await update.message.reply_text(
            "Hmm, scrivi il tuo nome (almeno 2 caratteri)."
        )
        return NOME

    context.user_data["nome"] = nome
    await update.message.reply_text(
        f"Piacere {nome}!\n\n"
        "Quanti anni hai?"
    )
    return ETA


async def ricevi_eta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    testo = update.message.text.strip()
    try:
        eta = int(testo)
        if eta < 14 or eta > 100:
            await update.message.reply_text(
                "Inserisci un'eta valida (tra 14 e 100 anni)."
            )
            return ETA
    except ValueError:
        await update.message.reply_text(
            "Scrivi la tua eta come numero (es: 35)."
        )
        return ETA

    context.user_data["eta"] = eta

    keyboard = [["Maschio", "Femmina"]]
    await update.message.reply_text(
        "Qual e il tuo sesso biologico? (serve per calcolare il metabolismo basale)",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return SESSO


async def ricevi_sesso(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    testo = update.message.text.strip().lower()
    if testo in ("maschio", "m", "uomo", "maschile"):
        sesso = "maschio"
    elif testo in ("femmina", "f", "donna", "femminile"):
        sesso = "femmina"
    else:
        keyboard = [["Maschio", "Femmina"]]
        await update.message.reply_text(
            "Per favore scegli: Maschio o Femmina",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
        )
        return SESSO

    context.user_data["sesso"] = sesso
    await update.message.reply_text(
        "Quanto pesi attualmente? (in kg, es: 75)",
        reply_markup=ReplyKeyboardRemove(),
    )
    return PESO


async def ricevi_peso(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    testo = update.message.text.strip().replace(",", ".").replace(" kg", "").replace("kg", "")
    try:
        peso = float(testo)
        if peso < 30 or peso > 300:
            await update.message.reply_text(
                "Inserisci un peso valido in kg (es: 72.5)."
            )
            return PESO
    except ValueError:
        await update.message.reply_text(
            "Scrivi il tuo peso come numero (es: 75 oppure 72.5)."
        )
        return PESO

    context.user_data["peso"] = peso
    await update.message.reply_text(
        "Quanto sei alto/a? (in cm, es: 175)"
    )
    return ALTEZZA


async def ricevi_altezza(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    testo = update.message.text.strip().replace(",", ".").replace(" cm", "").replace("cm", "")
    try:
        altezza = float(testo)
        if altezza < 100 or altezza > 250:
            await update.message.reply_text(
                "Inserisci un'altezza valida in cm (es: 175)."
            )
            return ALTEZZA
    except ValueError:
        await update.message.reply_text(
            "Scrivi la tua altezza come numero in cm (es: 170)."
        )
        return ALTEZZA

    context.user_data["altezza"] = altezza

    # Calcola BMI
    peso = context.user_data["peso"]
    bmi = calcola_bmi(peso, altezza)
    cat = categoria_bmi(bmi)
    context.user_data["bmi"] = bmi

    await update.message.reply_text(
        f"Il tuo BMI e: {bmi} ({cat})\n\n"
        "Bene! Qual e il tuo obiettivo principale?",
    )

    keyboard = [
        ["Perdere peso", "Correre"],
        ["Migliorare resistenza", "Tonificare"],
        ["Mangiare meglio"],
    ]
    await update.message.reply_text(
        "Scegli o scrivi liberamente:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return OBIETTIVO


async def ricevi_obiettivo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    testo = update.message.text.strip().lower()
    obiettivi_validi = [
        "perdere peso", "correre", "migliorare resistenza",
        "tonificare", "mangiare meglio"
    ]
    # Accetta sia le opzioni predefinite che testo libero
    obiettivo = testo if testo in obiettivi_validi else testo
    context.user_data["obiettivo"] = obiettivo

    keyboard = [
        ["Sedentario", "Leggermente attivo"],
        ["Moderatamente attivo", "Molto attivo"],
    ]
    await update.message.reply_text(
        "Qual e il tuo livello di attivita attuale?\n\n"
        "  Sedentario = lavoro d'ufficio, poco movimento\n"
        "  Leggermente attivo = camminate, attivita leggera\n"
        "  Moderatamente attivo = esercizio 3-4 volte/settimana\n"
        "  Molto attivo = allenamento intenso quasi ogni giorno",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return LIVELLO_ATTIVITA


async def ricevi_livello(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    testo = update.message.text.strip().lower()
    livelli = ["sedentario", "leggermente attivo", "moderatamente attivo", "molto attivo"]

    livello = None
    for l in livelli:
        if l in testo or testo in l:
            livello = l
            break

    if not livello:
        livello = "moderatamente attivo"  # default ragionevole

    context.user_data["livello_attivita"] = livello
    await update.message.reply_text(
        "Hai patologie, problemi di salute o infortuni di cui devo tenere conto?\n\n"
        "(es: diabete, ipertensione, problemi cardiaci, mal di schiena, "
        "infortuni al ginocchio, ecc.)\n\n"
        "Se non hai nulla scrivi \"nessuna\".",
        reply_markup=ReplyKeyboardRemove(),
    )
    return PATOLOGIE


async def ricevi_patologie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    testo = update.message.text.strip()
    context.user_data["patologie"] = testo

    await update.message.reply_text(
        "Hai intolleranze alimentari o allergie?\n\n"
        "(es: lattosio, glutine, frutta a guscio, nichel, ecc.)\n\n"
        "Se non hai nulla scrivi \"nessuna\"."
    )
    return INTOLLERANZE


async def ricevi_intolleranze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    testo = update.message.text.strip()
    context.user_data["intolleranze"] = testo

    keyboard = [["2", "3", "4", "5", "6"]]
    await update.message.reply_text(
        "Quante volte a settimana vorresti allenarti?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return FREQUENZA


async def ricevi_frequenza(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    testo = update.message.text.strip()
    try:
        freq = int(testo)
        if freq < 1 or freq > 7:
            freq = min(max(freq, 1), 7)
    except ValueError:
        freq = 3  # default

    context.user_data["frequenza"] = freq

    keyboard = [["Mai", "Qualche volta", "Regolare"]]
    await update.message.reply_text(
        "Hai gia esperienza di corsa?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return ESPERIENZA_CORSA


async def ricevi_esperienza_corsa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    testo = update.message.text.strip().lower()
    if "mai" in testo or "no" in testo:
        esperienza = "mai"
    elif "regol" in testo or "spesso" in testo or "si" in testo:
        esperienza = "regolare"
    else:
        esperienza = "qualche volta"

    context.user_data["esperienza_corsa"] = esperienza

    # Salva il profilo completo
    user_id = str(update.effective_user.id)
    peso = context.user_data["peso"]
    altezza = context.user_data["altezza"]
    eta = context.user_data["eta"]
    sesso = context.user_data["sesso"]

    bmr = calcola_bmr(peso, altezza, eta, sesso)
    tdee = calcola_tdee(bmr, context.user_data["livello_attivita"])

    user_data = {
        "nome": context.user_data["nome"],
        "eta": eta,
        "sesso": sesso,
        "peso": peso,
        "altezza": altezza,
        "bmi": context.user_data["bmi"],
        "obiettivo": context.user_data["obiettivo"],
        "livello_attivita": context.user_data["livello_attivita"],
        "patologie": context.user_data.get("patologie", "nessuna"),
        "intolleranze": context.user_data.get("intolleranze", "nessuna"),
        "frequenza": context.user_data["frequenza"],
        "esperienza_corsa": esperienza,
        "bmr": round(bmr),
        "tdee": tdee,
        "progressi": [],
        "onboarding_completo": True,
        "created": datetime.now().isoformat(),
        "updated": datetime.now().isoformat(),
    }
    set_user(user_id, user_data)

    # Messaggio di riepilogo
    nome = user_data["nome"]
    bmi = user_data["bmi"]
    cat = categoria_bmi(bmi)

    riepilogo = (
        f"Perfetto {nome}, ho tutto quello che mi serve!\n\n"
        f"RIEPILOGO DEL TUO PROFILO\n\n"
        f"  Eta: {eta} anni\n"
        f"  Peso: {peso} kg | Altezza: {altezza} cm\n"
        f"  BMI: {bmi} ({cat})\n"
        f"  Metabolismo basale: {round(bmr)} kcal/giorno\n"
        f"  Fabbisogno stimato: {tdee} kcal/giorno\n"
        f"  Obiettivo: {user_data['obiettivo']}\n"
        f"  Allenamenti: {user_data['frequenza']}x/settimana\n"
    )

    if ha_patologie_serie(user_data):
        riepilogo += (
            "\n"
            "NOTA IMPORTANTE: Hai indicato condizioni di salute rilevanti. "
            "Ti ricordo di consultare il tuo medico prima di iniziare "
            "qualsiasi programma. Adattero i consigli di conseguenza, "
            "ma la supervisione medica e fondamentale.\n"
        )

    riepilogo += (
        "\n"
        "Ecco i comandi disponibili:\n\n"
        "  /allenamento - Piano settimanale personalizzato\n"
        "  /alimentazione - Piano nutrizionale su misura\n"
        "  /motivami - Carica motivazionale\n"
        "  /progressi - Registra e visualizza progressi\n"
        "  /profilo - Visualizza o aggiorna il profilo\n"
        "  /reset - Ricomincia da capo\n\n"
        "Sono pronto ad aiutarti a raggiungere i tuoi obiettivi!"
    )

    await send_long_message(update, riepilogo)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Ok, nessun problema. Quando vuoi ricominciare scrivi /start",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# --- /allenamento ---

async def allenamento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user or not user.get("onboarding_completo"):
        await update.message.reply_text(
            "Prima devo conoscerti! Scrivi /start per iniziare."
        )
        return

    user_ctx = build_user_context(user)
    nome = user["nome"]

    prompt = (
        f"Crea un piano di allenamento settimanale COMPLETO e PERSONALIZZATO per {nome}.\n\n"
        f"Basati su questi dati:\n{user_ctx}\n\n"
        "Il piano deve includere:\n"
        "1. Giorni di allenamento e giorni di riposo (rispetta la frequenza indicata)\n"
        "2. Per ogni giorno: riscaldamento (5-10 min), corpo principale, defaticamento/stretching\n"
        "3. Intensita, durata e tipo di esercizi specifici\n"
        "4. Progressione graduale (indica come aumentare nelle settimane successive)\n"
        "5. Consigli per il recupero\n\n"
        "Se l'utente ha patologie o limitazioni, adatta gli esercizi di conseguenza.\n"
        "Se e principiante, parti dal livello base con progressione lenta.\n"
        "Usa testo semplice con emoji come separatori, NO markdown.\n"
        "Scrivi il piano giorno per giorno in modo chiaro e pratico."
    )

    risposta = ask_ai(prompt, user_ctx, max_tokens=2000)
    risposta = f"PIANO DI ALLENAMENTO SETTIMANALE per {nome}\n\n{risposta}"
    risposta += disclaimer_medico(user)

    await send_long_message(update, risposta)


# --- /alimentazione ---

async def alimentazione(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user or not user.get("onboarding_completo"):
        await update.message.reply_text(
            "Prima devo conoscerti! Scrivi /start per iniziare."
        )
        return

    user_ctx = build_user_context(user)
    nome = user["nome"]
    tdee = user.get("tdee", 2000)
    obiettivo = user.get("obiettivo", "")

    # Aggiusta calorie in base all'obiettivo
    if "perdere peso" in obiettivo or "dimagrire" in obiettivo:
        target_cal = round(tdee * 0.80)  # deficit 20%
        nota_cal = f"(deficit del 20% rispetto al TDEE di {tdee} kcal)"
    elif "tonificare" in obiettivo or "massa" in obiettivo:
        target_cal = round(tdee * 1.10)  # surplus 10%
        nota_cal = f"(surplus del 10% rispetto al TDEE di {tdee} kcal)"
    else:
        target_cal = tdee
        nota_cal = f"(mantenimento, TDEE stimato: {tdee} kcal)"

    prompt = (
        f"Crea un piano nutrizionale giornaliero COMPLETO e PERSONALIZZATO per {nome}.\n\n"
        f"Dati utente:\n{user_ctx}\n\n"
        f"Target calorico: {target_cal} kcal/giorno {nota_cal}\n\n"
        "Il piano deve includere:\n"
        "1. Colazione, Spuntino mattina, Pranzo, Spuntino pomeriggio, Cena\n"
        "2. Per ogni pasto: alimenti specifici con porzioni in grammi\n"
        "3. Ripartizione macronutrienti: carboidrati 45-55%, proteine 20-30%, grassi 25-30%\n"
        "4. Usa piatti italiani REALI e ingredienti facilmente reperibili\n"
        "5. Varianti per 2-3 giorni diversi\n"
        "6. Idratazione consigliata\n\n"
        f"Se ci sono intolleranze ({user.get('intolleranze', 'nessuna')}), "
        "sostituisci gli alimenti problematici.\n"
        "Se ci sono patologie, adatta il piano (es: basso indice glicemico per diabete).\n"
        "Usa testo semplice con emoji come separatori, NO markdown.\n"
        "Sii pratico e concreto, come un nutrizionista che scrive un piano vero."
    )

    risposta = ask_ai(prompt, user_ctx, max_tokens=2000)
    risposta = (
        f"PIANO NUTRIZIONALE per {nome}\n"
        f"Target: {target_cal} kcal/giorno {nota_cal}\n\n"
        f"{risposta}"
    )
    risposta += disclaimer_medico(user)

    await send_long_message(update, risposta)


# --- /motivami ---

async def motivami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    nome = user.get("nome", "campione") if user else "campione"
    obiettivo = user.get("obiettivo", "migliorarti") if user else "migliorarti"

    user_ctx = build_user_context(user) if user else ""

    prompt = (
        f"Scrivi un messaggio motivazionale POTENTE e personalizzato per {nome}.\n"
        f"Il suo obiettivo e: {obiettivo}\n\n"
        "Il messaggio deve:\n"
        "- Essere diretto e incisivo (5-8 frasi)\n"
        "- Far sentire che ogni giorno conta\n"
        "- Essere specifico per il suo obiettivo\n"
        "- Dare energia e voglia di agire ORA\n"
        "- Essere amichevole ma senza accettare scuse\n"
        "- Usare emoji con gusto\n"
        "- NO markdown, solo testo e emoji"
    )
    risposta = ask_ai(prompt, user_ctx, max_tokens=500)
    await update.message.reply_text(risposta)


# --- /progressi ---

async def progressi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user or not user.get("onboarding_completo"):
        await update.message.reply_text(
            "Prima devo conoscerti! Scrivi /start per iniziare."
        )
        return

    args = context.args
    if not args:
        # Mostra ultimi progressi e chiedi input
        prog = user.get("progressi", [])
        if not prog:
            await update.message.reply_text(
                "Non hai ancora registrato progressi!\n\n"
                "Come registrare:\n"
                "  /progressi 75.2 - solo peso (kg)\n"
                "  /progressi 75.2 5.0 - peso + km corsi\n"
                "  /progressi 75.2 5.0 Mi sento bene - peso + km + nota\n\n"
                "Oppure scrivi solo /progressi peso per registrare il peso."
            )
        else:
            lines = ["I TUOI ULTIMI PROGRESSI\n"]
            for p in prog[-10:]:
                line = f"  {p['data']}"
                if p.get("peso"):
                    line += f" | Peso: {p['peso']} kg"
                if p.get("km"):
                    line += f" | Km: {p['km']}"
                if p.get("nota"):
                    line += f" | {p['nota']}"
                lines.append(line)

            # Calcola trend peso
            pesi = [float(p["peso"]) for p in prog if p.get("peso")]
            if len(pesi) >= 2:
                diff = pesi[-1] - pesi[0]
                trend = f"+{diff:.1f}" if diff > 0 else f"{diff:.1f}"
                lines.append(f"\nTrend peso: {trend} kg dall'inizio")

            await update.message.reply_text("\n".join(lines))
        return

    # Parse input: /progressi <peso> [km] [nota...]
    try:
        peso = float(args[0].replace(",", "."))
    except (ValueError, IndexError):
        await update.message.reply_text(
            "Formato: /progressi <peso_kg> [km] [nota]\n"
            "Esempio: /progressi 74.5 3.2 Corsa facile al parco"
        )
        return

    km = None
    nota = None
    if len(args) > 1:
        try:
            km = float(args[1].replace(",", "."))
            nota = " ".join(args[2:]) if len(args) > 2 else None
        except ValueError:
            # Il secondo argomento non e un numero, fa parte della nota
            nota = " ".join(args[1:])

    entry = {
        "data": datetime.now().strftime("%d/%m/%Y"),
        "peso": peso,
        "km": km,
        "nota": nota,
    }

    if "progressi" not in user:
        user["progressi"] = []
    user["progressi"].append(entry)
    user["updated"] = datetime.now().isoformat()

    # Aggiorna anche il peso nel profilo
    user["peso"] = peso
    user["bmi"] = calcola_bmi(peso, user["altezza"])

    set_user(user_id, user)

    msg = f"Progresso registrato!\n\n"
    msg += f"  Peso: {peso} kg\n"
    if km:
        msg += f"  Km: {km}\n"
    if nota:
        msg += f"  Nota: {nota}\n"
    msg += f"\n  BMI aggiornato: {user['bmi']} ({categoria_bmi(user['bmi'])})\n"
    msg += f"\nBravo/a {user['nome']}! Ogni passo conta!"

    await update.message.reply_text(msg)


# --- /profilo ---

async def profilo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user or not user.get("onboarding_completo"):
        await update.message.reply_text(
            "Non hai ancora un profilo. Scrivi /start per crearlo."
        )
        return

    bmi = user.get("bmi", 0)
    testo = (
        f"IL TUO PROFILO\n\n"
        f"  Nome: {user.get('nome')}\n"
        f"  Eta: {user.get('eta')} anni\n"
        f"  Sesso: {user.get('sesso')}\n"
        f"  Peso: {user.get('peso')} kg\n"
        f"  Altezza: {user.get('altezza')} cm\n"
        f"  BMI: {bmi} ({categoria_bmi(bmi)})\n"
        f"  Metabolismo basale: {user.get('bmr')} kcal/giorno\n"
        f"  Fabbisogno calorico: {user.get('tdee')} kcal/giorno\n\n"
        f"  Obiettivo: {user.get('obiettivo')}\n"
        f"  Livello attivita: {user.get('livello_attivita')}\n"
        f"  Frequenza allenamento: {user.get('frequenza')}x/settimana\n"
        f"  Esperienza corsa: {user.get('esperienza_corsa')}\n"
        f"  Patologie: {user.get('patologie', 'nessuna')}\n"
        f"  Intolleranze: {user.get('intolleranze', 'nessuna')}\n\n"
        f"  Registrato il: {user.get('created', 'N/D')[:10]}\n"
        f"  Ultimo aggiornamento: {user.get('updated', 'N/D')[:10]}\n\n"
        "Per aggiornare un dato, scrivi:\n"
        "  /profilo peso 73.5\n"
        "  /profilo obiettivo tonificare\n"
        "  /profilo frequenza 4\n"
        "  /profilo patologie nessuna\n"
        "  /profilo intolleranze lattosio"
    )
    args = context.args
    if args and len(args) >= 2:
        campo = args[0].lower()
        valore = " ".join(args[1:])

        campi_aggiornabili = {
            "peso": "peso",
            "obiettivo": "obiettivo",
            "frequenza": "frequenza",
            "patologie": "patologie",
            "intolleranze": "intolleranze",
            "livello": "livello_attivita",
            "livello_attivita": "livello_attivita",
        }

        if campo in campi_aggiornabili:
            campo_db = campi_aggiornabili[campo]
            if campo == "peso":
                try:
                    valore_num = float(valore.replace(",", "."))
                    user["peso"] = valore_num
                    user["bmi"] = calcola_bmi(valore_num, user["altezza"])
                    user["bmr"] = round(calcola_bmr(valore_num, user["altezza"], user["eta"], user["sesso"]))
                    user["tdee"] = calcola_tdee(user["bmr"], user["livello_attivita"])
                except ValueError:
                    await update.message.reply_text("Il peso deve essere un numero (es: 73.5).")
                    return
            elif campo == "frequenza":
                try:
                    valore_num = int(valore)
                    user["frequenza"] = min(max(valore_num, 1), 7)
                except ValueError:
                    await update.message.reply_text("La frequenza deve essere un numero (1-7).")
                    return
            else:
                user[campo_db] = valore

            user["updated"] = datetime.now().isoformat()
            set_user(user_id, user)
            await update.message.reply_text(
                f"Profilo aggiornato! {campo} = {valore}\n\n"
                "Scrivi /profilo per vedere il profilo completo."
            )
            return

    await send_long_message(update, testo)


# --- /reset ---

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if user:
        delete_user(user_id)
    await update.message.reply_text(
        "Profilo cancellato. Scrivi /start per ricominciare da zero."
    )


# --- Free text (coach mode) ---

async def risposta_libera(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)

    if not user or not user.get("onboarding_completo"):
        await update.message.reply_text(
            "Ciao! Scrivi /start per iniziare e permettermi di conoscerti."
        )
        return

    nome = user.get("nome", "amico")
    user_ctx = build_user_context(user)

    prompt = (
        f"{nome} ti scrive: \"{update.message.text}\"\n\n"
        "Rispondi come il suo coach personale.\n"
        "- Se cerca scuse, non accettarle ma sii comprensivo\n"
        "- Se ha dubbi, dai risposte concrete e pratiche\n"
        "- Se chiede consigli su allenamento o alimentazione, rispondi con competenza\n"
        "- Tieni conto del suo profilo e delle sue condizioni\n"
        "- Max 5-6 frasi, dritto al punto\n"
        "- NO markdown"
    )
    risposta = ask_ai(prompt, user_ctx, max_tokens=600)
    await update.message.reply_text(risposta)


# --- Main ---

def main():
    logger.info("MotivaMe v2 - Coach professionale avviamento...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Conversation handler for onboarding
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            NOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_nome)],
            ETA: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_eta)],
            SESSO: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_sesso)],
            PESO: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_peso)],
            ALTEZZA: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_altezza)],
            OBIETTIVO: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_obiettivo)],
            LIVELLO_ATTIVITA: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_livello)],
            PATOLOGIE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_patologie)],
            INTOLLERANZE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_intolleranze)],
            FREQUENZA: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_frequenza)],
            ESPERIENZA_CORSA: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_esperienza_corsa)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("allenamento", allenamento))
    app.add_handler(CommandHandler("alimentazione", alimentazione))
    app.add_handler(CommandHandler("motivami", motivami))
    app.add_handler(CommandHandler("progressi", progressi))
    app.add_handler(CommandHandler("profilo", profilo))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, risposta_libera))

    logger.info("MotivaMe v2 avviato! In ascolto...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
