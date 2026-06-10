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
USER_AGENT = "Visione/16.0 (RAG + Groq)"
STORIA = deque(maxlen=10)

# GROQ
GROQ_API_KEY = os.environ.get("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")
GROQ_MODEL = "llama-3.1-8b-instant"  # Modello veloce e stabile su Groq
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# ... (Le classi Database e RicercaWeb rimangono IDENTICHE al tuo codice attuale, le ho omesse per brevità) ...

# ========== GENERAZIONE CON GROQ ==========
def genera_con_groq(prompt):
    """Invia un prompt a Groq e restituisce la risposta."""
    if not GROQ_API_KEY:
        print("GROQ_API_KEY non impostata")
        return None
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 350
    }
    try:
        resp = requests.post(GROQ_URL, json=payload, headers=headers, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        else:
            print(f"Groq status {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"Groq eccezione: {e}")
    return None

# ========== INIZIALIZZAZIONE GLOBALE ==========
db = Database()
ricerca = RicercaWeb()

def rispondi(domanda):
    # ... (La logica di risposta e creazione del prompt rimane IDENTICA) ...
    # Alla fine, la chiamata a Groq:
    risposta_groq = genera_con_groq(prompt)
    if risposta_groq:
        return risposta_groq
    else:
        # ... (Fallback identico) ...
        return "Mi dispiace, la generazione automatica non è disponibile."

# ... (Il resto del server Flask rimane IDENTICO) ...
