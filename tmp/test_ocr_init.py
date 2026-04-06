import os
import sys
from unittest.mock import MagicMock

# Add current directory to path
sys.path.append(os.getcwd())

from app.services.ocr import OCRService

def test_initialization():
    print("Testing OCRService Initialization...")
    try:
        service = OCRService()
        print(f"Picked Gemini Model: {service.gemini_model}")
        print(f"Picked Groq Model: {service.groq_model}")
        
        print("\nTesting Model Rotation...")
        old_gemini = service.gemini_model
        service._rotate_gemini_model()
        print(f"Gemini Rotated: {old_gemini} -> {service.gemini_model}")
        
        old_groq = service.groq_model
        service._rotate_groq_model()
        print(f"Groq Rotated: {old_groq} -> {service.groq_model}")
        
    except Exception as e:
        print(f"Initialization Error: {e}")

if __name__ == "__main__":
    test_initialization()
