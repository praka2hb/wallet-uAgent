 
import json
import os
from typing import Any
import requests
 
HEADERS = {
'Content-Type': 'application/json',
'Accept': 'application/json',
'Authorization': 'bearer sk_8dbdb6fe1b9042fe921cd631142fb5ee1ff6343f18b14513a883954bc63787c4'
}
 
URL = "https://api.asi1.ai/v1/chat/completions"
 
MODEL = "asi1-mini"
 
 
async def get_completion(context: str, prompt: str, max_tokens: int = 2000) -> str:
    """Call ASI1-mini LLM for summarization with configurable max_tokens."""
    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": context + " " + prompt}],
        "temperature": 0,
        "stream": False,
        "max_tokens": max_tokens
    })
    try:
        resp = requests.post(URL, headers=HEADERS, data=payload)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"LLM error: {e}")
        return "Error generating summary."