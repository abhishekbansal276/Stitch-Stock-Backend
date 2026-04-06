import os
import requests
import json
from dotenv import load_dotenv

load_dotenv('backend/.env')
api_key = os.getenv("GROQ_API_KEY")

url = "https://api.groq.com/openai/v1/models"
headers = {"Authorization": f"Bearer {api_key}"}

response = requests.get(url, headers=headers)
if response.status_code == 200:
    data = response.json()
    with open('backend/tmp/groq_models.json', 'w') as f:
        json.dump(data, f, indent=2)
    print("Models saved to backend/tmp/groq_models.json")
else:
    print(f"Error: {response.status_code}")
