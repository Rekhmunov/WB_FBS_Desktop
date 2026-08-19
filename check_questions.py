import sys, os
sys.path.insert(0, "/var/www/www-root/data/www/feedpilot")
APP_DB_URL = os.environ.get("APP_DB_URL", "")
if not APP_DB_URL:
    # Try loading from feedpilot env file via systemd env loading
    import subprocess
    env_out = subprocess.check_output(
        ["systemctl", "show", "feedpilot", "--property=Environment"], text=True
    )
    for part in env_out.strip().replace("Environment=", "").split():
        if "APP_DB_URL" in part:
            APP_DB_URL = part.split("=", 1)[1]
            break
import psycopg, psycopg.rows

conn = psycopg.connect(APP_DB_URL, row_factory=psycopg.rows.dict_row, autocommit=True)

# Check question metadata
rows = conn.execute(
    "SELECT kind, source, metadata_json::jsonb->'raw' as raw "
    "FROM conversation_items WHERE kind='question' LIMIT 3"
).fetchall()

print("=== QUESTIONS METADATA ===")
for r in rows:
    raw = r["raw"] or {}
    print(f"\nkind={r['kind']} source={r['source']}")
    print(f"  productName: {raw.get('productName', 'MISSING')}")
    print(f"  nmId: {raw.get('nmId', raw.get('nmID', 'MISSING'))}")
    answer = raw.get("answer", {})
    print(f"  answer: {answer}")
    print(f"  subjectName: {raw.get('subjectName', 'MISSING')}")
    print(f"  ALL KEYS: {sorted(raw.keys())}")

# Check answered questions
rows2 = conn.execute(
    "SELECT kind, source, last_sent_at, metadata_json::jsonb->'raw' as raw "
    "FROM conversation_items WHERE kind='question' AND status='answered_manual' LIMIT 3"
).fetchall()
print("\n=== ANSWERED QUESTIONS ===")
for r in rows2:
    raw = r["raw"] or {}
    answer = raw.get("answer", {})
    print(f"source={r['source']} last_sent_at={r['last_sent_at']}")
    print(f"  answer.text: {answer.get('text', 'MISSING') if isinstance(answer, dict) else answer}")
