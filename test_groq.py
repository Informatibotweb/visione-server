import os
import requests

GROQ_API_KEY = "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"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

def test_groq():
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": "Ciao, rispondi in italiano"}],
        "temperature": 0.7,
        "max_tokens": 50
    }
    try:
        resp = requests.post(GROQ_URL, json=payload, headers=headers, timeout=30)
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            print("Risposta:", resp.json()["choices"][0]["message"]["content"])
        else:
            print("Errore:", resp.text)
    except Exception as e:
        print(f"Eccezione: {e}")

if __name__ == "__main__":
    test_groq()