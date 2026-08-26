#!/usr/bin/env python3
"""MotivaMe v5.1 - Bot Telegram per fitness e motivazione
Piano settimanale, /oggi, /domani, coach realtime, sveglia motivazionale
Persistenza dati su Redis (compatibile Railway)"""

import os
import re
import json
import logging
from datetime import datetime, timedelta, time as dtime
import zoneinfo
import redis
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackContext,
    CallbackQueryHandler, filters
)
from groq import Groq

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
TELEGRAM_TOKEN = os.environ["MOTIVAME_TELEGRAM_TOKEN"]
GROQ_KEY = os.environ["MOTIVAME_GROQ_KEY"]
AI_MODEL = "groq/compound"

# Groq client
groq_client = Groq(api_key=GROQ_KEY)

# Suffisso obbligatorio per tutti i prompt AI
PROMPT_SUFFIX = (
    "\n\nIMPORTANTE: scrivi SOLO testo normale con emoji. "
    "VIETATO usare: tabelle con |, simboli ---, asterischi **, cancelletti #, tag HTML. "
    "Usa emoji come 🏃 💪 🥗 per separare le sezioni."
)

# Giorni della settimana in italiano
GIORNI_IT = ["lunedi", "martedi", "mercoledi", "giovedi", "venerdi", "sabato", "domenica"]
GIORNI_IT_DISPLAY = ["Lunedi", "Martedi", "Mercoledi", "Giovedi", "Venerdi", "Sabato", "Domenica"]


# ===== REDIS PERSISTENCE =====

def get_redis():
    """Connessione a Redis (Railway fornisce REDIS_URL automaticamente)"""
    url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    return redis.from_url(url, decode_responses=True)


def get_user(user_id):
    """Ottieni dati utente da Redis"""
    r = get_redis()
    data = r.get(f"user:{user_id}")
    return json.loads(data) if data else None


def save_user(user_id, data):
    """Salva dati utente su Redis"""
    r = get_redis()
    r.set(f"user:{user_id}", json.dumps(data, ensure_ascii=False))


def delete_user(user_id):
    """Elimina dati utente da Redis"""
    r = get_redis()
    r.delete(f"user:{user_id}")


def get_all_user_ids():
    """Ottieni tutti gli user_id salvati su Redis"""
    r = get_redis()
    keys = r.keys("user:*")
    return [k.replace("user:", "") for k in keys]


# ===== UTILITY =====

def pulisci_testo(text):
    """Rimuove formattazione markdown/tabelle dal testo AI"""
    if not text:
        return ""
    text = re.sub(r'\|[-| ]+\|', '', text)  # rimuovi righe separatore tabelle
    text = re.sub(r'\|[^\n]+\|', lambda m: m.group().replace('|', ' ').strip(), text)  # converti celle tabella
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)  # rimuovi bold
    text = re.sub(r'#{1,6}\s', '', text)  # rimuovi headers
    text = re.sub(r'---+', '', text)  # rimuovi separatori
    text = re.sub(r'<br>', '\n', text)  # converti br
    text = re.sub(r"\\([\[\](){}])", r"\1", text)  # rimuovi escape
    text = re.sub(r'\n{3,}', '\n\n', text)  # max 2 righe vuote
    return text.strip()


def clean_ai_text(text):
    """Rimuove tag think, markdown e formattazione indesiderata"""
    if not text:
        return ""
    # Rimuovi tag <think>...</think>
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    # Rimuovi <think> senza chiusura (troncato)
    text = re.sub(r'<think>.*', '', text, flags=re.DOTALL)
    # Applica pulizia formato
    text = pulisci_testo(text)
    return text.strip()


def ask_ai(prompt, max_tokens=800):
    """Chiama il modello AI. Riceve SOLO il prompt completo."""
    try:
        r = groq_client.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.7
        )
        content = r.choices[0].message.content or ""
        cleaned = clean_ai_text(content)
        if len(cleaned) == 0:
            return "Mi dispiace, non sono riuscito a generare una risposta. Riprova tra poco."
        return cleaned
    except Exception as e:
        logger.error(f"Errore AI: {e}")
        return "Si e' verificato un errore nella generazione della risposta. Riprova tra poco."


async def send_long_message(update, text):
    """Invia messaggi lunghi in blocchi da max 3500 caratteri"""
    if len(text) <= 3500:
        await update.message.reply_text(text)
        return

    chunks = []
    while text:
        if len(text) <= 3500:
            chunks.append(text)
            break
        # Cerca un punto di interruzione naturale
        cut = text[:3500].rfind('\n')
        if cut < 1000:
            cut = text[:3500].rfind('. ')
            if cut < 1000:
                cut = 3500
            else:
                cut += 1
        chunks.append(text[:cut])
        text = text[cut:].strip()

    for chunk in chunks:
        if chunk.strip():
            await update.message.reply_text(chunk.strip())


async def send_long_message_by_chat(context, chat_id, text):
    """Invia messaggi lunghi in blocchi da max 3500 caratteri (senza update)"""
    if len(text) <= 3500:
        await context.bot.send_message(chat_id=chat_id, text=text)
        return

    chunks = []
    while text:
        if len(text) <= 3500:
            chunks.append(text)
            break
        cut = text[:3500].rfind('\n')
        if cut < 1000:
            cut = text[:3500].rfind('. ')
            if cut < 1000:
                cut = 3500
            else:
                cut += 1
        chunks.append(text[:cut])
        text = text[cut:].strip()

    for chunk in chunks:
        if chunk.strip():
            await context.bot.send_message(chat_id=chat_id, text=chunk.strip())


