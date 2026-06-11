import os
import re
import sqlite3
import urllib.parse
from collections import deque
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
print("DEBUG: GROQ_API_KEY =", "✅ TROVATA" if os.environ.get("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") else "❌ MANCANTE")
# ========== CONFIGURAZIONE ==========
DB_FILE = "visione_conoscenza.db"
TIMEOUT_WEB = 10
USER_AGENT = "Visione/16.0 (RAG + Groq)"
STORIA = deque(maxlen=10)

# GROQ
GROQ_API_KEY = "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"  # temporanea, poi la toglieremo
GROQ_MODEL = "llama3-8b-8192"  # Modello stabile e supportato
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# ========== DATABASE ==========
class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self.cursore = self.conn.cursor()
        self._init_db()
        print("Database inizializzato")

    def _init_db(self):
        self.cursore.execute('''
            CREATE TABLE IF NOT EXISTS pagine (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                titolo TEXT UNIQUE,
                url TEXT,
                contenuto TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                fonte TEXT
            )
        ''')
        self.cursore.execute('''
            CREATE VIRTUAL TABLE IF NOT EXISTS pagine_fts USING fts5(
                titolo, contenuto, content=pagine
            )
        ''')
        self.conn.commit()

    def aggiungi_pagina(self, titolo, url, contenuto, fonte):
        if self.pagina_esiste(titolo):
            return False
        try:
            self.cursore.execute(
                "INSERT INTO pagine (titolo, url, contenuto, fonte) VALUES (?, ?, ?, ?)",
                (titolo, url, contenuto, fonte)
            )
            self.conn.commit()
            return True
        except:
            return False

    def pagina_esiste(self, titolo):
        self.cursore.execute("SELECT 1 FROM pagine WHERE titolo = ?", (titolo,))
        return self.cursore.fetchone() is not None

    def cerca(self, query, limit=3):
        q = query.lower().strip()
        self.cursore.execute("SELECT titolo, contenuto FROM pagine WHERE LOWER(titolo) = ?", (q,))
        riga = self.cursore.fetchone()
        if riga:
            return [(riga[0], riga[1], 1.0)]
        self.cursore.execute("SELECT titolo, contenuto FROM pagine WHERE LOWER(titolo) LIKE ? LIMIT 5", (f"%{q}%",))
        risultati = [(t, c, 0.9) for t, c in self.cursore.fetchall()]
        if risultati:
            return risultati[:limit]
        try:
            self.cursore.execute("SELECT titolo, contenuto, rank FROM pagine_fts WHERE pagine_fts MATCH ? LIMIT 10", (q,))
            candidati = []
            for titolo, contenuto, rank in self.cursore.fetchall():
                pert = max(0, 1 - rank / 100.0)
                if len(q) < 5 and len(contenuto) > 5000:
                    pert *= 0.3
                candidati.append((pert, titolo, contenuto))
            candidati.sort(reverse=True, key=lambda x: x[0])
            return [(t, c, pert) for pert, t, c in candidati[:limit]]
        except:
            return []

    def conteggio_pagine(self):
        self.cursore.execute("SELECT COUNT(*) FROM pagine")
        return self.cursore.fetchone()[0]

    def dim_totale_mb(self):
        try:
            return os.path.getsize(DB_FILE) / (1024*1024)
        except:
            return 0

    def chiudi(self):
        self.conn.close()

# ========== RICERCA WEB ==========
# Aggiungi in fondo, prima di if __name__ == '__main__'

import requests
from bs4 import BeautifulSoup  # se vuoi fare scraping, ma evita dipendenze: usa API

@app.route('/cerca_web', methods=['POST'])
def cerca_web():
    data = request.get_json()
    query = data.get('query', '')
    if not query:
        return jsonify({'error': 'No query'}), 400
    risultati = {}
    # Wikipedia (API)
    wiki_url = f"https://it.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(query)}&format=json"
    try:
        resp = requests.get(wiki_url, timeout=5)
        wiki_data = resp.json()
        risultati['wikipedia'] = [{'title': r['title'], 'snippet': r['snippet']} for r in wiki_data.get('query', {}).get('search', [])[:3]]
    except: pass
    # DuckDuckGo (API lite)
    ddg_url = f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote(query)}"
    try:
        resp = requests.get(ddg_url, timeout=5)
        soup = BeautifulSoup(resp.text, 'html.parser')
        snippets = soup.find_all('p', class_='result-snippet')
        risultati['duckduckgo'] = [s.get_text() for s in snippets[:3]]
    except: risultati['duckduckgo'] = []
    # Internet Archive (search)
    ia_url = f"https://archive.org/advancedsearch.php?q={urllib.parse.quote(query)}&fl%5B%5D=title&fl%5B%5D=identifier&rows=3&page=1&output=json"
    try:
        resp = requests.get(ia_url, timeout=5)
        ia_data = resp.json()
        risultati['internet_archive'] = [{'title': d['title'], 'id': d['identifier']} for d in ia_data.get('response', {}).get('docs', [])]
    except: pass
    # YouTube (richiede API key, la userai tu)
    # risultati['youtube'] = []  # da implementare con API key
    return jsonify(risultati)

