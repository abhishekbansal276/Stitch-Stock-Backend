#!/usr/bin/env python3
"""
diagnose_jwt.py
───────────────
Run this ON YOUR RAILWAY SERVER via railway run, or locally.
It pinpoints exactly why invalid_grant is happening.

Usage:
    # Locally (with same env vars):
    python3 diagnose_jwt.py

    # On Railway shell:
    railway run python3 diagnose_jwt.py
"""
import os, json, time, datetime, sys

from app.services.firebase import BASE_DIR

print("=" * 60)
print("JWT SIGNATURE DIAGNOSTIC")
print("=" * 60)

# ── 1. Load the JSON or File ──────────────────────────────────
sa_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
sa_file = BASE_DIR / os.getenv("SERVICE_ACCOUNT_KEY", "serviceAccountKey.json")
cred = None

if sa_json:
    try:
        cred = json.loads(sa_json)
        print(f"✓  JSON parsed from Environment Variable OK")
    except json.JSONDecodeError as e:
        print(f"❌ JSON parse from ENV failed: {e}")

if not cred and sa_file.exists():
    try:
        cred = json.loads(sa_file.read_text())
        print(f"✓  JSON parsed from Physical File OK: {sa_file}")
    except Exception as e:
        print(f"❌ JSON parse from File failed: {e}")

if not cred:
    print("❌ NO SERVICE ACCOUNT FOUND (Check your env or file)")
    sys.exit(1)

# ── 2. Check key fields ───────────────────────────────────────
print(f"\n── Service Account Info ──")
print(f"   project_id     : {cred.get('project_id', 'MISSING')}")
print(f"   client_email   : {cred.get('client_email', 'MISSING')}")
print(f"   client_id      : {cred.get('client_id', 'MISSING')}")
print(f"   type           : {cred.get('type', 'MISSING')}")

pk = cred.get("private_key", "")
pk_normalized = pk.replace("\\n", "\n")
print(f"\n── Private Key ──")
print(f"   raw length     : {len(pk)} chars")
print(f"   normalized len : {len(pk_normalized)} chars")
print(f"   has BEGIN      : {'YES' if 'BEGIN PRIVATE KEY' in pk_normalized else 'NO ❌'}")
print(f"   has END        : {'YES' if 'END PRIVATE KEY' in pk_normalized else 'NO ❌'}")

# Count actual newlines vs escaped
real_newlines = pk_normalized.count("\n")
escaped_newlines = pk.count("\\n")
print(f"   real \\n count  : {real_newlines}")
print(f"   escaped \\\\n   : {escaped_newlines}")

if escaped_newlines > 0 and real_newlines <= 2:
    print("   ⚠️  Key has escaped \\n but no real newlines — clean_private_key should fix this")
elif real_newlines > 10:
    print("   ✓  Key has proper newlines")

# ── 3. Try to sign something with the key ────────────────────
print(f"\n── Signing Test ──")
try:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    pk_bytes = pk_normalized.encode("utf-8")
    private_key = load_pem_private_key(pk_bytes, password=None)
    print("   ✓  Private key loads and parses correctly")
    
    # Try signing
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    test_data = b"test"
    signature = private_key.sign(test_data, padding.PKCS1v15(), hashes.SHA256())
    print(f"   ✓  Signing works (sig length: {len(signature)} bytes)")
except ImportError:
    print("   ℹ️  cryptography not installed, skipping key parse test")
    print("      Run: pip install cryptography")
except Exception as e:
    print(f"   ❌ Key signing FAILED: {e}")
    print("      This is the actual cause of invalid_grant")

# ── 4. Check system clock (most common cause) ────────────────
print(f"\n── System Clock Check ──")
local_time = datetime.datetime.utcnow()
print(f"   Server UTC time: {local_time.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"   Unix timestamp : {int(time.time())}")

try:
    import urllib.request
    with urllib.request.urlopen("https://worldtimeapi.org/api/timezone/UTC", timeout=5) as r:
        world_time = json.loads(r.read())
        world_unix = world_time.get("unixtime", 0)
        drift = abs(int(time.time()) - world_unix)
        print(f"   World clock UTC: {world_time.get('utc_datetime', 'unknown')}")
        print(f"   Clock drift    : {drift} seconds")
        if drift > 30:
            print(f"   ❌ CLOCK DRIFT IS {drift}s — THIS IS THE CAUSE OF invalid_grant!")
            print(f"      Google rejects JWTs if server clock is off by more than 60s.")
        else:
            print(f"   ✓  Clock is accurate (drift < 30s)")
except Exception as e:
    print(f"   ⚠️  Could not check world clock: {e}")

# ── 5. Check Google Sheets API is enabled ────────────────────
print(f"\n── Quick Auth Test ──")
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    pk_fixed = pk_normalized
    cred["private_key"] = pk_fixed
    
    creds = service_account.Credentials.from_service_account_info(
        cred, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    
    sheets_id = os.getenv("GOOGLE_SHEETS_ID", "")
    if sheets_id:
        service = build("sheets", "v4", credentials=creds)
        result = service.spreadsheets().get(spreadsheetId=sheets_id).execute()
        print(f"   ✓  Sheets API call succeeded!")
        print(f"   ✓  Spreadsheet title: {result.get('properties', {}).get('title', 'unknown')}")
    else:
        print("   ⚠️  GOOGLE_SHEETS_ID not set, skipping live test")
        # Try token refresh only
        import google.auth.transport.requests
        request = google.auth.transport.requests.Request()
        creds.refresh(request)
        print(f"   ✓  Token refresh succeeded!")
        print(f"   ✓  Token expiry: {creds.expiry}")

except Exception as e:
    print(f"   ❌ Auth test FAILED: {e}")
    print(f"\n   Possible causes:")
    print(f"   1. Wrong service account (not the one with Sheets access)")
    print(f"   2. Google Sheets API not enabled in Google Cloud Console")  
    print(f"   3. Service account not added as editor to the spreadsheet")
    print(f"   4. private_key_id mismatch (key was rotated/deleted)")

print("\n" + "=" * 60)
print("DONE")
print("=" * 60)