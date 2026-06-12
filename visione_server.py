import os
import re
import sqlite3
import urllib.parse
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime

# ========== CONFIGURAZIONE ==========
DB_FILE = "visione_conoscenza.db"
TIMEOUT_WEB = 10
USER_AGENT = "Visione/18.0 (RAG + DuckDuckGo)"

# ========== DATABASE ==========
class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self.cursore = self.conn.cursor()
        self._init_db()

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
        # Titolo esatto
        self.cursore.execute("SELECT titolo, contenuto FROM pagine WHERE LOWER(titolo) = ?", (q,))
        riga = self.cursore.fetchone()
        if riga:
            return [(riga[0], riga[1], 1.0)]
        # Titolo parziale
        self.cursore.execute("SELECT titolo, contenuto FROM pagine WHERE LOWER(titolo) LIKE ? LIMIT 5", (f"%{q}%",))
        risultati = [(t, c, 0.9) for t, c in self.cursore.fetchall()]
        if risultati:
            return risultati[:limit]
        # Full-text FTS5
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

# ========== RICERCA WEB ==========
class RicercaWeb:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': USER_AGENT})

    def wikipedia(self, query):
        if len(query) < 3:
            return None
        url_api = f"https://it.wikipedia.org/w/api.php?action=query&generator=search&gsrsearch={urllib.parse.quote(query)}&gsrlimit=1&prop=extracts&exchars=1500&exintro=1&explaintext=1&format=json"
        try:
            resp = self.session.get(url_api, timeout=TIMEOUT_WEB)
            data = resp.json()
            pages = data.get("query", {}).get("pages", {})
            for page in pages.values():
                if "missing" not in page:
                    titolo = page["title"]
                    estratto = page.get("extract", "").strip()
                    if estratto:
                        url = f"https://it.wikipedia.org/wiki/{urllib.parse.quote(titolo)}"
                        return estratto, titolo, url
        except Exception as e:
            print(f"Wikipedia error: {e}")
        return None

    def duckduckgo_lite(self, query):
        """Scraping di DuckDuckGo Lite (testuale, senza JS)"""
        url = f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote(query)}"
        try:
            resp = self.session.get(url, timeout=TIMEOUT_WEB)
            html = resp.text
            matches = re.findall(r'<p class="result-snippet">(.*?)</p>', html, re.DOTALL | re.IGNORECASE)
            if matches:
                snippets = []
                for m in matches[:3]:
                    clean = re.sub('<[^<]+?>', '', m).strip()
                    if clean:
                        snippets.append(clean)
                return "\n".join(snippets)
        except Exception as e:
            print(f"DuckDuckGo error: {e}")
        return None

# ========== FLASK APP ==========
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
db = Database()
ricerca = RicercaWeb()

@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()
    domanda = data.get('message', '')
    if not domanda:
        return jsonify({'error': 'Messaggio vuoto'}), 400

    contesto = ""
    # 1. Cerca nel database
    risultati_db = db.cerca(domanda, limit=2)
    if risultati_db:
        contesto = "📚 **Dal mio database:**\n\n"
        for titolo, contenuto, score in risultati_db:
            snippet = contenuto[:600] + "..." if len(contenuto) > 600 else contenuto
            contesto += f"**{titolo}**\n{snippet}\n\n"
    else:
        # 2. Cerca su Wikipedia
        wiki = ricerca.wikipedia(domanda)
        if wiki:
            estratto, titolo, url = wiki
            contesto = f"🌐 **Da Wikipedia:**\n\n**{titolo}**\n{estratto}\n\n"
            db.aggiungi_pagina(titolo, url, estratto, "wikipedia_live")
        else:
            # 3. Cerca su DuckDuckGo
            ddg = ricerca.duckduckgo_lite(domanda)
            if ddg:
                contesto = f"🦆 **Da DuckDuckGo:**\n\n{ddg}\n\n"
                db.aggiungi_pagina(f"Ricerca: {domanda[:50]}", "", ddg, "duckduckgo_live")
            else:
                contesto = "⚠️ Nessuna informazione trovata nel database né sul web.\n\n"
    return jsonify({'response': contesto})

@app.route('/stato', methods=['GET'])
def stato():
    return jsonify({
        "pagine": db.conteggio_pagine(),
        "dimensione_mb": round(db.dim_totale_mb(), 2),
        "stato": "attivo",
        "ultimo_aggiornamento": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

@app.route('/')
def home():
    return jsonify({"status": "Visione backend v18.0 attivo", "endpoints": ["/chat", "/stato"]})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
