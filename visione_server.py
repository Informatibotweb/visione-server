import os
import re
import sqlite3
import urllib.parse
from collections import deque
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup

# ========== CONFIGURAZIONE ==========
DB_FILE = "visione_conoscenza.db"
TIMEOUT_WEB = 10
USER_AGENT = "Visione/18.0 (RAG + Web + Media)"
STORIA = deque(maxlen=10)

# GROQ (non usato direttamente qui, ma tenuto per eventuali fallback)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = "llama-3.1-8b-instant"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# ========== DATABASE (uguale a prima) ==========
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

# ========== RICERCA WEB (WIKIPEDIA, DDG, INTERNET ARCHIVE) ==========
class RicercaWebAvanzata:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': USER_AGENT})

    def wikipedia(self, query):
        if len(query) < 3:
            return []
        url_api = f"https://it.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(query)}&format=json"
        try:
            resp = self.session.get(url_api, timeout=TIMEOUT_WEB)
            data = resp.json()
            results = []
            for r in data.get('query', {}).get('search', [])[:3]:
                results.append({
                    'title': r['title'],
                    'snippet': re.sub('<[^<]+?>', '', r['snippet']),
                    'url': f"https://it.wikipedia.org/wiki/{urllib.parse.quote(r['title'])}"
                })
            return results
        except Exception as e:
            print(f"Wikipedia search error: {e}")
            return []

    def duckduckgo(self, query):
        url = f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote(query)}"
        try:
            resp = self.session.get(url, timeout=TIMEOUT_WEB)
            soup = BeautifulSoup(resp.text, 'html.parser')
            results = []
            for snippet in soup.find_all('p', class_='result-snippet')[:3]:
                text = snippet.get_text(strip=True)
                if text:
                    results.append({'snippet': text})
            return results
        except Exception as e:
            print(f"DuckDuckGo error: {e}")
            return []

    def internet_archive(self, query):
        url = f"https://archive.org/advancedsearch.php?q={urllib.parse.quote(query)}&fl[]=title&fl[]=identifier&rows=3&page=1&output=json"
        try:
            resp = self.session.get(url, timeout=TIMEOUT_WEB)
            data = resp.json()
            results = []
            for doc in data.get('response', {}).get('docs', []):
                results.append({
                    'title': doc.get('title', 'Senza titolo'),
                    'identifier': doc.get('identifier', ''),
                    'url': f"https://archive.org/details/{doc.get('identifier', '')}"
                })
            return results
        except Exception as e:
            print(f"Internet Archive error: {e}")
            return []

# ========== ROTTE PER RICERCA WEB, IMMAGINI, AUDIO ==========
db = Database()
ricerca_web = RicercaWebAvanzata()

@app.route('/cerca_web', methods=['POST'])
def cerca_web_route():
    data = request.get_json()
    query = data.get('query', '')
    if not query:
        return jsonify({'error': 'Query mancante'}), 400
    risultati = {
        'wikipedia': ricerca_web.wikipedia(query),
        'duckduckgo': ricerca_web.duckduckgo(query),
        'internet_archive': ricerca_web.internet_archive(query)
    }
    return jsonify(risultati)

@app.route('/analizza_immagine', methods=['POST'])
def analizza_immagine():
    """Placeholder per analisi immagini. Qui puoi integrare un servizio come Replicate o Google Vision."""
    data = request.get_json()
    image_base64 = data.get('image_base64', '')
    if not image_base64:
        return jsonify({'error': 'Nessuna immagine'}), 400
    # Per ora restituiamo una descrizione fittizia
    return jsonify({'description': 'Immagine ricevuta. L\'analisi visiva non è ancora attiva. Potrai integrare API esterne.'})

@app.route('/genera_immagine', methods=['POST'])
def genera_immagine():
    """Placeholder per generazione immagini (es. Replicate)."""
    data = request.get_json()
    prompt = data.get('prompt', '')
    if not prompt:
        return jsonify({'error': 'Prompt mancante'}), 400
    # Placeholder: restituisce un URL fittizio
    return jsonify({'image_url': 'https://placehold.co/600x400?text=Immagine+generata+placeholder'})

@app.route('/genera_audio', methods=['POST'])
def genera_audio():
    """Placeholder per generazione audio (es. ElevenLabs)."""
    data = request.get_json()
    testo = data.get('text', '')
    if not testo:
        return jsonify({'error': 'Testo mancante'}), 400
    # Placeholder: restituisce un URL audio di esempio
    return jsonify({'audio_url': 'https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3'})

@app.route('/stato', methods=['GET'])
def stato():
    return jsonify({
        "pagine": db.conteggio_pagine(),
        "dimensione_mb": round(db.dim_totale_mb(), 2)
    })

@app.route('/chat', methods=['POST'])
def chat():
    """Endpoint principale per il RAG: restituisce il contesto (database + Wikipedia live)."""
    data = request.get_json()
    domanda = data.get('message', '')
    if not domanda:
        return jsonify({'error': 'Messaggio vuoto'}), 400

    # Cerca nel database
    risultati_db = db.cerca(domanda, limit=2)
    contesto = ""
    if risultati_db:
        contesto = "Ecco informazioni dal mio database:\n\n"
        for titolo, contenuto, score in risultati_db:
            snippet = contenuto[:800] + "..." if len(contenuto) > 800 else contenuto
            contesto += f"Fonte: {titolo}\n{snippet}\n\n"
    else:
        # Fallback: cerca su Wikipedia live
        wiki_results = ricerca_web.wikipedia(domanda)
        if wiki_results:
            primo = wiki_results[0]
            # Scarica l'estratto completo della pagina (si può ottimizzare con API)
            url_api = f"https://it.wikipedia.org/w/api.php?action=query&titles={urllib.parse.quote(primo['title'])}&prop=extracts&exintro=1&explaintext=1&format=json"
            try:
                resp = requests.get(url_api, timeout=TIMEOUT_WEB)
                data = resp.json()
                pages = data.get('query', {}).get('pages', {})
                for page in pages.values():
                    estratto = page.get('extract', '').strip()
                    if estratto:
                        contesto = f"Informazione da Wikipedia (appena recuperata):\n{page['title']}\n{estratto[:1000]}\n\n"
                        db.aggiungi_pagina(page['title'], f"https://it.wikipedia.org/wiki/{urllib.parse.quote(page['title'])}", estratto, "wikipedia_live")
                        break
            except:
                pass
        if not contesto:
            contesto = "Non ho trovato informazioni specifiche nel database né su Wikipedia.\n\n"

    return jsonify({'response': contesto})

@app.route('/')
def home():
    return jsonify({"status": "Visione backend attivo", "version": "18.0"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
