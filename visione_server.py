import os
import sqlite3
import json
import urllib.parse
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DB_FILE = "visione_conoscenza.db"
TIMEOUT_WEB = 8

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def cerca_in_db(query):
    conn = get_db()
    cursor = conn.cursor()
    q = query.lower().strip()
    # 1. Titolo esatto
    cursor.execute("SELECT titolo, contenuto FROM pagine WHERE LOWER(titolo) = ?", (q,))
    row = cursor.fetchone()
    if row:
        return row["titolo"], row["contenuto"]
    # 2. Titolo LIKE
    cursor.execute("SELECT titolo, contenuto FROM pagine WHERE LOWER(titolo) LIKE ? LIMIT 3", (f"%{q}%",))
    rows = cursor.fetchall()
    if rows:
        # restituisci il primo
        return rows[0]["titolo"], rows[0]["contenuto"]
    # 3. Full-text FTS5
    try:
        cursor.execute("SELECT titolo, contenuto FROM pagine_fts WHERE pagine_fts MATCH ? ORDER BY rank LIMIT 1", (q,))
        row = cursor.fetchone()
        if row:
            return row["titolo"], row["contenuto"]
    except:
        pass
    conn.close()
    return None, None

def cerca_wikipedia(query):
    if len(query) < 3:
        return None
    url = f"https://it.wikipedia.org/w/api.php?action=query&generator=search&gsrsearch={urllib.parse.quote(query)}&gsrlimit=1&prop=extracts&exchars=1500&exintro=1&explaintext=1&format=json"
    headers = {"User-Agent": "Visione/1.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT_WEB)
        data = resp.json()
        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            if "missing" not in page:
                titolo = page["title"]
                estratto = page.get("extract", "").strip()
                if estratto:
                    # Salva nel database per future ricerche
                    conn = get_db()
                    try:
                        conn.execute("INSERT OR IGNORE INTO pagine (titolo, url, contenuto, fonte) VALUES (?, ?, ?, ?)",
                                     (titolo, f"https://it.wikipedia.org/wiki/{urllib.parse.quote(titolo)}", estratto, "wikipedia_live"))
                        conn.commit()
                    except:
                        pass
                    conn.close()
                    return titolo, estratto
    except Exception as e:
        print("Wikipedia error:", e)
    return None, None

@app.route('/stato', methods=['GET'])
def stato():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM pagine")
    count = cursor.fetchone()[0]
    size_mb = os.path.getsize(DB_FILE) / (1024*1024) if os.path.exists(DB_FILE) else 0
    conn.close()
    return jsonify({"pagine": count, "dimensione_mb": round(size_mb, 2)})

@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()
    domanda = data.get('message', '')
    if not domanda:
        return jsonify({'response': 'Messaggio vuoto'}), 400

    # 1. Cerca nel database
    titolo_db, contenuto_db = cerca_in_db(domanda)
    if contenuto_db:
        snippet = contenuto_db[:800] + "..." if len(contenuto_db) > 800 else contenuto_db
        response = f"📚 **Trovato nel mio database**\n\n**{titolo_db}**\n{snippet}"
        return jsonify({'response': response})

    # 2. Cerca su Wikipedia live
    titolo_wiki, estratto = cerca_wikipedia(domanda)
    if estratto:
        snippet = estratto[:800] + "..." if len(estratto) > 800 else estratto
        response = f"🌐 **Da Wikipedia (appena acquisito)**\n\n**{titolo_wiki}**\n{snippet}"
        return jsonify({'response': response})

    # 3. Nessuna informazione
    return jsonify({'response': "Non ho trovato informazioni su questo argomento. Prova a riformulare."})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
