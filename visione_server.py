import os
import re
import sqlite3
import urllib.parse
import json
import collections
import math
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime

# Se requests non è installato (ma su Render lo sarà tramite requirements.txt)
try:
    import requests
except ImportError:
    import subprocess
    subprocess.run(["pip", "install", "requests"])
    import requests

# ========== CONFIGURAZIONE AVANZATA ==========
DB_FILE = "visione_conoscenza.db"
TIMEOUT_WEB = 12
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
MAX_CRONOLOGIA = 10  # Numero di messaggi passati da ricordare per il contesto

# ========== ARCHITETTURA DATABASE POTENZIATA ==========
class DatabaseAvanzato:
    def __init__(self):
        self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self.cursore = self.conn.cursor()
        self._init_db()

    def _init_db(self):
        # Tabella Core per le pagine indicizzate
        self.cursore.execute('''
            CREATE TABLE IF NOT EXISTS pagine (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                titolo TEXT UNIQUE,
                url TEXT,
                contenuto TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                fonte TEXT,
                tag TEXT
            )
        ''')
        # Tabella per la cronologia della chat (Contesto conversazionale)
        self.cursore.execute('''
            CREATE TABLE IF NOT EXISTS cronologia (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                ruolo TEXT,
                messaggio TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Tabella di configurazione e metadati di sistema
        self.cursore.execute('''
            CREATE TABLE IF NOT EXISTS impostazioni (
                chiave TEXT PRIMARY KEY,
                valore TEXT
            )
        ''')
        # Tabella Virtuale FTS5 per la ricerca testuale veloce
        try:
            self.cursore.execute('''
                CREATE VIRTUAL TABLE IF NOT EXISTS pagine_fts USING fts5(
                    titolo, contenuto, content=pagine
                )
            ''')
        except sqlite3.OperationalError:
            print("FTS5 non supportato o già configurato.")
            
        # Inserimento versione di default se non presente
        self.cursore.execute("INSERT OR IGNORE INTO impostazioni (chiave, valore) VALUES ('versione_db', '18.5')")
        self.conn.commit()

    def aggiungi_pagina(self, titolo, url, contenuto, fonte, tag="generale"):
        if not titolo or not contenuto:
            return False
        if self.pagina_esiste(titolo):
            return False
        try:
            self.cursore.execute(
                "INSERT INTO pagine (titolo, url, contenuto, fonte, tag) VALUES (?, ?, ?, ?, ?)",
                (titolo, url, contenuto, fonte, tag)
            )
            # Aggiornamento indice FTS5
            try:
                self.cursore.execute(
                    "INSERT INTO pagine_fts(titolo, contenuto) VALUES (?, ?)",
                    (titolo, contenuto)
                )
            except:
                pass
            self.conn.commit()
            return True
        except Exception as e:
            print(f"[DB ERR] Impossibile aggiungere pagina: {e}")
            return False

    def pagina_esiste(self, titolo):
        self.cursore.execute("SELECT 1 FROM pagine WHERE LOWER(titolo) = ?", (titolo.lower().strip(),))
        return self.cursore.fetchone() is not None

    def salva_messaggio(self, session_id, ruolo, messaggio):
        try:
            self.cursore.execute(
                "INSERT INTO cronologia (session_id, ruolo, messaggio) VALUES (?, ?, ?)",
                (session_id, ruolo, messaggio)
            )
            self.conn.commit()
            # Mantieni pulita la cronologia rimuovendo i record vecchi oltre il limite
            self.cursore.execute(
                "DELETE FROM cronologia WHERE id NOT IN (SELECT id FROM cronologia WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?)",
                (session_id, MAX_CRONOLOGIA)
            )
            self.conn.commit()
        except Exception as e:
            print(f"[DB ERR] Errore salvataggio cronologia: {e}")

    def recupera_contesto_conversazione(self, session_id):
        try:
            self.cursore.execute(
                "SELECT ruolo, messaggio FROM cronologia WHERE session_id = ? ORDER BY timestamp ASC",
                (session_id,)
            )
            righe = self.cursore.fetchall()
            contesto_str = ""
            for r in righe:
                contesto_str += f"{r[0].upper()}: {r[1]}\n"
            return contesto_str
        except:
            return ""

    def svuota_cronologia(self, session_id):
        try:
            self.cursore.execute("DELETE FROM cronologia WHERE session_id = ?", (session_id,))
            self.conn.commit()
            return True
        except:
            return False

    def algoritmo_ricerca_ibrida(self, query, limit=3):
        """Combina ricerca esatta, FTS5 e un fallback TF-IDF algoritmico in Python"""
        q = query.lower().strip()
        risultati_finali = []
        visti = set()

        # 1. Match Esatto sul Titolo (Priorità Assoluta)
        self.cursore.execute("SELECT titolo, contenuto, url, fonte FROM pagine WHERE LOWER(titolo) = ?", (q,))
        riga = self.cursore.fetchone()
        if riga:
            visti.add(riga[0])
            risultati_finali.append({"titolo": riga[0], "contenuto": riga[1], "url": riga[2], "fonte": riga[3], "score": 1.5})

        # 2. Match Parziale (LIKE)
        self.cursore.execute("SELECT titolo, contenuto, url, fonte FROM pagine WHERE LOWER(titolo) LIKE ? LIMIT 5", (f"%{q}%",))
        for r in self.cursore.fetchall():
            if r[0] not in visti:
                visti.add(r[0])
                risultati_finali.append({"titolo": r[0], "contenuto": r[1], "url": r[2], "fonte": r[3], "score": 1.0})

        # 3. Match FTS5 Avanzato
        try:
            query_pulita = re.sub(r'[^\w\s]', '', q)
            if query_pulita:
                self.cursore.execute(
                    "SELECT titolo, contenuto, url, fonte, rank FROM pagine INNER JOIN pagine_fts ON pagine.titolo = pagine_fts.titolo WHERE pagine_fts MATCH ? LIMIT 10",
                    (query_pulita,)
                )
                for r in self.cursore.fetchall():
                    if r[0] not in visti:
                        visti.add(r[0])
                        score_calcolato = max(0.1, 1.0 - (r[4] / 100.0))
                        risultati_finali.append({"titolo": r[0], "contenuto": r[1], "url": r[2], "fonte": r[3], "score": score_calcolato})
        except Exception as e:
            print(f"[RICERCA] Errore FTS5, passo al motore TF-IDF manuale: {e}")

        # 4. Fallback TF-IDF Algoritmico Software in Python Puro (Se abbiamo pochi risultati)
        if len(risultati_finali) < limit:
            self.cursore.execute("SELECT titolo, contenuto, url, fonte FROM pagine")
            tutte_pagine = self.cursore.fetchall()
            parole_chiave = [w for w in q.split() if len(w) > 2]
            
            if parole_chiave and tutte_pagine:
                punteggi_tfidf = []
                for tit, cont, url_p, fnt in tutte_pagine:
                    if tit in visti:
                        continue
                    testo_totale = (tit + " " + cont).lower()
                    score_documento = 0
                    for parola in parole_chiave:
                        conteggio = testo_totale.count(parola)
                        if conteggio > 0:
                            # Formula semplificata TF-IDF
                            score_documento += (conteggio / len(testo_totale.split())) * math.log(1.0 + len(tutte_pagine))
                    if score_documento > 0:
                        punteggi_tfidf.append(({"titolo": tit, "contenuto": cont, "url": url_p, "fonte": fnt, "score": score_documento}))
                
                punteggi_tfidf.sort(key=lambda x: x["score"], reverse=True)
                for item in punteggi_tfidf[:(limit - len(risultati_finali))]:
                    risultati_finali.append(item)

        # Ordinamento definitivo per punteggio di rilevanza rilevato
        risultati_finali.sort(key=lambda x: x["score"], reverse=True)
        return risultati_finali[:limit]

    def ottimizza_tabelle(self):
        """Rimuove i duplicati e riorganizza gli indici del database"""
        try:
            self.cursore.execute('''
                DELETE FROM pagine 
                WHERE id NOT IN (SELECT MIN(id) FROM pagine GROUP BY titolo)
            ''')
            self.cursore.execute("VACUUM")
            self.conn.commit()
            return True
        except Exception as e:
            print(f"[MANUTENZIONE] Errore durante il vacuum: {e}")
            return False

    def estrai_statistiche_avanzate(self):
        self.cursore.execute("SELECT COUNT(*), fonte FROM pagine GROUP BY fonte")
        fonti_distribuite = {riga[1]: riga[0] for riga in self.cursore.fetchall()}
        
        self.cursore.execute("SELECT COUNT(*) FROM cronologia")
        messaggi_totali = self.cursore.fetchone()[0]
        
        return {
            "pagine_totali": sum(fonti_distribuite.values()),
            "ripartizione_fonti": fonti_distribuite,
            "messaggi_loggati_cronologia": messaggi_totali,
            "dimensione_file_bytes": os.path.getsize(DB_FILE) if os.path.exists(DB_FILE) else 0
        }

# ========== CLASSIFICATORE INTENTI DI QUERY ==========
class AnalizzatoreQuery:
    @staticmethod
    def analizza(query):
        q = query.lower()
        # Euristiche di categorizzazione
        if any(w in q for w in ["crea", "codice", "python", "html", "javascript", "script", "funzione", "programma"]):
            return "CODICE_E_SVILUPPO"
        if any(w in q for w in ["chi è", "chi fu", "storia", "nato", "biografia"]):
            return "STORICO_BIOGRAFICO"
        if any(w in q for w in ["meteo", "ora", "oggi", "prezzo", "notizie", "news", "recenti"]):
            return "ATTUALITA_TEMPO_REALE"
        if any(w in q for w in ["perché", "come funziona", "spiega", "definizione"]):
            return "SPIEGAZIONE_CONCETTUALE"
        return "GENERALE"

# ========== MOTORE DI RICERCA INTERNET RESILIENTE ==========
class WebIntelligenceEngine:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': USER_AGENT})

    def estrai_da_wikipedia(self, query):
        if len(query) < 2:
            return None
        api_url = f"https://it.wikipedia.org/w/api.php?action=query&generator=search&gsrsearch={urllib.parse.quote(query)}&gsrlimit=2&prop=extracts&exchars=1200&exintro=1&explaintext=1&format=json"
        try:
            r = self.session.get(api_url, timeout=TIMEOUT_WEB)
            dati = r.json()
            pagine_mappa = dati.get("query", {}).get("pages", {})
            raccolta = []
            for p in pagine_mappa.values():
                if "missing" not in p and "extract" in p:
                    titolo = p["title"]
                    testo = p["extract"].strip()
                    link = f"https://it.wikipedia.org/wiki/{urllib.parse.quote(titolo)}"
                    if testo:
                        raccolta.append({"titolo": titolo, "testo": testo, "url": link})
            return raccolta if raccolta else None
        except:
            return None

    def estrai_da_duckduckgo(self, query):
        """Scraping avanzato tramite interfaccia HTML stabile senza Javascript"""
        endpoint = "https://html.duckduckgo.com/html/"
        payload = {'q': query}
        try:
            r = self.session.post(endpoint, data=payload, timeout=TIMEOUT_WEB)
            corpo_html = r.text
            
            frammenti = re.findall(r'<a class="result__snippet".*?>(.*?)</a>', corpo_html, re.DOTALL)
            titoli = re.findall(r'<a class="result__url".*?>(.*?)</a>', corpo_html, re.DOTALL)
            collegamenti = re.findall(r'<a class="result__url" href="(.*?)"', corpo_html, re.DOTALL)
            
            risultati = []
            for i in range(min(4, len(frammenti))):
                testo_pulito = re.sub('<[^<]+?>', '', frammenti[i]).strip()
                titolo_pulito = re.sub('<[^<]+?>', '', titoli[i]).strip() if i < len(titoli) else "Riscontro Web"
                link_pulito = collegamenti[i] if i < len(collegamenti) else ""
                
                # Decodifica url se reindirizzato da duckduckgo
                if "uddg=" in link_pulito:
                    link_pulito = urllib.parse.unquote(link_pulito.split("uddg=")[1].split("&")[0])
                
                if testo_pulito:
                    risultati.append({"titolo": titolo_pulito, "testo": testo_pulito, "url": link_pulito})
            return risultati if risultati else None
        except Exception as e:
            print(f"[SCRAPER] Fallimento critico DDG: {e}")
            return None

    def gratta_url_diretto(self, url):
        """Se l'utente inserisce un link, il server lo legge in tempo reale e ne estrae il testo"""
        try:
            r = self.session.get(url, timeout=TIMEOUT_WEB)
            html = r.text
            # Rimuove tag script, stile e markup HTML per pulire il testo
            html_pulito = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
            testo_estratto = re.sub(r'<[^<]+?>', ' ', html_pulito)
            testo_estratto = re.sub(r'\s+', ' ', testo_estratto).strip()
            
            # Estrae un titolo di riferimento
            match_titolo = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
            titolo = match_titolo.group(1).strip() if match_titolo else f"Contenuto Estratto: {datetime.now().strftime('%H%M%S')}"
            
            return {"titolo": titolo, "testo": testo_estratto[:4000], "url": url} # Limite di sicurezza a 4000 caratteri
        except Exception as e:
            return {"errore": f"Impossibile leggere l'URL: {str(e)}"}

# ========== MOTORE DI RAGIONAMENTO LOGICO STRUTTURATO ==========
class MotoreRagionamentoAvanzato:
    @staticmethod
    def compila_output(domanda, contesto_risorse, intent, cronologia_precedente=""):
        orario_attuale = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # --- GENERAZIONE DEL TAG <thinking> (Chain of Thought) ---
        pensiero = [
            "<thinking>",
            f"Analisi Ricezione Query: '{domanda}' alle ore {orario_attuale}.",
            f"Classificazione Intento Query: {intent}."
        ]
        
        if cronologia_precedente:
            pensiero.append("Rilevata cronologia attiva per la sessione. Collegamento dei vincoli conversazionali precedenti in corso.")
        
        if contesto_risorse.get("fonti"):
            fonti_utilizzate = ", ".join(contesto_risorse["fonti"])
            pensiero.append(f"Iniezione Contesto RAG Avvenuta con successo. Fonti abilitate: [{fonti_utilizzate}].")
            pensiero.append("Ponderazione delle informazioni estratte per prevenire sovrascritture mentali errate.")
        else:
            pensiero.append("Nessuna fonte esterna reperita. Attivazione logica deduttiva interna autonoma.")
            
        pensiero.append(f"Fase Finale: Generazione della risposta in formato strutturato Markdown.")
        pensiero.append("</thinking>\n\n")
        
        # --- STRUTTURAZIONE DELLA RISPOSTA FINALE ---
        risposta = f"## 🧠 VISIONE v18.5 • MOTORE DI RAGIONAMENTO\n\n"
        
        if intent == "CODICE_E_SVILUPPO":
            risposta += "💡 *Modalità Sviluppo Software Attiva. Ottimizzazione della sintassi in corso.*\n\n"
        
        risposta += contesto_risorse["corpo_testo"]
        
        if contesto_risorse.get("riferimenti_link"):
            risposta += "\n\n### 🔗 Fonti consultate ed Esaminate:\n"
            for link in contesto_risorse["riferimenti_link"]:
                risposta += f"- {link}\n"
                
        return "\n".join(pensiero) + risposta

# ========== FLASK APPLICATION SERVER ==========
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

db = DatabaseAvanzato()
web_search = WebIntelligenceEngine()

@app.route('/chat', methods=['POST'])
def chat():
    dati = request.get_json() or {}
    domanda = dati.get('message', '').strip()
    session_id = dati.get('session_id', 'default_user')
    
    if not domanda:
        return jsonify({'error': 'Il campo di testo message non può essere vuoto.'}), 400

    # 1. Analisi preliminare della query
    intento = AnalizzatoreQuery.analizza(domanda)
    cronologia_contesto = db.recupera_contesto_conversazione(session_id)
    
    contesto_risorse = {"corpo_testo": "", "fonti": [], "riferimenti_link": []}
    trovato = False

    # Controllo se l'utente ha passato un URL diretto nella domanda
    url_trovati = re.findall(r'(https?://\S+)', domanda)
    if url_trovati:
        url_target = url_trovati[0]
        risultato_scraping = web_search.gratta_url_diretto(url_target)
        if "errore" not in risultato_scraping:
            contesto_risorse["corpo_testo"] = f"📑 **Analisi Live del Link allegato ({risultato_scraping['titolo']}):**\n\n{risultato_scraping['testo'][:1200]}..."
            contesto_risorse["fonti"].append("url_diretto")
            contesto_risorse["riferimenti_link"].append(url_target)
            # Salvataggio immediato in cache interna
            db.aggiungi_pagina(risultato_scraping['titolo'], url_target, risultato_scraping['testo'], "scraped_link")
            trovato = True

    # 2. Ricerca Ibrida nel Database Locale (RAG)
    if not trovato:
        match_locali = db.algoritmo_ricerca_ibrida(domanda, limit=2)
        if match_locali:
            testo_accumulato = "📚 **Dati estratti dall'Archivio Interno di Visione:**\n\n"
            for m in match_locali:
                testo_accumulato += f"📌 **{m['titolo']}** *(Rilevanza Score: {round(m['score'], 3)})*\n> {m['contenuto'][:700]}...\n\n"
                if m['url']:
                    contesto_risorse["riferimenti_link"].append(m['url'])
            contesto_risorse["corpo_testo"] = testo_accumulato
            contesto_risorse["fonti"].append("database_locale")
            trovato = True

    # 3. Ricerca Live su Wikipedia
    if not trovato:
        wiki_res = web_search.estrai_da_wikipedia(domanda)
        if wiki_res:
            testo_accumulato = "🌐 **Riscontri Enciclopedici trovati su Wikipedia:**\n\n"
            for w in wiki_res:
                testo_accumulato += f"🔹 **{w['titolo']}**\n{w['testo']}\n\n"
                contesto_risorse["riferimenti_link"].append(w['url'])
                db.aggiungi_pagina(w['titolo'], w['url'], w['testo'], "wikipedia")
            contesto_risorse["corpo_testo"] = testo_accumulato
            contesto_risorse["fonti"].append("wikipedia_live")
            trovato = True

    # 4. Ricerca Globale tramite DuckDuckGo HTML Engine
    if not trovato:
        ddg_res = web_search.estrai_da_duckduckgo(domanda)
        if ddg_res:
            testo_accumulato = "🦆 **Index Ricerca Web (DuckDuckGo Live):**\n\n"
            for d in ddg_res:
                testo_accumulato += f"🔸 **{d['titolo']}**\n{d['testo']}\n\n"
                if d['url']:
                    contesto_risorse["riferimenti_link"].append(d['url'])
            contesto_risorse["corpo_testo"] = testo_accumulato
            contesto_risorse["fonti"].append("duckduckgo_live")
            
            # Salvataggio indicizzato per query futura
            db.aggiungi_pagina(f"Ricerca: {domanda[:40]}...", ddg_res[0]['url'] if ddg_res[0]['url'] else "", ddg_res[0]['testo'], "duckduckgo")
            trovato = True

    # Fallback se la rete e il database sono vuoti
    if not trovato:
        contesto_risorse["corpo_testo"] = "⚠️ **Nessuna risorsa informativa trovata.** Il sistema RAG e i motori Web non hanno restituito corrispondenze utili per la query formulata."

    # Compilazione tramite motore di ragionamento CoT
    risposta_finale = MotoreRagionamentoAvanzato.compila_output(domanda, contesto_risorse, intento, cronologia_contesto)
    
    # Salva lo scambio nella cronologia per mantenere il contesto dei messaggi
    db.salva_messaggio(session_id, "user", domanda)
    db.salva_messaggio(session_id, "assistant", risposta_finale)

    return jsonify({'response': risposta_finale, 'intento_rilevato': intento})

@app.route('/impara', methods=['POST'])
def impara_manuale():
    """Endpoint per forzare l'apprendimento manuale di un testo dall'interfaccia"""
    dati = request.get_json() or {}
    titolo = dati.get('titolo', '').strip()
    contenuto = dati.get('contenuto', '').strip()
    url = dati.get('url', '').strip()
    fonte = dati.get('fonte', 'inserimento_manuale').strip()
    
    if not titolo or not contenuto:
        return jsonify({'error': 'Campi titolo e contenuto obbligatori.'}), 400
        
    successo = db.aggiungi_pagina(titolo, url, contenuto, fonte)
    if successo:
        return jsonify({'status': 'success', 'message': f"Pagina '{titolo}' memorizzata con successo nell'indice RAG."})
    return jsonify({'status': 'error', 'message': 'Errore durante la scrittura. Titolo forse duplicato.'}), 409

@app.route('/cronologia', methods=['DELETE'])
def svuota_chat():
    session_id = request.args.get('session_id', 'default_user')
    if db.svuota_cronologia(session_id):
        return jsonify({'status': 'success', 'message': f'Cronologia per la sessione {session_id} azzerata.'})
    return jsonify({'status': 'error', 'message': 'Impossibile ripulire la cronologia.'}), 500

@app.route('/manutenzione', methods=['POST'])
def esegui_manutenzione():
    esito = db.ottimizza_tabelle()
    if esito:
        return jsonify({'status': 'success', 'message': 'Database ottimizzato tramite processo di VACUUM e rimozione duplicati.'})
    return jsonify({'status': 'error', 'message': 'Errore interno di manutenzione.'}), 500

@app.route('/stato', methods=['GET'])
def stato():
    statistiche = db.estrai_statistiche_avanzate()
    return jsonify({
        "software": "Visione Advanced Engine",
        "versione_sistema": "18.5-Pro",
        "ambiente_esecuzione": "Render Cloud Infrastructure",
        "database_statistiche": {
            "pagine_indicizzate": statistiche["pagine_totali"],
            "ripartizione_per_fonte": statistiche["ripartizione_fonti"],
            "record_cronologia_attivi": statistiche["messaggi_loggati_cronologia"],
            "peso_file_mb": round(statistiche["dimensione_file_bytes"] / (1024 * 1024), 3)
        },
        "timestamp_live": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

@app.route('/')
def home():
    return jsonify({
        "messaggio": "Visione Enterprise Core Engine v18.5 attivo.",
        "endpoints": ["/chat", "/stato", "/impara", "/cronologia", "/manutenzione"],
        "struttura": "RAG + TF-IDF ibrido + Web Scraper integrato"
    })

if __name__ == '__main__':
    # Configurazione porta flessibile per l'ambiente Render
    porta_render = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=porta_render, debug=False)
