
import sys
import os
from pathlib import Path

# Add project root to path
project_root = str(Path(__file__).parent.parent.parent)
if project_root not in sys.path:
    sys.path.append(project_root)

print("Imports start...")
try:
    from src.models.model_factory import model_factory
    print("ModelFactory imported.")
    model = model_factory.get_model("ollama")
    print(f"Model: {model}")
    if model:
        print("Testing model...")
        resp = model.generate_response("You are a helpful assistant.", "Say hello from Moon Dev")
        print(f"Response: {resp.content}")
except Exception as e:
    print(f"Error testing model: {e}")

print("Imports nice_funcs...")
try:
    from src import nice_funcs as n
    print("nice_funcs imported.")
except Exception as e:
    print(f"Error importing nice_funcs: {e}")

print("Done.")
