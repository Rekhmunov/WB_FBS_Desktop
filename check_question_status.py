import sys, os, subprocess, json
sys.path.insert(0, "/var/www/www-root/data/www/feedpilot")

env_out = subprocess.check_output(
    ["systemctl", "show", "feedpilot", "--property=EnvironmentFiles"], text=True
)
# Read env file
import re
env_file = re.search(r'EnvironmentFiles=(.*?) \(', env_out)
db_url = ""
if env_file:
    env_path = env_file.group(1).strip()
    try:
        with open(env_path) as f:
            for line in f:
                if "APP_DB_URL" in line:
                    db_url = line.strip().split("=", 1)[1]
    except PermissionError:
        pass

if not db_url:
    # Try from process environ
    pid_out = subprocess.check_output(["systemctl", "show", "feedpilot", "--property=MainPID"], text=True)
    pid = pid_out.strip().split("=")[1]
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            for item in f.read().split(b"\x00"):
                if b"APP_DB_URL" in item:
                    db_url = item.decode().split("=", 1)[1]
    except:
        pass

if not db_url:
    print("Cannot get DB URL")
    sys.exit(1)

import psycopg, psycopg.rows
conn = psycopg.connect(db_url, row_factory=psycopg.rows.dict_row, autocommit=True)

# Find the specific question
print("=== Question about latex mattress ===")
rows = conn.execute(
    """SELECT conversation_uid, kind, status, last_sent_at, last_message_at, updated_at,
       message_text, customer_name,
       metadata_json::jsonb->'raw'->'answer' as answer,
       metadata_json::jsonb->'raw'->>'state' as state
       FROM conversation_items
       WHERE kind='question' AND message_text ILIKE '%латекс%'
       LIMIT 5"""
).fetchall()

if not rows:
    # Try broader search
    rows = conn.execute(
        """SELECT conversation_uid, kind, status, last_sent_at,
           message_text,
           metadata_json::jsonb->'raw'->'answer' as answer,
           metadata_json::jsonb->'raw'->>'state' as state
           FROM conversation_items
           WHERE kind='question' AND message_text ILIKE '%матрас%'
           LIMIT 5"""
    ).fetchall()

for r in rows:
    print(f"\nUID: {r['conversation_uid']}")
    print(f"Status: {r['status']}")
    print(f"last_sent_at: {r['last_sent_at']}")
    print(f"last_message_at: {r['last_message_at']}")
    print(f"updated_at: {r['updated_at']}")
    print(f"message_text: {str(r['message_text'])[:100]}")
    print(f"state (raw): {r['state']}")
    answer = r['answer']
    if answer:
        print(f"answer (raw): {json.dumps(answer, ensure_ascii=False)[:200]}")
    else:
        print("answer: NULL/None (NO ANSWER IN RAW DATA)")

# Also check all questions statuses
print("\n\n=== All questions statuses ===")
rows2 = conn.execute(
    """SELECT conversation_uid, status, last_sent_at,
       metadata_json::jsonb->'raw'->>'state' as raw_state,
       metadata_json::jsonb->'raw'->'answer' as raw_answer,
       message_text
       FROM conversation_items WHERE kind='question'"""
).fetchall()
for r in rows2:
    ans = r['raw_answer']
    ans_text = ans.get('text', '') if isinstance(ans, dict) else ''
    print(f"status={r['status']} raw_state={r['raw_state']} has_answer={'YES: ' + ans_text[:30] if ans_text else 'NO'}")
    print(f"  text: {str(r['message_text'])[:60]}")
