#!/usr/bin/env python3
"""MotivaMe v4 - Bot Telegram per fitness e motivazione
Formato pulito + alimentazione interattiva + aggiorna profilo"""

import os
import re
import json
import logging
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackContext, filters
from groq import Groq

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
TELEGRAM_TOKEN = os.environ["MOTIVAME_TELEGRAM_TOKEN"]
GROQ_KEY = os.environ["MOTIVAME_GROQ_KEY"]
AI_MODEL = "groq/compound"
USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")

# Groq client
groq_client = Groq(api_key=GROQ_KEY)

# Suffisso obbligatorio per tutti i prompt AI
PROMPT_SUFFIX = (
    "\n\nIMPORTANTE: scrivi SOLO testo normale con emoji. "
    "VIETATO usare: tabelle con |, simboli ---, asterischi **, cancelletti #, tag HTML. "
    "Usa emoji come 🏃 💪 🥗 per separare le sezioni."
)


# ===== UTILITY =====

def load_users():
    """Carica il database utenti da JSON"""
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_users(users):
    """Salva il database utenti su JSON"""
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)


def get_user(user_id):
    """Ottieni dati utente"""
    users = load_users()
    return users.get(str(user_id))


def save_user(user_id, data):
    """Salva dati utente"""
    users = load_users()
    users[str(user_id)] = data
    save_users(users)


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
            "/motivami - Messaggio motivazionale\n"
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
    """Gestisce tutti i messaggi di testo (onboarding + flussi interattivi)"""

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
            await update.message.reply_text(
                "Non ho capito. Usa uno dei comandi disponibili:\n"
                "/allenamento /alimentazione /motivami /progressi /profilo /aggiorna /reset"
            )
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
        profile["data_registrazione"] = datetime.now().isoformat()
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
            "/motivami - Messaggio motivazionale\n"
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
            "data": datetime.now().strftime("%d/%m/%Y"),
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
        f"TDEE: {int(user_data.get('tdee', 0))} kcal/giorno"
    )
    await update.message.reply_text(msg)


async def reset_command(update: Update, context: CallbackContext):
    """Cancella profilo e ricomincia"""
    user_id = update.effective_user.id
    users = load_users()
    users.pop(str(user_id), None)
    save_users(users)

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
    app.add_handler(CommandHandler("motivami", motivami_command))
    app.add_handler(CommandHandler("progressi", progressi_con_peso, has_args=True))
    app.add_handler(CommandHandler("progressi", progressi_command, has_args=False))
    app.add_handler(CommandHandler("profilo", profilo_command))
    app.add_handler(CommandHandler("aggiorna", aggiorna_command))
    app.add_handler(CommandHandler("reset", reset_command))

    # Messaggi di testo (onboarding + flussi interattivi)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot MotivaMe v4 avviato!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
