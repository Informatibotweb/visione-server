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
USER_AGENT = "Visione/16.0 (RAG Web)"
STORIA = deque(maxlen=10)  # contesto conversazione

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

    def dim_totale_mb(self):
        try:
            return os.path.getsize(DB_FILE) / (1024*1024)
        except:
            return 0

    def conteggio_pagine(self):
        self.cursore.execute("SELECT COUNT(*) FROM pagine")
        return self.cursore.fetchone()[0]

    def chiudi(self):
        self.conn.close()

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
            print(f"⚠️ Wikipedia errore: {e}")
        return None

    def duckduckgo(self, query):
        url = f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote(query)}"
        try:
            resp = self.session.get(url, timeout=TIMEOUT_WEB)
            html = resp.text
            matches = re.findall(r'<p class="result-snippet">(.*?)</p>', html, re.DOTALL | re.IGNORECASE)
            snippets = [re.sub('<[^<]+?>', '', m).strip() for m in matches[:2]]
            if snippets:
                return "\n".join(snippets)
        except Exception as e:
            print(f"⚠️ DuckDuckGo errore: {e}")
        return None

# ========== INTENTI E RISPOSTE ==========
def classifica_intento(testo):
    testo = testo.lower().strip()
    if testo in ["ciao", "buongiorno", "buonasera", "salve", "ehi", "hey"]:
        return "saluto"
    if re.match(r"^(come stai|come va|tutto bene|che si dice)", testo):
        return "come_stai"
    if re.match(r"^(come ti chiami|chi sei|cosa sei|ti presento)", testo):
        return "identita"
    if re.match(r"^(che cos'è|cos'è|cosa vuol dire|definizione di|significato di|chi è|che cosa sono)", testo):
        return "definizione"
    if testo.startswith("/"):
        return "comando"
    return "domanda"

# ========== INIZIALIZZAZIONE GLOBALE ==========
db = Database()
ricerca = RicercaWeb()

def rispondi(domanda):
    global STORIA
    intento = classifica_intento(domanda)
    # Risposte immediate senza RAG
    if intento == "saluto":
        return "Ciao! Come posso aiutarti oggi?"
    if intento == "come_stai":
        return "Sto benissimo, grazie! Sono sempre operativa."
    if intento == "identita":
        return "Sono Visione, un'assistente IA con accesso a Wikipedia e a un database di conoscenza."
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

    # RAG: cerca nel DB
    risultati_db = db.cerca(domanda, limit=2)
    contesto_rag = ""
    if risultati_db:
        contesto_rag = "Ecco informazioni dal mio database:\n\n"
        for titolo, contenuto, score in risultati_db:
            snippet = contenuto[:800] + "..." if len(contenuto) > 800 else contenuto
            contesto_rag += f"Fonte: {titolo}\n{snippet}\n\n"
    else:
        # Ricerca live
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
            contesto_rag = "Non ho trovato informazioni utili né nel database né online.\n\n"

    # Costruisci risposta testuale (senza LLM, perché su cloud non c'è Ollama)
    # Possiamo generare una risposta semplice usando le informazioni trovate.
    if contesto_rag and not contesto_rag.startswith("Non ho trovato"):
        return f"{contesto_rag}\n\nIn base a queste informazioni, posso dirti che ho trovato dei dati rilevanti. Vuoi che approfondisca qualcosa?"
    else:
        return "Mi dispiace, non ho trovato informazioni sufficienti per rispondere alla tua domanda. Prova a riformulare o usa /cerca per cercare nel database."

# ========== FLASK APP ==========
app = Flask(__name__)
CORS(app)

@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()
    messaggio = data.get('message', '')
    if not messaggio:
        return jsonify({'error': 'Messaggio vuoto'}), 400
    risposta = rispondi(messaggio)
    return jsonify({'response': risposta})

@app.route('/')
def home():
    return jsonify({"status": "Visione backend attivo", "version": "16.0-web"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
