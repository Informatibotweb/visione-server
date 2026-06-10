import os
import re
import sqlite3
import urllib.parse
from collections import deque
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from groq import Groq

# ========== CONFIGURAZIONE ==========
DB_FILE = "visione_conoscenza.db"
TIMEOUT_WEB = 10
USER_AGENT = "Visione/16.0 (RAG + Groq)"
STORIA = deque(maxlen=10)

# Chiave Groq (usa variabile ambiente per sicurezza)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")
if not GROQ_API_KEY:
    print("⚠️ GROQ_API_KEY non trovata. Verrà usato solo RAG senza LLM.")
    client_groq = None
else:
    client_groq = Groq(api_key=GROQ_API_KEY)

# ========== DATABASE ==========
class Database:
    # ... (invariato, come prima, con i metodi: __init__, _init_db, aggiungi_pagina, pagina_esiste, cerca, dim_totale_mb, conteggio_pagine, chiudi)
    # Per brevità riporto solo i metodi essenziali; copia la classe dal codice precedente.
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
        try:
            self.cursore.execute("INSERT INTO pagine (titolo, url, contenuto, fonte) VALUES (?,?,?,?)",
                                 (titolo, url, contenuto, fonte))
            self.conn.commit()
            return True
        except:
            return False
    def pagina_esiste(self, titolo):
        self.cursore.execute("SELECT 1 FROM pagine WHERE titolo=?", (titolo,))
        return self.cursore.fetchone() is not None
    def cerca(self, query, limit=3):
        q = query.lower().strip()
        self.cursore.execute("SELECT titolo, contenuto FROM pagine WHERE LOWER(titolo)=?", (q,))
        r = self.cursore.fetchone()
        if r:
            return [(r[0], r[1], 1.0)]
        self.cursore.execute("SELECT titolo, contenuto FROM pagine WHERE LOWER(titolo) LIKE ? LIMIT 5", (f"%{q}%",))
        res = [(t, c, 0.9) for t, c in self.cursore.fetchall()]
        if res:
            return res[:limit]
        try:
            self.cursore.execute("SELECT titolo, contenuto, rank FROM pagine_fts WHERE pagine_fts MATCH ? LIMIT 10", (q,))
            cand = []
            for t, c, rank in self.cursore.fetchall():
                pert = max(0, 1 - rank/100.0)
                if len(q)<5 and len(c)>5000:
                    pert *= 0.3
                cand.append((pert, t, c))
            cand.sort(reverse=True, key=lambda x: x[0])
            return [(t, c, pert) for pert, t, c in cand[:limit]]
        except:
            return []
    def dim_totale_mb(self):
        try:
            return os.path.getsize(DB_FILE)/(1024*1024)
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
        if len(query)<3:
            return None
        url_api = f"https://it.wikipedia.org/w/api.php?action=query&generator=search&gsrsearch={urllib.parse.quote(query)}&gsrlimit=1&prop=extracts&exchars=1500&exintro=1&explaintext=1&format=json"
        try:
            resp = self.session.get(url_api, timeout=10)
            data = resp.json()
            pages = data.get("query",{}).get("pages",{})
            for page in pages.values():
                if "missing" not in page:
                    titolo = page["title"]
                    estratto = page.get("extract","").strip()
                    if estratto:
                        url = f"https://it.wikipedia.org/wiki/{urllib.parse.quote(titolo)}"
                        return estratto, titolo, url
        except:
            pass
        return None
    def duckduckgo(self, query):
        url = f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote(query)}"
        try:
            resp = self.session.get(url, timeout=10)
            html = resp.text
            matches = re.findall(r'<p class="result-snippet">(.*?)</p>', html, re.DOTALL|re.IGNORECASE)
            snippets = [re.sub('<[^<]+?>', '', m).strip() for m in matches[:2]]
            if snippets:
                return "\n".join(snippets)
        except:
            pass
        return None

