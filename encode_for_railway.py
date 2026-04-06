#!/usr/bin/env python3
"""
Run this locally with serviceAccountKey.json in the same folder.
Prints the exact one-line value to paste into Railway.
"""
import json, pathlib, sys

path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "serviceAccountKey.json")
data = json.loads(path.read_text())

# Compact single-line JSON — Railway-safe
output = json.dumps(data, separators=(",", ":"))

print("\nPaste this into Railway → FIREBASE_SERVICE_ACCOUNT_JSON:\n")
print(output)
print()

# Verify
check = json.loads(output)
pk = check["private_key"].replace("\\n", "\n")
assert "BEGIN PRIVATE KEY" in pk
print(f"✓ Valid  |  project: {check['project_id']}  |  email: {check['client_email']}")