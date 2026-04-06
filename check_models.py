import os
from google import genai
from dotenv import load_dotenv

load_dotenv()

def list_gemini_models():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY not found in .env")
        return

    client = genai.Client(api_key=api_key)
    print("--- Available Gemini Models ---")
    try:
        models = client.models.list()
        for m in models:
            print(f"Name: {m.name}, Supported: {m.supported_generation_methods}")
    except Exception as e:
        print(f"Error listing models: {e}")

if __name__ == "__main__":
    list_gemini_models()