# ========== INTENTI ==========
def classifica_intento(testo):
    testo = testo.lower().strip()
    if testo in ["ciao","buongiorno","buonasera","salve","ehi","hey"]:
        return "saluto"
    if re.match(r"^(come stai|come va|tutto bene|che si dice)", testo):
        return "come_stai"
    if re.match(r"^(come ti chiami|chi sei|cosa sei|ti presento)", testo):
        return "identita"
    if testo.startswith("/"):
        return "comando"
    return "domanda"

# ========== GENERAZIONE CON GROQ ==========
def genera_con_groq(prompt, max_tokens=300):
    if not client_groq:
        return None
    try:
        completion = client_groq.chat.completions.create(
            model="llama3-8b-8192",   # oppure "mixtral-8x7b-32768"
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=max_tokens,
            top_p=0.9
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        print(f"Groq error: {e}")
        return None

# ========== ISTANZE GLOBALI ==========
db = Database()
ricerca = RicercaWeb()

# ========== FUNZIONE RISPOSTA ==========
def rispondi(domanda):
    global STORIA
    intento = classifica_intento(domanda)
    # Risposte immediate
    if intento == "saluto":
        return "Ciao! Come posso aiutarti oggi?"
    if intento == "come_stai":
        return "Sto benissimo, grazie! Sempre operativa."
    if intento == "identita":
        return "Sono Visione, un'assistente IA con accesso a Wikipedia e un database di conoscenza. Uso Groq per generare risposte intelligenti."
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

    # RAG: cerca info nel DB o live
    risultati_db = db.cerca(domanda, limit=2)
    contesto = ""
    if risultati_db:
        for titolo, contenuto, score in risultati_db:
            contesto += f"Fonte: {titolo}\n{contenuto[:800]}...\n\n"
    else:
        # Ricerca live Wikipedia
        wiki = ricerca.wikipedia(domanda)
        if wiki:
            estratto, titolo, url = wiki
            contesto = f"Wikipedia: {titolo}\n{estratto}\n\n"
            db.aggiungi_pagina(titolo, url, estratto, "wikipedia_live")
        else:
            # Fallback DuckDuckGo
            ddg = ricerca.duckduckgo(domanda)
            if ddg:
                contesto = f"DuckDuckGo: {ddg}\n\n"
                db.aggiungi_pagina(f"Ricerca: {domanda[:50]}", "", ddg, "duckduckgo_live")

    # Costruisce prompt per Groq
    if contesto:
        prompt = f"""Sei Visione, un'assistente AI amichevole. Usa le seguenti informazioni per rispondere alla domanda dell'utente.

Informazioni:
{contesto}

Domanda: {domanda}

Risposta in italiano, chiara e completa (2-4 frasi). Se le informazioni non sono sufficienti, dì che non sai e suggerisci all'utente di cercare meglio."""
    else:
        prompt = f"""Sei Visione, un'assistente AI amichevole. L'utente chiede: {domanda}

Rispondi in italiano, in modo utile. Se non sai, ammetti di non sapere."""

    risposta_llm = genera_con_groq(prompt)
    if risposta_llm:
        return risposta_llm
    else:
        # Fallback senza LLM
        if contesto:
            return f"Ecco cosa ho trovato:\n{contesto}\n(Generazione automatica non disponibile, ma questi dati potrebbero aiutarti.)"
        else:
            return "Mi dispiace, non ho trovato informazioni sufficienti e la generazione automatica non è disponibile."

# ========== FLASK ==========
app = Flask(__name__)
CORS(app)

@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()
    msg = data.get('message', '')
    if not msg:
        return jsonify({'error': 'Messaggio vuoto'}), 400
    risp = rispondi(msg)
    return jsonify({'response': risp})

@app.route('/stato')
def stato():
    return {
        "pagine": db.conteggio_pagine(),
        "dimensione_mb": round(db.dim_totale_mb(), 2)
    }

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
