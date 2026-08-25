#!/usr/bin/env python3
"""MotivaMe v3 - Bot Telegram per fitness e motivazione"""

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


def clean_ai_text(text):
    """Rimuove tag think, markdown e formattazione indesiderata"""
    if not text:
        return ""
    # Rimuovi tag <think>...</think>
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    # Rimuovi <think> senza chiusura (troncato)
    text = re.sub(r'<think>.*', '', text, flags=re.DOTALL)
    # Rimuovi markdown
    text = text.replace('**', '').replace('##', '').replace('__', '')
    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)
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
        f"BMR: {user_data.get('bmr','')}, TDEE: {user_data.get('tdee','')}"
    )


# ===== ONBOARDING =====

ONBOARDING_STEPS = {
    0: {"msg": "Ciao! Sono MotivaMe, il tuo coach personale per fitness e alimentazione.\n\nCome ti chiami?", "key": "nome"},
    1: {"msg": "Piacere {nome}! Quanti anni hai?", "key": "eta"},
    2: {"msg": "Qual e' il tuo sesso?", "key": "sesso", "buttons": [["Maschio", "Femmina"]]},
    3: {"msg": "Quanto pesi? (in kg)", "key": "peso"},
    4: {"msg": "Quanto sei alto/a? (in cm)", "key": "altezza"},
    5: {"msg": "Qual e' il tuo obiettivo principale?", "key": "obiettivo", "buttons": [["Perdere peso", "Correre"], ["Tonificarmi", "Mangiare meglio"]]},
    6: {"msg": "Come descriveresti il tuo livello di attivita' fisica?", "key": "attivita", "buttons": [["Sedentario", "Poco attivo"], ["Attivo", "Molto attivo"]]},
    7: {"msg": "Hai patologie o condizioni mediche di cui devo tenere conto? (scrivi 'nessuna' se non ne hai)", "key": "patologie"},
    8: {"msg": "Hai intolleranze o allergie alimentari? (scrivi 'nessuna' se non ne hai)", "key": "intolleranze"},
    9: {"msg": "Quante volte a settimana vuoi allenarti?", "key": "frequenza", "buttons": [["2", "3"], ["4", "5"]]},
    10: {"msg": "Qual e' la tua esperienza con la corsa?", "key": "esperienza_corsa", "buttons": [["Mai", "Qualche volta", "Regolare"]]},
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
            "/reset - Ricomincia da capo"
        )
        await update.message.reply_text(menu, reply_markup=ReplyKeyboardRemove())
    else:
        # Inizia onboarding
        context.user_data["step"] = 0
        context.user_data["profile"] = {}
        await send_step(update, 0)


async def handle_message(update: Update, context: CallbackContext):
    """Gestisce tutti i messaggi di testo (onboarding + comandi non riconosciuti)"""
    if "step" not in context.user_data:
        # Controlla se l'utente ha gia' completato l'onboarding
        user_id = update.effective_user.id
        user_data = get_user(user_id)
        if user_data and user_data.get("onboarding_completo"):
            await update.message.reply_text(
                "Non ho capito. Usa uno dei comandi disponibili:\n"
                "/allenamento /alimentazione /motivami /progressi /profilo /reset"
            )
        else:
            # L'utente non ha completato l'onboarding, inizia
            context.user_data["step"] = 0
            context.user_data["profile"] = {}
            await send_step(update, 0)
        return
    
    step = context.user_data["step"]
    text = update.message.text.strip()
    profile = context.user_data.get("profile", {})
    
    # Valida e salva la risposta dello step corrente
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
            # Calcola BMI
            bmi = round(profile["peso"] / ((altezza / 100) ** 2), 1)
            profile["bmi"] = bmi
        except ValueError:
            await update.message.reply_text("Per favore inserisci un numero valido per l'altezza.")
            return
    elif key == "sesso" and text not in ["Maschio", "Femmina"]:
        await update.message.reply_text("Per favore scegli Maschio o Femmina.")
        return
    elif key == "obiettivo" and text not in ["Perdere peso", "Correre", "Tonificarmi", "Mangiare meglio"]:
        await update.message.reply_text("Per favore scegli una delle opzioni disponibili.")
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
    else:
        profile[key] = text
    
    # Avanza allo step successivo
    next_step = step + 1
    
    if next_step > 10:
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
            "/reset - Ricomincia"
        )
        await update.message.reply_text(completion_msg, reply_markup=ReplyKeyboardRemove())
    else:
        context.user_data["step"] = next_step
        context.user_data["profile"] = profile
        await send_step(update, next_step, profile)


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
    )
    
    response = ask_ai(prompt, max_tokens=1200)
    await send_long_message(update, response)


async def alimentazione_command(update: Update, context: CallbackContext):
    """Genera piano nutrizionale"""
    user_id = update.effective_user.id
    user_data = get_user(user_id)
    
    if not user_data or not user_data.get("onboarding_completo"):
        await update.message.reply_text("Devi prima completare il profilo. Usa /start")
        return
    
    await update.message.reply_text("Sto preparando il tuo piano alimentare...")
    
    ctx = build_user_context(user_data)
    prompt = (
        f"Sei un nutrizionista italiano. Crea un piano alimentare giornaliero per questo utente:\n"
        f"{ctx}\n\n"
        f"Fornisci colazione, spuntino, pranzo, merenda e cena con porzioni indicative. "
        f"Considera le intolleranze e l'obiettivo. Aggiungi consigli pratici."
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
        user_data["peso"] = peso  # Aggiorna peso corrente
        
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
    app.add_handler(CommandHandler("reset", reset_command))
    
    # Messaggi di testo (onboarding)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("Bot MotivaMe v3 avviato!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