@app.route('/analizza_immagine', methods=['POST'])
def analizza_immagine():
    data = request.get_json()
    image_base64 = data.get('image_base64', '')
    if not image_base64:
        return jsonify({'error': 'No image'}), 400
    # Qui puoi usare un servizio come Google Vision API, ma per semplicità usiamo Groq? Non supporta.
    # Forniamo un placeholder che restituisce un testo descrittivo fittizio.
    # In realtà dovresti inviare a un LLM che supporta immagini (es. GPT-4V).
    # Oppure usi un OCR locale (Tesseract) ma è complesso.
    return jsonify({'description': 'Immagine ricevuta, ma l’analisi visiva non è ancora implementata. Per ora descrizione fittizia.'})

@app.route('/genera_immagine', methods=['POST'])
def genera_immagine():
    data = request.get_json()
    prompt = data.get('prompt', '')
    if not prompt:
        return jsonify({'error': 'No prompt'}), 400
    # Usa Replicate o Stability AI. Devi avere API key. Fornisco placeholder.
    # Esempio con Replicate (richiede chiave)
    # replicate_api_key = os.environ.get('REPLICATE_API_KEY')
    # ... chiamata a replicate ...
    return jsonify({'image_url': 'https://placehold.co/600x400?text=Immagine+generata+placeholder'})

@app.route('/genera_audio', methods=['POST'])
def genera_audio():
    data = request.get_json()
    testo = data.get('text', '')
    if not testo:
        return jsonify({'error': 'No text'}), 400
    # Usa ElevenLabs o altro. Placeholder.
    return jsonify({'audio_url': 'https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3'})  # esempio
# ========== INTENTI ==========
def classifica_intento(testo):
    testo = testo.lower().strip()
    if testo in ["ciao", "buongiorno", "buonasera", "salve", "ehi", "hey"]:
        return "saluto"
    if re.match(r"^(come stai|come va|tutto bene|che si dice)", testo):
        return "come_stai"
    if re.match(r"^(come ti chiami|chi sei|cosa sei|ti presento)", testo):
        return "identita"
    if testo.startswith("/"):
        return "comando"
    return "domanda"

# ========== GENERAZIONE CON GROQ (con debug) ==========
def genera_con_groq(prompt):
    if not GROQ_API_KEY:
        print("DEBUG: GROQ_API_KEY mancante")
        return None
    print(f"DEBUG: Chiamata Groq con modello {GROQ_MODEL}")
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
        print(f"DEBUG: Groq risposta status {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        else:
            print(f"DEBUG: Groq errore {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"DEBUG: Groq eccezione: {e}")
    return None

# ========== INIZIALIZZAZIONE GLOBALE ==========
db = Database()
ricerca = RicercaWeb()

def rispondi(domanda):
    global STORIA
    intento = classifica_intento(domanda)
    if intento == "saluto":
        return "Ciao! Come posso aiutarti oggi?"
    if intento == "come_stai":
        return "Sto benissimo, grazie! Sono sempre operativa."
    if intento == "identita":
        return "Sono Visione, un'assistente IA con accesso a Wikipedia e a un database di conoscenza, potenziata da Groq."
    if intento == "comando":
        if domanda.startswith("/cerca "):
            query = domanda[7:].strip()
            risultati = db.cerca(query)
            if not risultati:
                return f"Nessun risultato nel database per '{query}'."
            risp = f"Risultati per '{query}':\n"
            for titolo, contenuto, score in risultati[:2]:
                risp += f"\n📖 {titolo} (score {score:.2f})\n{contenuto[:300]}...\n"
            return risp
        elif domanda == "/stato":
            return f"📊 STATO: {db.conteggio_pagine()} pagine, {db.dim_totale_mb():.1f} MB"
        else:
            return "Comando non riconosciuto. Usa /cerca <testo> o /stato."

    # RAG
    risultati_db = db.cerca(domanda, limit=2)
    contesto_rag = ""
    if risultati_db:
        contesto_rag = "Ecco informazioni dal mio database:\n\n"
        for titolo, contenuto, score in risultati_db:
            snippet = contenuto[:1000] + "..." if len(contenuto) > 1000 else contenuto
            contesto_rag += f"Fonte: {titolo}\n{snippet}\n\n"
    else:
        wiki = ricerca.wikipedia(domanda)
        ddg = ricerca.duckduckgo(domanda)
        if wiki:
            estratto, titolo, url = wiki
            contesto_rag = f"Informazione da Wikipedia (appena recuperata):\n{titolo}\n{estratto}\n\n"
            db.aggiungi_pagina(titolo, url, estratto, "wikipedia_live")
        elif ddg:
            contesto_rag = f"Informazione da DuckDuckGo:\n{ddg}\n\n"
            db.aggiungi_pagina(f"Ricerca: {domanda[:50]}", "", ddg, "duckduckgo_live")
        else:
            contesto_rag = "Non ho trovato informazioni utili.\n\n"

    prompt = f"""Sei Visione, un'assistente AI intelligente e amichevole.
Usa le informazioni seguenti per rispondere alla domanda dell'utente.
Se non trovi la risposta, dì semplicemente che non lo sai.

{contesto_rag}

Utente: {domanda}

Risposta in italiano, chiara e naturale:"""

    risposta_groq = genera_con_groq(prompt)
    if risposta_groq:
        return risposta_groq
    else:
        if contesto_rag and "Non ho trovato" not in contesto_rag:
            return f"{contesto_rag}\n\n(Generazione automatica non disponibile, ma questi dati potrebbero aiutarti.)"
        else:
            return "Mi dispiace, non ho trovato informazioni sufficienti e la generazione automatica non è disponibile. Riprova più tardi."

# ========== FLASK ==========
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
    return jsonify({"status": "Visione backend attivo", "version": "16.0-groq"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
