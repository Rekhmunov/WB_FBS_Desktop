import os

# Allow tests to run without APP_DB_URL by using SQLite in-memory/temp files.
os.environ.setdefault("FEEDPILOT_TEST_MODE", "1")
