import os
import re
import json
import sqlite3
import urllib.parse
from datetime import datetime
from collections import deque

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

# ========== CONFIGURAZIONE ==========
DB_FILE = os.environ.get("DB_FILE", "visione_conoscenza.db")
TIMEOUT_WEB = 10
USER_AGENT = "Visione/17.0 (RAG + Web Search)"
MAX_STUDY_GB = 5.0   # limite per la modalità studio

# Chiavi API (opzionali, da variabili d'ambiente)
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
REPLICATE_API_KEY = os.environ.get("REPLICATE_API_KEY", "")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")

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
        self.cursore.execute('''
            CREATE TABLE IF NOT EXISTS coda_studio (
                url TEXT PRIMARY KEY,
                titolo TEXT,
                fonte TEXT
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
        # 1) Titolo esatto
        self.cursore.execute("SELECT titolo, contenuto FROM pagine WHERE LOWER(titolo) = ?", (q,))
        riga = self.cursore.fetchone()
        if riga:
            return [(riga[0], riga[1], 1.0)]
        # 2) Titolo parziale
        self.cursore.execute("SELECT titolo, contenuto FROM pagine WHERE LOWER(titolo) LIKE ? LIMIT 5", (f"%{q}%",))
        risultati = [(t, c, 0.9) for t, c in self.cursore.fetchall()]
        if risultati:
            return risultati[:limit]
        # 3) Full-text FTS5
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

    def aggiungi_coda(self, titolo, fonte="wikipedia", url=None):
        if url is None:
            url = f"{fonte}:{titolo}"
        try:
            self.cursore.execute("INSERT OR IGNORE INTO coda_studio (url, titolo, fonte) VALUES (?, ?, ?)",
                                (url, titolo, fonte))
            self.conn.commit()
        except:
            pass

    def preleva_coda(self):
        self.cursore.execute("SELECT url, titolo, fonte FROM coda_studio LIMIT 1")
        riga = self.cursore.fetchone()
        if riga:
            self.cursore.execute("DELETE FROM coda_studio WHERE url = ?", (riga[0],))
            self.conn.commit()
            return riga
        return None

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
            print(f"Wikipedia error: {e}")
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
            print(f"DuckDuckGo error: {e}")
        return None

    def internet_archive(self, query):
        url = f"https://archive.org/advancedsearch.php?q={urllib.parse.quote(query)}&fl[]=title&fl[]=identifier&rows=3&page=1&output=json"
        try:
            resp = self.session.get(url, timeout=TIMEOUT_WEB)
            data = resp.json()
            docs = data.get("response", {}).get("docs", [])
            results = []
            for doc in docs:
                title = doc.get("title", "Senza titolo")
                identifier = doc.get("identifier", "")
                results.append(f"{title} (archive.org/details/{identifier})")
            return "\n".join(results)
        except Exception as e:
            print(f"Internet Archive error: {e}")
        return None

    def youtube(self, query):
        if not YOUTUBE_API_KEY:
            return None
        url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&maxResults=3&q={urllib.parse.quote(query)}&key={YOUTUBE_API_KEY}"
        try:
            resp = self.session.get(url, timeout=TIMEOUTIMEOUT_WEB)
_WEB)
            data = resp.json_WEB)
            data = resp.json            data = resp.json()
            items = data.get("items",()
            items = data.get("()
            items = data.get("items", [])
            results = [])
            results =items", [])
            results = []
            for item in items:
                title []
            for item in items:
                title []
            for item in items:
                title = item[" = item["snippet"]["title = item["snippet"]["titlesnippet"]["title"]
                video"]
                video_id = item[""]
                video_id = item["id"]["videoId_id = item["id"]["videoIdid"]["videoId"]
                results.append(f"{"]
                results.append(f"{title} - https"]
                results.append(f"{title} - https://youtu.be/{video_idtitle} - https://youtu.be/{video_id}")
            return://youtu.be/{video_id}")
            return}")
            return "\n".join(results "\n".join(results "\n".join(results)
        except Exception as e:
           )
        except Exception as e:
            print)
        except Exception as e:
            print print(f"You(f"YouTube error: {(f"YouTube error: {Tube error: {e}")
        returne}")
        returne}")
        return None

# ========== GENER None

# ========== GENER None

# ========== GENERAZIONE IMMAGINI/AAZIONE IMMAZIONE IMMAGINI/AUDIO (placeholder conUDIO (placeholder conAGINI/AUDIO (placeholder con API real API real API reali) =========i) ==========
def generai) ==========
def genera=
def genera_immagine(p_immagine(p_immagine(prompt):
    if not REPLICrompt):
    if not REPLICATE_API_KEYrompt):
    if not REPLICATE_API_KEY:
        return ":
        return "ATE_API_KEY:
        return "https://placehttps://placehold.co/600https://placehold.co/600hold.co/600x400?x400?text=Immx400?text=Immagine+text=Immagine+agine+placeholderplaceholder+set+APIplaceholder+set+set+API+key"
    #+API+key+key"
    # Es Esempio con Re"
    # Esempio con Reempio con Replicate (modplicate (modello flux-splicate (modello flux-sello flux-schnell)
   chnell)
   chnell)
    # # Richiede install # Rich Richiede installazioneazione: pipiede installazione: pip install replicate
    install replicate
    # import: pip install replicate
    # import replicate
    # # import replicate
    # output = replicate.run replicate
    # output = replicate.run output = replicate.run("black("black-forest-labs("black-forest-labs-forest-labs/flux-schn/flux-schnell", input/flux-schnell",ell", input={"prompt": prompt})
    # input={"prompt":={"prompt": prompt})
    # return output[0]
    return prompt})
    # return output[0]
    return "https://place return output[0]
    return "https://placehold.co/600 "https://placehold.co/600xx400?texthold.co/600x400?text400?text=Gener=Generazione+imm=Generazione+immazione+immagine+nonagine+nonagine+non+implement+implementata"

def genera+implementata"

def genera_audio(testo):
   ata"

def genera_audio(testo):
    if_audio(testo):
    if if not ELEVEN not ELEVENLABS_API_KEY not ELEVENLABS_API_KEY:
        return "LABS_API_KEY:
        return ":
        return "https://wwwhttps://www.soundhelixhttps://www.soundhelix.com/examples/mp3/Sound.soundhelix.com/ex.com/examples/mp3/SoundHelix-SongHelix-Songamples/mp3/SoundHelix-Song-1.mp3-1.mp3"
    # Es-1.mp3"
    # Esempio con ElevenLabs
   "
    # Esempioempio con ElevenLabs
    # url = f" con ElevenLabs
    # urlhttps://api.ele # url = f"https://api.elevenlabs = f"https://api.elevenlabs.io/v1/text-to.io/v1/text-tovenlabs.io/v1/text-to-speech/EX-speech/EX-speech/EXAVAVITQuAVITQuITQu4vr4vr4vrHEsHEsHEsDDDn7jQhn7jQhn7jQh"
    # headers"
    # headers"
    # headers = {"xi-api-key": E = {"xi-api-key": E = {"xi-api-key": ELEVENLABS_API_KEY}
   LEVENLABSLEVENLABS_API_KEY}
    # data = {"_API_KEY}
    # data # data = {"text": testotext": testo}
    # resp = = {"text": testo}
    # resp =}
    # resp = requests.post(url, json=data, headers requests.post(url, json=data, headers=headers)
    requests.post(url, json=data, headers=headers)
    # return # return resp.json()["=headers)
    # return resp resp.json()["audio_url"] ....json()["audio_url"] ...audio_url"] ... ma ma rest ma rest restituisce audioituisce audio binarioituisce audio binario.
    return " binario.
    return ".
    return "https://www.shttps://www.soundhelix.comhttps://www.soundhelix.com/examples/mpoundhelix.com/examples/mp3/SoundHel/examples/mp3/Sound3/SoundHelix-Song-ix-Song-1.mp3Helix-Song-1.mp3"

# =========="

# ========== ANAL1.mp3"

# ========== ANALISI IMM ANALISI IMMAGINE (placeholder)ISI IMMAGINE (placeholderAGINE (placeholder) ========= ==========
def analizza_) ==========
def analizza_=
def analizza_immagine(base64immagine(base64_stringimmagine(base64_string):
    # Qui_string):
    # Qui pot):
    # Qui pot potresti usareresti usare GPTresti usare GPT GPT-4V o un modello-4V o un modello-4V o un modello locale
    return " locale
    return locale
    return "Immagine rice "Immagine ricevuta, ma l'analisiImmagine ricevuta, mavuta, ma l'analisi visiva non è ancora l'analisi visiva non è ancora implementata. visiva non è ancora implementata. implementata. Des Descrivi manual Descrivi manualcrivi manualmente cosamente cosa vedmente cosa ved vedi."

# =i."

# ========== SCRi."

# ========== SCRAPER PER MOD========= SCRAPERAPER PERALIT PER MODALITÀ STUDIO = MODALITÀ STUDIO ==========
def scar=========
def scarica_paginaÀ STUDIO ==========
def scarica_pagina_wica_pagina_wikipedia(tit_wikipedia(titolo, dbikipedia(titolo, dbolo, db):
    if db):
    if db):
    if db.pagina_.pagina_.pagina_esiste(titesiste(titolo):
        returnesiste(titolo):
        returnolo):
        return False
    ricerca = Ricerca False
    ricerca = RicercaWeb()
    wiki False
    ricerca = RicercaWeb()
    wiki = ricerca.wikipediaWeb()
    wiki = ricerca.wikipedia = ricerca.wikipedia(titolo)
   (titolo)
   (titolo)
    if wiki:
        if wiki:
        if wiki:
        estrat estratto, titolo_reale, url = wiki estratto, titolo_reale, url = wiki
        db.aggito, titolo_reale, url = wiki
        db.aggi
        db.aggiungi_paginaungi_pagina(titolo_reungi_pagina(titolo_re(titolo_reale, url,ale, url,ale, url, estratto, " estratto, "wikipedia")
        estratto, "wikipedia")
       wikipedia")
        return True
    return True
    return False

def return True
    return False

def return False

def modalita_studio(db, seme=None modalita_studio(db, seme=None modalita_studio(db, seme=None):
    if seme:
        if):
    if seme:
        if):
    if seme:
        if seme.lower() == seme.lower() == "tutto seme.lower() == "tutto":
            seeds "tutto":
            seeds":
            seeds = ["HTML = ["HTML", "CSS", = ["HTML", "CSS",", "CSS", "JavaScript", "Python", "Program "JavaScript", "Python", "JavaScript", "Python", "Programmazione", "mazione", "Computer", "Int "Programmazione", "Computer", "Intelligenza artificialelligenza artificiale"]
           Computer", "Intelligenza artificiale"]
            for se"]
            for s for s in seeds:
                db. in seeds:
                in seeds:
                db.aggiungiaggiungi_coda(s, db.aggiungi_coda(s,_coda(s, "wikipedia "wikipedia")
        else:
            "wikipedia")
        else:
            db.aggiungi")
        else:
            db.aggiungi_coda(seme db.aggiungi_coda(seme_coda(seme, "wikipedia, "wikipedia")
    # Ver, "wikipedia")
    # Verifica coda")
    # Verifica coda
    db.cursore.execute("ifica coda
    db.cursore.execute("SELECT COUNT(*) FROM
    db.cursore.execute("SELECT COUNT(*) FROM cSELECT COUNT(*) FROM coda_studiooda_studio")
    if db coda_studio")
    if db")
    if db.cursore.fetch.cursore.fetchone()[0].cursore.fetchone()[0]one()[0] == 0 == 0:
        return == 0:
        return "⚠:
        return "⚠ "⚠️ Coda vuota. Us️ Coda vuota. Usa /studia️ Coda vuota. Usa /studia <argoma /studia <argomento> o /studento> o /studia tutto"
 <argomento> o /studia tutto"
    # Av    # Avia tutto"
    # Avvia studiovia studio
    dbvia studio
    db.dimensione
    db.dimensione_iniz_iniziale = db.dimensione_iniziale = dbiale = db.dim.dim_totale_m.dim_totale_mb_totale_mb() * b() * () * 1024 * 1024 * 1024
   1024 * 1024
    db.dimensione_aggiunta =1024
    db.dimensione db.dimensione_aggiunta = 0
    0
   _aggiunta = cont = 0
    cont = 0
    0
    cont = 0
    while not db while not db.limite while not db.limite_superato.limite_superato_superato():
        task = db.preleva_c():
():
        task = db.preleva_coda()
        if not task:
           oda()
        if not task:
                   task = db.preleva_coda()
        if not task:
            break
        url break
        url, titolo, break
        url, titolo,, titolo, fonte = task fonte = task fonte = task
        if db.pagina_es
        if db.p
        if db.pagina_esiste(titolo):
            continueagina_esiste(titoloiste(titolo):
            continue
        print(f"):
            continue
        print(f"
        print(f"📥 Scarico: {titolo📥 Scarico: {titolo}")
        if scar📥 Scarico: {titolo}")
        if scarica_pagina_wikipedia}")
        if scarica_pica_pagina_wikipedia(titolo, db):
            contagina_wikipedia(titolo,(titolo, db):
            cont += 1 db):
            cont += 1
        # += 1
        #
        # A Aggiunge Aggiunge link internggiunge link intern link interni? (i? (sempli? (semplsemplificato, non implementificato, non implementificato, non implementato)
        timeato)
        time.sleep(0ato)
        time.sleep(0.5)
   .sleep(0.5)
    return f"📚.5)
    return f" return f"📚 Studio completato:📚 Studio completato: Studio completato: {cont} pagine {cont} pag {cont} pagine scaricate. Lim scaricate. Limine scaricate. Limite {MAX_STite {ite {MAX_STUDY_GBUDY_GB} GBMAX_STUDY_GB} GB."

# ==========."

# ==========} GB."

# ========== FLASK APP FLASK APP ==========
app FLASK APP ==========
app = Flask(__name ==========
app = Flask(__name = Flask(__name__)
CORS(app, resources={__)
CORS(app, resources={__)
CORS(app, resources={rr"/*": {"origins":r"/*": {"origins":"/*": {"origins": "*"}})
db "*"}} "*"}})
db = Database = Database()
ricerca = Ric)
db = Database()
ricerca = RicercaWeb()

# ---------- Rot()
ricerca = RicercaWeb()

# ---------- RotercaWeb()

# ---------- Rottatata principale: principale: /chat principale: /chat /chat (R (RAG, senza gener (RAG, senza generazione) ----------
@app.routeAG, senza generazione) ----------
@app.route('/chatazione) ----------
@app.route('/chat', methods=['POST', methods=['POST'])
def chat('/chat', methods=['POST'])
def chat():
    data = request'])
def chat():
    data = request():
    data = request.get_json()
    domanda = data.get_json()
    domanda = data.get('message', '')
    if.get_json()
    domanda = data.get('message', '')
    if.get('message', not domanda not domanda '')
    if not domanda:
        return jsonify({'error': ':
        return jsonify({'error': ':
        return jsonify({'error': 'Messaggio vuotoMessaggio vuoto'}), 400Messaggio vuoto'}), 400

    # C'}), 400

    # Cerca nel database
    risultati_db =erca nel database


    # Cerca nel database
    risultati_db = db.cerca(domanda, limit    risultati_db = db.cerca( db.cerca(domanda, limit=2)
    contesto ==2)
   domanda, limit=2)
    contesto = ""
    if ""
    if risultati_db:
        contest contesto = ""
    if risultati_db:
        contesto = "E risultati_db:
        contesto = "Ecco informazioni dalo = "Ecco informazioni dalcco informazioni dal mio database:\n mio database:\n\n"
        for mio database:\n\n\n"
        for titolo, titolo, contenuto, score in"
        for titolo, contenuto, score in risultati_db:
            snippet = contenuto, score in risultati_db:
            snippet = contenuto risultati_db:
            snippet = contenuto[:1000] contenuto[:1000] + "..." if + "..." if len(conten[:1000] + "..." if len(contenuto) >  len(contenuto) > 1000 else contenuto) > 1000 else conten1000 else contenuto
            contesto += f"uto
            contesto += f"uto
            contesto += f"Fonte: {titolo}\nFonte: {titolo}\nFonte: {titolo}\n{snippet}\n{snippet}\n\n"
   {snippet}\n\n"
    else:
        # Ric\n"
    else:
        # Ric else:
        # Ricerca live Wikipedia
        wiki =erca live Wikipedia
        wiki =erca live Wikipedia
        wiki = ricerca.wikipedia ricerca.wikipedia( ricerca.wikipedia(domanda)
       (domanda)
       domanda)
        if wiki:
            estratto, tit if wiki:
            estratto, tit if wiki:
            estratto, titolo, url = wiki
            contestolo, url = wiki
            contestolo, url = wiki
           o = f"o = f"Informazione da Wikipedia (appena recuper contesto = f"Informazione da Wikipedia (appena recuperInformazione da Wikipedia (appena recuperata):\n{titolo}\ata):\n{titolo}\ata):\n{titolon{estratto}\n\nn{estratto}\n\n"
            db.aggi}\n{estratto}\n\n"
            db."
            db.aggiungi_pagina(titoloaggiungi_pagina(titoloungi_pagina(titolo, url, estratto, "w, url, estrat, url, estratto, "wikipedia_liveto, "wikipedia_liveikipedia_live")
        else:
            contest")
        else:
            contest")
        else:
            contesto = "Non ho trovato informazionio = "Non ho trovato informazionio = "Non ho utili.\n\n"
    utili.\n\n"
 trovato informazioni utili.\ return jsonify({'    return jsonify({'n\n"
    return jsonify({'response': contesto})

response': contesto})

# ---------response': contesto})

# ---------# ---------- Rotta- Rotta /stato ---------- Rotta /stato ----------
@app.route('/-
@app.route('/stato', /stato ----------
@app.route('/stato', methods=[' methods=['GET'])
defstato', methods=['GET'])
defGET'])
def stato():
    return stato():
    return jsonify({
        stato():
    return jsonify jsonify({
        "pagine": "pagine": db.con({
        "pagine": db.conteggio_pagine db.conteggio_pagine(),
        "dteggio_pagine(),
        "d(),
        "dimensione_mbimensione_mb": roundimensione_mb": round": round(db.dim_t(db.dim_totale_mb(db.dim_totale_mb(), 2),
        "connessotale_mb(), 2(), 2),
        "connesso": True),
        "connesso": Trueo": True,
        "l,
        "latenza_ms,
        "latenza_ms": 50atenza_ms": 50": 50,
        "veloc,
        "velocita_gbps":,
        "velocita_gbps":ita_gbps": 10,
        "s 10,
        "sistema": 10,
        "sistema":istema": "Linux ( "Linux (ultima vers "Linux (ultima versione)",
        "ram_gultima versione)",
        "ram_gb": 16,
       ione)",
        "ram_gb":b": 16,
        "db 16,
        "db "db_att_attivo": True_attivo": True,
        "ult,
        "ultimo_aggiorivo": True,
        "ultimo_aggiornamento": datetimenamento": datetimeimo_aggiornamento": datetime.now().strftime.now().strftime.now().strftime("%Y-%m("%Y-%m-%d %H("%Y-%m-%d %H-%d %H:%M:%S:%M:%S")
    })

#:%M:%S")
    })

#")
    })

# ---------- Rot ---------- Rotta / ---------- Rotta /cercata /cerca_web (riccerca_web (ricerca multi-fonte) ---------_web (ricerca multi-fonte) ---------erca multi-fonte) ----------
@app.route('/cerca_web',-
@app.route('/cerca_web',-
@app.route('/c methods=['POST'])
def cerca_web methods=['POST'])
def cerca_weberca_web', methods=['POST'])
def cerca_web():
    data =():
    data = request.get_json():
    data = request.get_json request.get_json()
    query =()
    query = data()
    query = data data.get('query',.get('query', '')
    if.get('query', '')
    if not query:
        return jsonify({' '')
    if not query:
        not query:
        return jsonify({'error': 'No query'}), 400 return jsonify({'error': 'No query'}), error': 'No query'}), 400
    risultati = {}
    # Wikipedia400
    risultati = {}
    # Wikipedia
    wiki =
    risultati = {}
    # Wikipedia
    wiki = ricerca.wikipedia ricerca.wikipedia(query
    wiki = ricerca.wikipedia(query)
    if wiki(query)
    if wiki)
    if wiki:
        risultati[':
        risultati['wikipedia'] =:
        risultati['wikipedia'] = wiki[0wikipedia'] = wiki wiki[0][:300[0][:300] + "..."
    #][:300] + "..."
    # DuckDuckGo] + "..."
    # DuckDuckGo
    ddg DuckDuckGo
    ddg = ricerca.duckduckgo
    ddg = ricerca.du = ricerca.duckduckgo(query)
    if ddg:
       (query)
    ifckduckgo(query)
    if ddg:
        risultati['duck risultati['duck ddg:
        risultati['duckduckgo'] = ddgduckgo'] = ddg[:300duckgo'] = ddg[:300]
    # Internet[:300]
]
    # Internet Archive
    ia = ricerca.internet_archive Archive
    ia = ricerca.internet_archive    # Internet Archive
    ia = ricerca.internet_archive(query)
    if ia:
        risultati(query)
    if ia:
        risultati(query)
    if ia:
        risultati['internet_['internet_archive'] = ia['internet_archive'] = ia[:archive'] = ia[:[:500]
    # YouTube
    y500]
    # YouTube
    y500]
    # YouTube
    yt = ricerca.youtubet = ricerca.youtube(query)
    ift = ricerca.youtube(query)
    if yt:
       (query)
    if yt:
        yt:
        risultati['youtube risultati['youtube'] = yt risultati['youtube'] = yt'] = yt
    return jsonify
   
    return jsonify(risultati)

# --------- return jsonify(risultati(risultati)

# ---------- Rotta /analizza_- Rotta)

# ---------- Rotta /analizza_immagine ---------immagine ----------
@app.route('/anal /analizza_immagine ----------
@app.route('/analizza_immagine', methods=['-
@app.route('/analizza_immagine', methods=['POST'])
def analizzaizza_immagine', methods=['POSTPOST'])
def analizza_immagine_route():
    data'])
def analizza_immagine_route():
    data_immagine_route():
    data = request.get_json()
    image_base = request.get_json = request.get_json()
    image_base64 = data.get('image_base64()
    image_base64 = data.get('image_base64', '')
   64 = data.get('image_base64', '')
    if not image_base', '')
    if not image_base64:
        return if not image_base64:
        return jsonify({'error64:
        return jsonify({'error': 'No jsonify({'error': 'No': 'No image'}), 400
    descrizione = anal image'}), 400
    descrizione = analizza_immagine image'}), 400
    descrizione = analizza_immagineizza_immagine(image_base64(image_base64(image_base64)
    return jsonify)
    return jsonify({'description': descrizione})

# ---------)
    return jsonify({'description': descrizione})

# ---------- Rotta /({'description': descrizione})

# ---------- Rotta /- Rotta /genera_genera_immaginegenera_immagine ----------
@appimmagine ----------
@app.route('/genera_immagine', ----------
@app.route('/genera_immagine', methods=['POST.route('/genera_immagine', methods=['POST methods=['POST'])
def genera_imm'])
def genera_immagine_'])
def genera_immagine_routeagine_route():
    data = request.get_json()
   route():
    data = request.get_json():
    data = request.get_json()
    prompt = data.get('prompt', prompt = data.get('prompt',()
    prompt = data.get('prompt', '')
    if '')
    if not prompt:
        return jsonify({' '')
    if not prompt:
        return jsonify({'error': 'No prompt'}), 400
    image_url = genera_ not prompt:
        return jsonify({'error': 'No prompt'}), 400
    image_url = genera_immagine(prompterror': 'No prompt'}), 400
    image_url = genera_immagine(promptimmagine(prompt)
    return jsonify({'image_url)
    return json)
    return jsonify({'image_url': image_url})

# ---------- Rotify({'image_url': image_url})

# ---------- Rotta /gener': image_url})

# ---------- Rotta /genera_audio ---------ta /genera_audio ----------
@app.route('/genera_audio ----------
@app.route('/genera_audio',-
@app.route('/genera_audio', methods=['POSTa_audio', methods=['POST'])
def genera_audio_route():
    data = request.get methods=['POST'])
def genera_audio_route():
   '])
def genera_audio_route():
    data = request.get_json()
    testo_json()
    testo = data.get(' data = request.get_json()
    testo = data.get('text', ' = data.get('text', '')
    if not testotext', '')
    if not testo:
        return json')
    if not testo:
        return jsonify({'error'::
        return jsonify({'error': 'No text'}), 400ify({'error': 'No text'}), 400
    audio_url = 'No text'}), 400
    audio_url = genera_audio(test
    audio_url = genera_audio(testo)
    return genera_audio(testo)
    return jsonify({'audio_url': audio_url})

# ----------o)
    return jsonify({'audio_url': audio_url})

# ---------- Rotta / jsonify({'audio_url': audio_url})

# ---------- Rotta /studia (modalstudia (modalità studio) --------- Rotta /studia (modalità studio) ---------ità studio) ----------
@app.route('/studia', methods-
@app.route('/studia', methods=['POST'])
def-
@app.route('/studia', methods=['POST'])
def=['POST'])
def studia():
    studia():
    data = request.get studia():
    data = request.get_json()
    seme = data.get(' data = request.get_json()
    seme = data.get('_json()
    seme = data.get('argomento',argomento',argomento', None None)
    msg = modalita_studio(db None)
    msg = modalita_studio(db)
    msg = modal, seme)
   , seme)
   ita_studio(db, seme)
    return jsonify({'status': msg return jsonify({'status': msg})

# ---------- return jsonify({'status': msg})

# ----------})

# ---------- Rotta /studia_tutto --------- Rotta /studia_tutto --------- Rotta /studia_tutto ----------
@app.route('/studia_tutto', methods=['POST-
@app.route('/studia_tutto', methods=['POST-
@app.route('/studia_tutto', methods=['POST'])
def studia_tutto():
   '])
def studia_tutto():
   '])
def studia_tutto():
    msg = modalita_studio(db, msg = modalita_studio(db, seme msg = modalita_studio(db, seme seme="tutto")
    return jsonify({'status': msg})

# ---------="tutto")
    return jsonify({'status': msg="tutto")
    return jsonify({'status': msg})

# ----------- Rot})

# ---------- Rot Rotta home ---------ta home ---------ta home ----------
@app.route('/')
def home():
    return jsonify-
@app.route('/')
def home():
    return jsonify-
@app.route('/')
def home():
    return jsonify({
        "status": "Vision({
        "status": "Visione backend({
        "status": "Visione backende backend v v17.0 attivo",
        " v17.0 att17.0 attivo",
        "endpoints": ["/chat", "/stato", "/cerca_webivo",
        "endpoints": ["/chat", "/endpoints": ["/chat", "/stato", "/", "/analizza_immaginestato", "/cerca_web", "/analizza_immagine",cerca_web", "/analizza_immagine", "/genera_", "/genera_immagine", "/genera_audio "/genera_immagine", "/genera_audio", "/studiaimmagine", "/genera_audio", "/studia", "/studia", "/studia_tutto"]
   ", "/studia_tutto"]
    })

if __name", "/studia_tutto"]
    })

if __name__ == '__main })

if __name__ == '__main__':
    port =__ == '__main__':
    port = int(os.environ.get('PORT',__':
    port = int(os.environ.get('PORT', int(os.environ.get('PORT', 5000))
    # Not 5000))
    # Nota: per produzione usare gunicorn 5000))
    # Nota: per produzione usare gunicorn, non ila: per produzione usare gunicorn, non il, non il server server di sviluppo
    app server di sviluppo
    app di sviluppo
    app.run(host='.run(host='0.0.0.0',.run(host='0.0.0.0', port=port, debug=False)
0.0.0.0', port=port, debug=False)
 port=port, debug=False)