def calc_bmr_tdee(user_data):
    """Calcola BMR (Harris-Benedict) e TDEE"""
    peso = float(user_data.get("peso", 70))
    altezza = float(user_data.get("altezza", 170))
    eta = int(user_data.get("eta", 30))
    sesso = user_data.get("sesso", "Maschio")
    attivita = user_data.get("attivita", "Poco attivo")

    if sesso == "Maschio":
        bmr = 88.362 + (13.397 * peso) + (4.799 * altezza) - (5.677 * eta)
    else:
        bmr = 447.593 + (9.247 * peso) + (3.098 * altezza) - (4.330 * eta)

    fattori = {
        "Sedentario": 1.2,
        "Poco attivo": 1.375,
        "Attivo": 1.55,
        "Molto attivo": 1.725
    }
    tdee = bmr * fattori.get(attivita, 1.375)
    return round(bmr, 0), round(tdee, 0)


def build_user_context(user_data):
    """Costruisce il contesto utente per i prompt AI"""
    bmi = user_data.get("bmi", "N/A")
    tipo_lavoro = user_data.get("tipo_lavoro", "non specificato")
    return (
        f"Utente: {user_data.get('nome','')}, {user_data.get('eta','')} anni, "
        f"{user_data.get('sesso','')}, {user_data.get('peso','')}kg, "
        f"{user_data.get('altezza','')}cm, BMI {bmi}, "
        f"Obiettivo: {user_data.get('obiettivo','')}, "
        f"Attivita: {user_data.get('attivita','')}, "
        f"Patologie: {user_data.get('patologie','nessuna')}, "
        f"Intolleranze: {user_data.get('intolleranze','nessuna')}, "
        f"Frequenza: {user_data.get('frequenza','')} volte/sett, "
        f"Esperienza corsa: {user_data.get('esperienza_corsa','')}, "
        f"Tipo lavoro: {tipo_lavoro}, "
        f"BMR: {user_data.get('bmr','')}, TDEE: {user_data.get('tdee','')}"
    )


def get_oggi_index():
    """Ritorna l'indice del giorno della settimana (0=lunedi, 6=domenica)"""
    return datetime.now(tz=zoneinfo.ZoneInfo("Europe/Rome")).weekday()


def get_piano_giorno(user_data, day_index):
    """Estrae il piano di un giorno specifico dal piano_settimana salvato"""
    piano = user_data.get("piano_settimana", {})
    contenuto = piano.get("contenuto", "")
    if not contenuto:
        return None

    # Cerca sezioni per giorno
    giorno_nome = GIORNI_IT_DISPLAY[day_index]
    giorno_lower = GIORNI_IT[day_index]

    # Trova la sezione del giorno richiesto
    lines = contenuto.split('\n')
    sezione = []
    trovato = False
    for line in lines:
        line_lower = line.lower().strip()
        # Controlla se questa riga e' l'inizio del giorno cercato
        if giorno_lower in line_lower and not trovato:
            trovato = True
            sezione.append(line)
            continue
        # Se siamo nel giorno giusto, aggiungi righe fino al prossimo giorno
        if trovato:
            # Controlla se inizia un nuovo giorno
            is_new_day = False
            for g in GIORNI_IT:
                if g != giorno_lower and g in line_lower and len(line_lower) < 80:
                    is_new_day = True
                    break
            if is_new_day:
                break
            sezione.append(line)

    if sezione:
        return '\n'.join(sezione).strip()
    return None


# ===== ONBOARDING =====

ONBOARDING_STEPS = {
    0: {"msg": "Ciao! Sono MotivaMe, il tuo coach personale per fitness e alimentazione.\n\nCome ti chiami?", "key": "nome"},
    1: {"msg": "Piacere {nome}! Quanti anni hai?", "key": "eta"},
    2: {"msg": "Qual e' il tuo sesso?", "key": "sesso", "buttons": [["Maschio", "Femmina"]]},
    3: {"msg": "Quanto pesi? (in kg)", "key": "peso"},
    4: {"msg": "Quanto sei alto/a? (in cm)", "key": "altezza"},
    5: {"msg": "Quali sono i tuoi obiettivi? (puoi scrivere piu obiettivi, es: Perdere peso e correre)", "key": "obiettivo", "buttons": [["Perdere peso", "Correre"], ["Tonificarmi", "Mangiare meglio"], ["Perdere peso e correre", "Tutto"]]},
    6: {"msg": "Come descriveresti il tuo livello di attivita' fisica?", "key": "attivita", "buttons": [["Sedentario", "Poco attivo"], ["Attivo", "Molto attivo"]]},
    7: {"msg": "Hai patologie o condizioni mediche di cui devo tenere conto? (scrivi 'nessuna' se non ne hai)", "key": "patologie"},
    8: {"msg": "Hai intolleranze o allergie alimentari? (scrivi 'nessuna' se non ne hai)", "key": "intolleranze"},
    9: {"msg": "Quante volte a settimana vuoi allenarti?", "key": "frequenza", "buttons": [["2", "3"], ["4", "5"]]},
    10: {"msg": "Qual e' la tua esperienza con la corsa?", "key": "esperienza_corsa", "buttons": [["Mai", "Qualche volta", "Regolare"]]},
    11: {"msg": "Che tipo di lavoro svolgi?", "key": "tipo_lavoro", "buttons": [["Ufficio/scrivania", "In movimento"], ["Lavoro fisico", "Non lavoro"]]},
}


