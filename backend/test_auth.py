import requests

def test_models():
    # If we have an API key, let's see available models
    import os
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("No API key")
        return
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    resp = requests.get(url)
    models = resp.json().get('models', [])
    for m in models:
        print(m['name'])

test_models()
