import os
import re
import sqlite3
import urllib.parse
from collections import deque
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

# ========== CONFIGURAZIONE ==========
DB_FILE = "visione_conoscenza.db"
TIMEOUT_WEB = 10
USER_AGENT = "Visione/16.0 (RAG + Gemini)"
STORIA = deque(maxlen=10)  # contesto conversazione

# Google Gemini
GEMINI_API_KEY = os.environ.get("AQ.Ab8RN6KWsSYYFYWNPs3ga6sJmx4l3z78aNVHmJawSOZ1PC0_IQ")
GEMINI_MODEL = "gemini-2.0-flash-lite" # Modello gratuito e veloce
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

# ========== DATABASE ==========
# ... (La classe Database rimane IDENTICA al tuo codice originale) ...

# ========== RICERCA WEB ==========
# ... (La classe RicercaWeb rimane IDENTICA al tuo codice originale) ...

# ========== INTENTI ==========
# ... (La funzione classifica_intento rimane IDENTICA al tuo codice originale) ...

# ========== GENERAZIONE CON GEMINI ==========
def genera_con_gemini(prompt):
    """Invia un prompt a Gemini e restituisce la risposta."""
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY non impostata")
        return None
    headers = {'Content-Type': 'application/json'}
    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }]
    }
    try:
        # Effettua la richiesta POST all'API di Gemini
        resp = requests.post(GEMINI_URL, json=payload, headers=headers, timeout=30)
        if resp.status_code == 200:
            # Estrae il testo dalla risposta JSON
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        else:
            print(f"Gemini status {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"Gemini eccezione: {e}")
    return None

# ========== INIZIALIZZAZIONE GLOBALE ==========
db = Database()
ricerca = RicercaWeb()

def rispondi(domanda):
    global STORIA
    intento = classifica_intento(domanda)
    
    # Risposte immediate per saluti e comandi (IDENTICO al tuo codice)
    if intento == "saluto":
        return "Ciao! Come posso aiutarti oggi?"
    if intento == "come_stai":
        return "Sto benissimo, grazie! Sono sempre operativa."
    if intento == "identita":
        return "Sono Visione, un'assistente IA con accesso a Wikipedia e a un database di conoscenza, potenziata da Google Gemini."
    if intento == "comando":
        if domanda.startswith("/cerca "):
            # ... (logica /cerca identica) ...
            pass
        elif domanda == "/stato":
            return f"📊 STATO: {db.conteggio_pagine()} pagine, {db.dim_totale_mb():.1f} MB"
        else:
            return "Comando non riconosciuto. Usa /cerca <testo> o /stato."

    # ========== RAG ==========
    # ... (La logica di ricerca nel DB e su Wikipedia rimane IDENTICA) ...
    # Alla fine, costruisci il prompt come nel codice originale che hai postato.

    # Esempio di prompt (come nel codice che funzionava con Groq)
    prompt = f"""Sei Visione, un'assistente AI intelligente e amichevole.
Usa le informazioni seguenti per rispondere alla domanda dell'utente.
Se non trovi la risposta, dì semplicemente che non lo sai.

{contesto_rag}

Utente: {domanda}

Risposta in italiano, chiara e naturale:"""

    # Chiamata a Gemini
    risposta_gemini = genera_con_gemini(prompt)
    if risposta_gemini:
        return risposta_gemini
    else:
        # Fallback: restituisci il contesto trovato se disponibile
        if contesto_rag and "Non ho trovato" not in contesto_rag:
            return f"{contesto_rag}\n\n(Generazione automatica non disponibile, ma questi dati potrebbero aiutarti.)"
        else:
            return "Mi dispiace, non ho trovato informazioni sufficienti e la generazione automatica non è disponibile. Riprova più tardi."

# ========== SERVER FLASK (IDENTICO) ==========
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()
    messaggio = data.get('message', '')
    if not messaggio:
        return jsonify({'error': 'Messaggio vuoto'}), 400
    risposta = rispondi(messaggio)
    return jsonify({'response': risposta})

@app.route('/stato', methods=['GET'])
def stato():
    return jsonify({
        "pagine": db.conteggio_pagine(),
        "dimensione_mb": round(db.dim_totale_mb(), 2)
    })

@app.route('/')
def home():
    return jsonify({"status": "Visione backend attivo", "version": "16.0-gemini"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