async def send_step(update, step, user_data=None):
    """Invia il messaggio per lo step corrente dell'onboarding"""
    step_info = ONBOARDING_STEPS.get(step)
    if not step_info:
        return

    msg = step_info["msg"]
    if user_data:
        msg = msg.format(**user_data)

    if "buttons" in step_info:
        keyboard = ReplyKeyboardMarkup(step_info["buttons"], one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text(msg, reply_markup=keyboard)
    else:
        await update.message.reply_text(msg, reply_markup=ReplyKeyboardRemove())


async def start_command(update: Update, context: CallbackContext):
    """Comando /start"""
    user_id = update.effective_user.id
    user_data = get_user(user_id)

    if user_data and user_data.get("onboarding_completo"):
        # Mostra menu
        menu = (
            f"Bentornato/a {user_data.get('nome', '')}!\n\n"
            "Ecco cosa posso fare per te:\n"
            "/allenamento - Piano settimanale di allenamento\n"
            "/alimentazione - Piano nutrizionale personalizzato\n"
            "/piano_settimana - Piano completo 7 giorni (allenamento + pasti)\n"
            "/oggi - Cosa fare oggi\n"
            "/domani - Cosa fare domani\n"
            "/motivami - Messaggio motivazionale\n"
            "/sveglia - Sveglia motivazionale mattutina\n"
            "/progressi - Registra o visualizza progressi\n"
            "/profilo - Visualizza il tuo profilo\n"
            "/aggiorna - Modifica dati del profilo\n"
            "/reset - Ricomincia da capo"
        )
        await update.message.reply_text(menu, reply_markup=ReplyKeyboardRemove())
    else:
        # Inizia onboarding
        context.user_data["step"] = 0
        context.user_data["profile"] = {}
        await send_step(update, 0)


async def handle_message(update: Update, context: CallbackContext):
    """Gestisce tutti i messaggi di testo (onboarding + flussi interattivi + coach realtime)"""

    # === FLUSSO ALIMENTAZIONE INTERATTIVA ===
    if context.user_data.get("alimentazione_step") is not None:
        await handle_alimentazione_flow(update, context)
        return

    # === FLUSSO AGGIORNA PROFILO ===
    if context.user_data.get("aggiorna_campo"):
        await handle_aggiorna_flow(update, context)
        return

    # === ONBOARDING ===
    if "step" not in context.user_data:
        user_id = update.effective_user.id
        user_data = get_user(user_id)
        if user_data and user_data.get("onboarding_completo"):
            # === COACH IN TEMPO REALE ===
            await handle_coach_realtime(update, context, user_data)
        else:
            context.user_data["step"] = 0
            context.user_data["profile"] = {}
            await send_step(update, 0)
        return

    step = context.user_data["step"]
    text = update.message.text.strip()
    profile = context.user_data.get("profile", {})

    step_info = ONBOARDING_STEPS.get(step)
    if not step_info:
        return

    key = step_info["key"]

    # Validazione specifica
    if key == "eta":
        try:
            eta = int(text)
            if eta < 10 or eta > 100:
                await update.message.reply_text("Per favore inserisci un'eta' valida (10-100).")
                return
            profile[key] = eta
        except ValueError:
            await update.message.reply_text("Per favore inserisci un numero valido per l'eta'.")
            return
    elif key == "peso":
        try:
            peso = float(text.replace(",", "."))
            if peso < 30 or peso > 300:
                await update.message.reply_text("Per favore inserisci un peso valido (30-300 kg).")
                return
            profile[key] = peso
        except ValueError:
            await update.message.reply_text("Per favore inserisci un numero valido per il peso.")
            return
    elif key == "altezza":
        try:
            altezza = float(text.replace(",", "."))
            if altezza < 100 or altezza > 250:
                await update.message.reply_text("Per favore inserisci un'altezza valida (100-250 cm).")
                return
            profile[key] = altezza
            bmi = round(profile["peso"] / ((altezza / 100) ** 2), 1)
            profile["bmi"] = bmi
        except ValueError:
            await update.message.reply_text("Per favore inserisci un numero valido per l'altezza.")
            return
    elif key == "sesso" and text not in ["Maschio", "Femmina"]:
        await update.message.reply_text("Per favore scegli Maschio o Femmina.")
        return
    elif key == "attivita" and text not in ["Sedentario", "Poco attivo", "Attivo", "Molto attivo"]:
        await update.message.reply_text("Per favore scegli una delle opzioni disponibili.")
        return
    elif key == "frequenza" and text not in ["2", "3", "4", "5"]:
        await update.message.reply_text("Per favore scegli tra 2, 3, 4 o 5.")
        return
    elif key == "esperienza_corsa" and text not in ["Mai", "Qualche volta", "Regolare"]:
        await update.message.reply_text("Per favore scegli una delle opzioni disponibili.")
        return
    elif key == "tipo_lavoro" and text not in ["Ufficio/scrivania", "In movimento", "Lavoro fisico", "Non lavoro"]:
        await update.message.reply_text("Per favore scegli una delle opzioni disponibili.")
        return
    else:
        profile[key] = text

    # Avanza allo step successivo
    next_step = step + 1

    if next_step > 11:
        # Onboarding completato
        bmr, tdee = calc_bmr_tdee(profile)
        profile["bmr"] = bmr
        profile["tdee"] = tdee
        profile["onboarding_completo"] = True
        profile["data_registrazione"] = datetime.now(tz=zoneinfo.ZoneInfo("Europe/Rome")).isoformat()
        profile["progressi"] = []

        # Salva
        user_id = update.effective_user.id
        save_user(user_id, profile)

        # Cleanup
        context.user_data.pop("step", None)
        context.user_data.pop("profile", None)

        bmi_text = f"Il tuo BMI e' {profile.get('bmi', 'N/A')}"
        if profile.get('bmi', 0) < 18.5:
            bmi_text += " (sottopeso)"
        elif profile.get('bmi', 0) < 25:
            bmi_text += " (normopeso)"
        elif profile.get('bmi', 0) < 30:
            bmi_text += " (sovrappeso)"
        else:
            bmi_text += " (obesita')"

        completion_msg = (
            f"Perfetto {profile['nome']}! Profilo completato.\n\n"
            f"{bmi_text}\n"
            f"BMR: {int(bmr)} kcal/giorno\n"
            f"TDEE: {int(tdee)} kcal/giorno\n\n"
            "Ecco cosa posso fare per te:\n"
            "/allenamento - Piano settimanale\n"
            "/alimentazione - Piano nutrizionale\n"
            "/piano_settimana - Piano completo 7 giorni\n"
            "/oggi - Cosa fare oggi\n"
            "/domani - Cosa fare domani\n"
            "/motivami - Messaggio motivazionale\n"
            "/sveglia - Sveglia motivazionale\n"
            "/progressi - Registra progressi\n"
            "/profilo - Il tuo profilo\n"
            "/aggiorna - Modifica profilo\n"
            "/reset - Ricomincia"
        )
        await update.message.reply_text(completion_msg, reply_markup=ReplyKeyboardRemove())
    else:
        context.user_data["step"] = next_step
        context.user_data["profile"] = profile
        await send_step(update, next_step, profile)


# ===== COACH IN TEMPO REALE =====

async def handle_coach_realtime(update: Update, context: CallbackContext, user_data):
    """Coach AI in tempo reale, tiene conto dell'ora e del piano del giorno"""
    text = update.message.text.strip()
    ora_attuale = datetime.now(tz=zoneinfo.ZoneInfo("Europe/Rome")).strftime("%H:%M")
    ora_int = datetime.now(tz=zoneinfo.ZoneInfo("Europe/Rome")).hour
    giorno_idx = get_oggi_index()
    giorno_nome = GIORNI_IT_DISPLAY[giorno_idx]

    ctx = build_user_context(user_data)

    # Cerca piano del giorno se esiste
    piano_oggi = get_piano_giorno(user_data, giorno_idx)
    piano_context = ""
    if piano_oggi:
        piano_context = f"\n\nPiano di oggi ({giorno_nome}):\n{piano_oggi}"

    # Determina fascia oraria
    if ora_int < 10:
        fascia = "mattina presto"
    elif ora_int < 12:
        fascia = "tarda mattinata"
    elif ora_int < 14:
        fascia = "ora di pranzo"
    elif ora_int < 17:
        fascia = "pomeriggio"
    elif ora_int < 20:
        fascia = "sera presto"
    else:
        fascia = "sera tardi"

    prompt = (
        f"Sei un coach fitness e nutrizionale italiano. Rispondi al messaggio dell'utente come un coach attento.\n\n"
        f"Dati utente: {ctx}\n"
        f"Ora attuale: {ora_attuale} ({fascia})\n"
        f"Giorno: {giorno_nome}"
        f"{piano_context}\n\n"
        f"Messaggio dell'utente: \"{text}\"\n\n"
        f"Se l'utente parla di fame, cibo, bevande o tentazioni:\n"
        f"- Considera l'ora attuale per suggerire cosa mangiare\n"
        f"- Se c'e' un piano, suggerisci coerentemente col piano\n"
        f"- Indica calorie se menziona cibi/bevande specifiche\n"
        f"- Suggerisci alternative salutari\n\n"
        f"Se l'utente chiede altro (motivazione, allenamento, domande), rispondi come coach.\n"
        f"Sii conciso, pratico e motivante. Max 6-8 frasi."
        f"{PROMPT_SUFFIX}"
    )

    response = ask_ai(prompt, max_tokens=600)
    await send_long_message(update, response)


# ===== PIANO SETTIMANALE =====

async def piano_settimana_command(update: Update, context: CallbackContext):
    """Genera un piano completo per 7 giorni (allenamento + pasti)"""
    user_id = update.effective_user.id
    user_data = get_user(user_id)

    if not user_data or not user_data.get("onboarding_completo"):
        await update.message.reply_text("Devi prima completare il profilo. Usa /start")
        return

    await update.message.reply_text("Sto generando il tuo piano settimanale completo (allenamento + pasti per 7 giorni)...")

    ctx = build_user_context(user_data)
    frequenza = user_data.get("frequenza", "3")

    prompt = (
        f"Sei un coach fitness e nutrizionista italiano. Crea un piano settimanale completo per 7 giorni (da Lunedi a Domenica).\n\n"
        f"Dati utente: {ctx}\n\n"
        f"Per OGNI giorno includi:\n"
        f"- Allenamento del giorno (se previsto, {frequenza} giorni su 7) oppure riposo attivo\n"
        f"- Colazione\n"
        f"- Spuntino mattina\n"
        f"- Pranzo\n"
        f"- Spuntino pomeriggio\n"
        f"- Cena\n\n"
        f"Usa emoji per separare le sezioni di ogni giorno. Inizia ogni giorno con il nome (es: 🗓 Lunedi).\n"
        f"Adatta tutto a obiettivo, intolleranze e livello di attivita dell'utente.\n"
        f"Sii specifico con porzioni e esercizi."
        f"{PROMPT_SUFFIX}"
    )

    response = ask_ai(prompt, max_tokens=2000)

    # Salva il piano
    user_data["piano_settimana"] = {
        "contenuto": response,
        "data_generazione": datetime.now(tz=zoneinfo.ZoneInfo("Europe/Rome")).isoformat()
    }
    save_user(user_id, user_data)

    await send_long_message(update, response)
    await update.message.reply_text(
        "Piano salvato! Usa /oggi per vedere cosa fare oggi, /domani per domani."
    )


# ===== OGGI E DOMANI =====

async def oggi_command(update: Update, context: CallbackContext):
    """Mostra il piano di oggi"""
    user_id = update.effective_user.id
    user_data = get_user(user_id)

    if not user_data or not user_data.get("onboarding_completo"):
        await update.message.reply_text("Devi prima completare il profilo. Usa /start")
        return

    piano = user_data.get("piano_settimana")
    if not piano or not piano.get("contenuto"):
        await update.message.reply_text(
            "Non hai ancora un piano settimanale!\n"
            "Usa /piano_settimana per generarne uno."
        )
        return

    giorno_idx = get_oggi_index()
    giorno_nome = GIORNI_IT_DISPLAY[giorno_idx]
    piano_giorno = get_piano_giorno(user_data, giorno_idx)

    if piano_giorno:
        header = f"📅 Oggi e' {giorno_nome}!\n\n"
        await send_long_message(update, header + piano_giorno)
    else:
        await update.message.reply_text(
            f"📅 Oggi e' {giorno_nome}, ma non riesco a trovare il piano di oggi.\n"
            f"Prova a rigenerare con /piano_settimana"
        )


async def domani_command(update: Update, context: CallbackContext):
    """Mostra il piano di domani"""
    user_id = update.effective_user.id
    user_data = get_user(user_id)

    if not user_data or not user_data.get("onboarding_completo"):
        await update.message.reply_text("Devi prima completare il profilo. Usa /start")
        return

    piano = user_data.get("piano_settimana")
    if not piano or not piano.get("contenuto"):
        await update.message.reply_text(
            "Non hai ancora un piano settimanale!\n"
            "Usa /piano_settimana per generarne uno."
        )
        return

    giorno_idx = (get_oggi_index() + 1) % 7
    giorno_nome = GIORNI_IT_DISPLAY[giorno_idx]
    piano_giorno = get_piano_giorno(user_data, giorno_idx)

    if piano_giorno:
        header = f"📅 Domani e' {giorno_nome}!\n\n"
        await send_long_message(update, header + piano_giorno)
    else:
        await update.message.reply_text(
            f"📅 Domani e' {giorno_nome}, ma non riesco a trovare il piano di domani.\n"
            f"Prova a rigenerare con /piano_settimana"
        )


# ===== SVEGLIA MOTIVAZIONALE =====

async def sveglia_command(update: Update, context: CallbackContext):
    """Mostra opzioni per la sveglia motivazionale mattutina"""
    user_id = update.effective_user.id
    user_data = get_user(user_id)

    if not user_data or not user_data.get("onboarding_completo"):
        await update.message.reply_text("Devi prima completare il profilo. Usa /start")
        return

    orario_attuale = user_data.get("orario_sveglia", "Non impostata")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏰ 5:30", callback_data="sveglia_05:30"),
         InlineKeyboardButton("⏰ 6:00", callback_data="sveglia_06:00")],
        [InlineKeyboardButton("⏰ 6:30", callback_data="sveglia_06:30"),
         InlineKeyboardButton("⏰ 7:00", callback_data="sveglia_07:00")],
        [InlineKeyboardButton("🔕 Disattiva", callback_data="sveglia_off")]
    ])

    msg = (
        f"⏰ Sveglia motivazionale\n\n"
        f"Stato attuale: {orario_attuale}\n\n"
        f"Ogni mattina riceverai un messaggio personalizzato con motivazione "
        f"e il programma del giorno.\n\n"
        f"A che ora vuoi la sveglia?"
    )
    await update.message.reply_text(msg, reply_markup=keyboard)


