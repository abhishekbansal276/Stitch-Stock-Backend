#!/usr/bin/env python3
"""
prepare_railway_env.py
──────────────────────
Run this ONCE locally with your serviceAccountKey.json present.
It prints the exact value to paste into Railway's FIREBASE_SERVICE_ACCOUNT_JSON env var.

Usage:
    python3 prepare_railway_env.py
    python3 prepare_railway_env.py /path/to/serviceAccountKey.json
"""
import sys
import json
import pathlib

key_path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "serviceAccountKey.json")

if not key_path.exists():
    print(f"ERROR: {key_path} not found")
    sys.exit(1)

# Load and validate
raw = key_path.read_text(encoding="utf-8")
try:
    data = json.loads(raw)
except json.JSONDecodeError as e:
    print(f"ERROR: Invalid JSON — {e}")
    sys.exit(1)

# Verify private key is present
pk = data.get("private_key", "")
if "BEGIN PRIVATE KEY" not in pk:
    print("ERROR: private_key field missing or malformed")
    sys.exit(1)

# Re-serialize with compact separators and explicit \n handling
# json.dumps preserves \n as \\n which is exactly what Railway needs
compact = json.dumps(data, separators=(",", ":"))

print("\n" + "─" * 60)
print("Paste this as FIREBASE_SERVICE_ACCOUNT_JSON in Railway:")
print("─" * 60)
print(compact)
print("─" * 60)

# Verify round-trip
try:
    verify = json.loads(compact)
    pk2 = verify.get("private_key", "")
    # Simulate what clean_private_key does
    pk2_clean = pk2.replace("\\n", "\n")
    assert "BEGIN PRIVATE KEY" in pk2_clean
    print(f"\n✓ Round-trip OK")
    print(f"  project_id   : {verify.get('project_id')}")
    print(f"  client_email : {verify.get('client_email')}")
    print(f"  private_key  : {len(pk2_clean)} chars, valid structure")
except Exception as e:
    print(f"\n✗ Round-trip FAILED: {e}")