async def sveglia_callback(update: Update, context: CallbackContext):
    """Gestisce la scelta dell'orario sveglia"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    user_data = get_user(user_id)
    if not user_data:
        await query.edit_message_text("Errore: profilo non trovato. Usa /start")
        return

    data = query.data

    if data == "sveglia_off":
        user_data.pop("orario_sveglia", None)
        save_user(user_id, user_data)
        # Rimuovi job esistente
        remove_sveglia_job(context, user_id)
        await query.edit_message_text("🔕 Sveglia motivazionale disattivata.")
        return

    # Formato: sveglia_HH:MM
    orario = data.replace("sveglia_", "")
    user_data["orario_sveglia"] = orario
    save_user(user_id, user_data)

    # Imposta il job
    setup_sveglia_job(context, user_id, orario)

    await query.edit_message_text(
        f"⏰ Sveglia impostata alle {orario}!\n\n"
        f"Ogni mattina riceverai motivazione + il programma del giorno."
    )


def remove_sveglia_job(context, user_id):
    """Rimuovi job sveglia per un utente"""
    job_name = f"sveglia_{user_id}"
    current_jobs = context.job_queue.get_jobs_by_name(job_name)
    for job in current_jobs:
        job.schedule_removal()


def setup_sveglia_job(context, user_id, orario):
    """Imposta il job sveglia giornaliero"""
    remove_sveglia_job(context, user_id)

    hour, minute = map(int, orario.split(":"))
    job_time = dtime(hour=hour, minute=minute)
    job_name = f"sveglia_{user_id}"

    context.job_queue.run_daily(
        sveglia_job_callback,
        time=job_time,
        name=job_name,
        data={"user_id": user_id, "chat_id": user_id}
    )
    logger.info(f"Sveglia impostata per utente {user_id} alle {orario}")


async def sveglia_job_callback(context: CallbackContext):
    """Callback eseguita dal JobQueue per inviare il messaggio sveglia"""
    job_data = context.job.data
    user_id = job_data["user_id"]
    chat_id = job_data["chat_id"]

    user_data = get_user(user_id)
    if not user_data:
        return

    nome = user_data.get("nome", "")
    obiettivo = user_data.get("obiettivo", "migliorare")
    giorno_idx = get_oggi_index()
    giorno_nome = GIORNI_IT_DISPLAY[giorno_idx]

    # Cerca piano di oggi
    piano_oggi = get_piano_giorno(user_data, giorno_idx)
    piano_context = ""
    if piano_oggi:
        piano_context = f"\nPiano di oggi ({giorno_nome}):\n{piano_oggi}"

    prompt = (
        f"Sei un coach motivazionale italiano. Scrivi un messaggio motivazionale mattutino per {nome}.\n"
        f"Obiettivo: {obiettivo}\n"
        f"Giorno: {giorno_nome}\n"
        f"{piano_context}\n\n"
        f"Il messaggio deve:\n"
        f"- Salutare per nome\n"
        f"- Motivare con energia\n"
        f"- Se c'e' un piano, menzionare brevemente cosa prevede oggi\n"
        f"- Essere breve e potente (max 5-6 frasi)\n"
        f"- Usare emoji per dare energia"
        f"{PROMPT_SUFFIX}"
    )

    response = ask_ai(prompt, max_tokens=400)
    try:
        await send_long_message_by_chat(context, chat_id, response)
    except Exception as e:
        logger.error(f"Errore invio sveglia a {user_id}: {e}")


def restore_sveglie(app):
    """Ripristina le sveglie salvate all'avvio del bot (da Redis)"""
    try:
        user_ids = get_all_user_ids()
        for user_id in user_ids:
            user_data = get_user(user_id)
            if user_data:
                orario = user_data.get("orario_sveglia")
                if orario:
                    hour, minute = map(int, orario.split(":"))
                    job_time = dtime(hour=hour, minute=minute)
                    job_name = f"sveglia_{user_id}"
                    app.job_queue.run_daily(
                        sveglia_job_callback,
                        time=job_time,
                        name=job_name,
                        data={"user_id": int(user_id), "chat_id": int(user_id)}
                    )
                    logger.info(f"Sveglia ripristinata per {user_id} alle {orario}")
    except Exception as e:
        logger.warning(f"Impossibile ripristinare sveglie da Redis: {e}")


# ===== ALIMENTAZIONE INTERATTIVA =====

ALIMENTAZIONE_DOMANDE = [
    {"msg": "🥗 Prima di creare il tuo piano, qualche domanda veloce!\n\nChe tipo di lavoro fai?", "key": "alim_lavoro", "buttons": [["Ufficio/scrivania", "In movimento", "Lavoro fisico"]]},
    {"msg": "I tuoi orari pasti sono?", "key": "alim_orari", "buttons": [["Regolari", "Irregolari"]]},
    {"msg": "Mangi spesso fuori casa?", "key": "alim_fuori", "buttons": [["Si, spesso", "A volte", "Raramente"]]},
]


async def alimentazione_command(update: Update, context: CallbackContext):
    """Avvia il flusso interattivo alimentazione"""
    user_id = update.effective_user.id
    user_data = get_user(user_id)

    if not user_data or not user_data.get("onboarding_completo"):
        await update.message.reply_text("Devi prima completare il profilo. Usa /start")
        return

    # Inizia le domande
    context.user_data["alimentazione_step"] = 0
    context.user_data["alimentazione_risposte"] = {}
    domanda = ALIMENTAZIONE_DOMANDE[0]
    keyboard = ReplyKeyboardMarkup(domanda["buttons"], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(domanda["msg"], reply_markup=keyboard)


async def handle_alimentazione_flow(update: Update, context: CallbackContext):
    """Gestisce il flusso domande alimentazione"""
    step = context.user_data.get("alimentazione_step", 0)
    text = update.message.text.strip()
    risposte = context.user_data.get("alimentazione_risposte", {})

    domanda = ALIMENTAZIONE_DOMANDE[step]

    # Valida risposta
    opzioni_valide = [btn for row in domanda["buttons"] for btn in row]
    if text not in opzioni_valide:
        await update.message.reply_text("Per favore scegli una delle opzioni disponibili.")
        return

    risposte[domanda["key"]] = text
    context.user_data["alimentazione_risposte"] = risposte

    next_step = step + 1
    if next_step < len(ALIMENTAZIONE_DOMANDE):
        context.user_data["alimentazione_step"] = next_step
        domanda_next = ALIMENTAZIONE_DOMANDE[next_step]
        keyboard = ReplyKeyboardMarkup(domanda_next["buttons"], one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text(domanda_next["msg"], reply_markup=keyboard)
    else:
        # Tutte le domande completate, genera il piano
        context.user_data.pop("alimentazione_step", None)
        context.user_data.pop("alimentazione_risposte", None)

        await update.message.reply_text("Sto preparando il tuo piano alimentare personalizzato...", reply_markup=ReplyKeyboardRemove())

        user_id = update.effective_user.id
        user_data = get_user(user_id)
        ctx = build_user_context(user_data)

        tipo_lavoro_alim = risposte.get("alim_lavoro", "non specificato")
        orari_pasti = risposte.get("alim_orari", "non specificato")
        fuori_casa = risposte.get("alim_fuori", "non specificato")

        prompt = (
            f"Sei un nutrizionista italiano. Crea un piano alimentare giornaliero per questo utente:\n"
            f"{ctx}\n\n"
            f"Informazioni aggiuntive per il piano:\n"
            f"- Tipo di lavoro: {tipo_lavoro_alim}\n"
            f"- Orari pasti: {orari_pasti}\n"
            f"- Mangia fuori casa: {fuori_casa}\n\n"
            f"Fornisci colazione, spuntino, pranzo, merenda e cena con porzioni indicative. "
            f"Considera le intolleranze e l'obiettivo. "
            f"Adatta i pasti in base al tipo di lavoro e agli orari: "
            f"chi lavora in movimento con orari irregolari deve ricevere consigli su pasti veloci, "
            f"panini salutari, spuntini pratici da portare con se. "
            f"Chi lavora in ufficio puo' avere pasti piu' strutturati. "
            f"Aggiungi consigli pratici."
            f"{PROMPT_SUFFIX}"
        )

        response = ask_ai(prompt, max_tokens=1200)
        await send_long_message(update, response)


# ===== AGGIORNA PROFILO =====

AGGIORNA_CAMPI = {
    "Peso": "peso",
    "Altezza": "altezza",
    "Obiettivo": "obiettivo",
    "Frequenza": "frequenza",
    "Patologie": "patologie",
    "Intolleranze": "intolleranze",
}


async def aggiorna_command(update: Update, context: CallbackContext):
    """Mostra bottoni per modificare campi del profilo"""
    user_id = update.effective_user.id
    user_data = get_user(user_id)

    if not user_data or not user_data.get("onboarding_completo"):
        await update.message.reply_text("Devi prima completare il profilo. Usa /start")
        return

    keyboard = ReplyKeyboardMarkup(
        [["Peso", "Altezza", "Obiettivo"], ["Frequenza", "Patologie", "Intolleranze"]],
        one_time_keyboard=True,
        resize_keyboard=True
    )
    await update.message.reply_text("Quale campo vuoi aggiornare?", reply_markup=keyboard)
    context.user_data["aggiorna_campo"] = "scegli"


async def handle_aggiorna_flow(update: Update, context: CallbackContext):
    """Gestisce il flusso aggiornamento profilo"""
    text = update.message.text.strip()
    campo_stato = context.user_data.get("aggiorna_campo")

    if campo_stato == "scegli":
        # L'utente ha scelto quale campo aggiornare
        if text not in AGGIORNA_CAMPI:
            await update.message.reply_text("Per favore scegli uno dei campi disponibili.")
            return

        context.user_data["aggiorna_campo"] = text
        prompts_campo = {
            "Peso": "Inserisci il nuovo peso (in kg):",
            "Altezza": "Inserisci la nuova altezza (in cm):",
            "Obiettivo": "Scrivi il tuo nuovo obiettivo:",
            "Frequenza": "Quante volte a settimana vuoi allenarti?",
            "Patologie": "Scrivi le tue patologie (o 'nessuna'):",
            "Intolleranze": "Scrivi le tue intolleranze (o 'nessuna'):",
        }
        if text == "Frequenza":
            keyboard = ReplyKeyboardMarkup([["2", "3"], ["4", "5"]], one_time_keyboard=True, resize_keyboard=True)
            await update.message.reply_text(prompts_campo[text], reply_markup=keyboard)
        else:
            await update.message.reply_text(prompts_campo[text], reply_markup=ReplyKeyboardRemove())
    else:
        # L'utente ha inserito il nuovo valore
        campo_label = campo_stato
        campo_key = AGGIORNA_CAMPI.get(campo_label)
        if not campo_key:
            context.user_data.pop("aggiorna_campo", None)
            return

        user_id = update.effective_user.id
        user_data = get_user(user_id)

        # Validazione
        if campo_key == "peso":
            try:
                val = float(text.replace(",", "."))
                if val < 30 or val > 300:
                    await update.message.reply_text("Peso non valido (30-300 kg). Riprova:")
                    return
                user_data["peso"] = val
                # Ricalcola BMI e TDEE
                altezza = float(user_data.get("altezza", 170))
                user_data["bmi"] = round(val / ((altezza / 100) ** 2), 1)
                bmr, tdee = calc_bmr_tdee(user_data)
                user_data["bmr"] = bmr
                user_data["tdee"] = tdee
            except ValueError:
                await update.message.reply_text("Numero non valido. Riprova:")
                return
        elif campo_key == "altezza":
            try:
                val = float(text.replace(",", "."))
                if val < 100 or val > 250:
                    await update.message.reply_text("Altezza non valida (100-250 cm). Riprova:")
                    return
                user_data["altezza"] = val
                # Ricalcola BMI e TDEE
                peso = float(user_data.get("peso", 70))
                user_data["bmi"] = round(peso / ((val / 100) ** 2), 1)
                bmr, tdee = calc_bmr_tdee(user_data)
                user_data["bmr"] = bmr
                user_data["tdee"] = tdee
            except ValueError:
                await update.message.reply_text("Numero non valido. Riprova:")
                return
        elif campo_key == "frequenza":
            if text not in ["2", "3", "4", "5"]:
                await update.message.reply_text("Scegli tra 2, 3, 4 o 5.")
                return
            user_data["frequenza"] = text
        else:
            user_data[campo_key] = text

        save_user(user_id, user_data)
        context.user_data.pop("aggiorna_campo", None)
        await update.message.reply_text(
            f"Aggiornato! {campo_label}: {text}",
            reply_markup=ReplyKeyboardRemove()
        )


# ===== COMANDI =====

async def allenamento_command(update: Update, context: CallbackContext):
    """Genera piano di allenamento settimanale"""
    user_id = update.effective_user.id
    user_data = get_user(user_id)

    if not user_data or not user_data.get("onboarding_completo"):
        await update.message.reply_text("Devi prima completare il profilo. Usa /start")
        return

    await update.message.reply_text("Sto preparando il tuo piano di allenamento...")

    ctx = build_user_context(user_data)
    prompt = (
        f"Sei un personal trainer italiano. Crea un piano di allenamento settimanale per questo utente:\n"
        f"{ctx}\n\n"
        f"Fornisci un piano dettagliato per {user_data.get('frequenza', '3')} giorni, "
        f"con esercizi, serie, ripetizioni e tempi di recupero. Sii specifico e pratico."
        f"{PROMPT_SUFFIX}"
    )

    response = ask_ai(prompt, max_tokens=1200)
    await send_long_message(update, response)


async def motivami_command(update: Update, context: CallbackContext):
    """Messaggio motivazionale"""
    user_id = update.effective_user.id
    user_data = get_user(user_id)

    if not user_data or not user_data.get("onboarding_completo"):
        await update.message.reply_text("Devi prima completare il profilo. Usa /start")
        return

    prompt = (
        f"Sei un coach motivazionale italiano. Scrivi un messaggio motivazionale breve e potente "
        f"per {user_data.get('nome', 'l utente')} che vuole {user_data.get('obiettivo', 'migliorare').lower()}. "
        f"Sii diretto, energico e ispirante. Max 4-5 frasi."
        f"{PROMPT_SUFFIX}"
    )

    response = ask_ai(prompt, max_tokens=400)
    await update.message.reply_text(response)


async def progressi_command(update: Update, context: CallbackContext):
    """Registra o visualizza progressi"""
    user_id = update.effective_user.id
    user_data = get_user(user_id)

    if not user_data or not user_data.get("onboarding_completo"):
        await update.message.reply_text("Devi prima completare il profilo. Usa /start")
        return

    progressi = user_data.get("progressi", [])

    if not progressi:
        msg = (
            "Non hai ancora registrato progressi.\n\n"
            "Per registrare il tuo peso attuale, invia:\n"
            "/progressi 75.5\n\n"
            "(sostituisci 75.5 con il tuo peso)"
        )
        await update.message.reply_text(msg)
        return

    # Mostra ultimi 10 progressi
    msg = "I tuoi progressi:\n\n"
    for p in progressi[-10:]:
        msg += f"{p['data']}: {p['peso']} kg\n"

    if len(progressi) >= 2:
        diff = progressi[-1]['peso'] - progressi[0]['peso']
        segno = "+" if diff > 0 else ""
        msg += f"\nVariazione totale: {segno}{diff:.1f} kg"

    await update.message.reply_text(msg)


async def progressi_con_peso(update: Update, context: CallbackContext):
    """Registra un nuovo peso nei progressi"""
    user_id = update.effective_user.id
    user_data = get_user(user_id)

    if not user_data or not user_data.get("onboarding_completo"):
        return

    if not context.args:
        return

    try:
        peso = float(context.args[0].replace(",", "."))
        if peso < 30 or peso > 300:
            await update.message.reply_text("Peso non valido. Inserisci un valore tra 30 e 300 kg.")
            return

        if "progressi" not in user_data:
            user_data["progressi"] = []

        user_data["progressi"].append({
            "data": datetime.now(tz=zoneinfo.ZoneInfo("Europe/Rome")).strftime("%d/%m/%Y"),
            "peso": peso
        })
        user_data["peso"] = peso

        # Ricalcola BMI
        altezza = float(user_data.get("altezza", 170))
        user_data["bmi"] = round(peso / ((altezza / 100) ** 2), 1)

        save_user(user_id, user_data)
        await update.message.reply_text(f"Peso registrato: {peso} kg. Continua cosi!")
    except (ValueError, IndexError):
        await update.message.reply_text("Formato non valido. Usa: /progressi 75.5")


async def profilo_command(update: Update, context: CallbackContext):
    """Mostra profilo utente"""
    user_id = update.effective_user.id
    user_data = get_user(user_id)

    if not user_data or not user_data.get("onboarding_completo"):
        await update.message.reply_text("Devi prima completare il profilo. Usa /start")
        return

    msg = (
        f"Il tuo profilo:\n\n"
        f"Nome: {user_data.get('nome', '')}\n"
        f"Eta': {user_data.get('eta', '')} anni\n"
        f"Sesso: {user_data.get('sesso', '')}\n"
        f"Peso: {user_data.get('peso', '')} kg\n"
        f"Altezza: {user_data.get('altezza', '')} cm\n"
        f"BMI: {user_data.get('bmi', '')}\n"
        f"Obiettivo: {user_data.get('obiettivo', '')}\n"
        f"Attivita': {user_data.get('attivita', '')}\n"
        f"Patologie: {user_data.get('patologie', 'nessuna')}\n"
        f"Intolleranze: {user_data.get('intolleranze', 'nessuna')}\n"
        f"Frequenza: {user_data.get('frequenza', '')} volte/settimana\n"
        f"Esperienza corsa: {user_data.get('esperienza_corsa', '')}\n"
        f"Tipo lavoro: {user_data.get('tipo_lavoro', 'non specificato')}\n"
        f"BMR: {int(user_data.get('bmr', 0))} kcal/giorno\n"
        f"TDEE: {int(user_data.get('tdee', 0))} kcal/giorno\n"
        f"Sveglia: {user_data.get('orario_sveglia', 'Non impostata')}"
    )
    await update.message.reply_text(msg)


async def reset_command(update: Update, context: CallbackContext):
    """Cancella profilo e ricomincia"""
    user_id = update.effective_user.id
    delete_user(user_id)

    # Rimuovi sveglia
    remove_sveglia_job(context, user_id)

    # Pulisci user_data
    context.user_data.clear()

    await update.message.reply_text(
        "Profilo cancellato. Usa /start per ricominciare.",
        reply_markup=ReplyKeyboardRemove()
    )


# ===== MAIN =====

def main():
    """Avvia il bot"""
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Comandi
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("allenamento", allenamento_command))
    app.add_handler(CommandHandler("alimentazione", alimentazione_command))
    app.add_handler(CommandHandler("piano_settimana", piano_settimana_command))
    app.add_handler(CommandHandler("oggi", oggi_command))
    app.add_handler(CommandHandler("domani", domani_command))
    app.add_handler(CommandHandler("motivami", motivami_command))
    app.add_handler(CommandHandler("sveglia", sveglia_command))
    app.add_handler(CommandHandler("progressi", progressi_con_peso, has_args=True))
    app.add_handler(CommandHandler("progressi", progressi_command, has_args=False))
    app.add_handler(CommandHandler("profilo", profilo_command))
    app.add_handler(CommandHandler("aggiorna", aggiorna_command))
    app.add_handler(CommandHandler("reset", reset_command))

    # Callback per bottoni inline (sveglia)
    app.add_handler(CallbackQueryHandler(sveglia_callback, pattern=r"^sveglia_"))

    # Messaggi di testo (onboarding + flussi interattivi + coach)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Ripristina sveglie salvate
    restore_sveglie(app)

    logger.info("Bot MotivaMe v5.1 avviato! (Redis persistence)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
