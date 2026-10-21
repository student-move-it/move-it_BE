"""로컬 SQLite 초기화·기존 DB 변환. Neon용 초기화는 postgres.py에서 처리합니다."""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE classroom (
    guild_id TEXT PRIMARY KEY NOT NULL,
    class_number INTEGER UNIQUE NOT NULL CHECK(class_number > 0),
    total_students INTEGER NOT NULL CHECK(total_students > 0)
);
CREATE TABLE movement (
    movement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL REFERENCES classroom(guild_id),
    student_number TEXT NOT NULL,
    student_name TEXT NOT NULL,
    move_date TEXT NOT NULL,
    period INTEGER NOT NULL CHECK(period > 0),
    place TEXT NOT NULL,
    registration_id INTEGER NOT NULL,
    period_string TEXT NOT NULL,
    UNIQUE(guild_id, student_number, move_date, period)
);
CREATE INDEX idx_movement_current ON movement(guild_id, move_date, period);
CREATE INDEX idx_movement_registration ON movement(registration_id);
CREATE TABLE analog_count (
    guild_id TEXT NOT NULL REFERENCES classroom(guild_id),
    count_date TEXT NOT NULL,
    analog_count INTEGER NOT NULL DEFAULT 0 CHECK(analog_count >= 0),
    PRIMARY KEY(guild_id, count_date)
);
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE VIEW movement_ranges AS
    SELECT registration_id AS id, guild_id, student_number, student_name AS name,
           MIN(period) AS start_period, MAX(period) AS end_period,
           period_string, place, move_date AS day
    FROM movement
    GROUP BY registration_id, guild_id, student_number, student_name,
             period_string, place, move_date;
"""


# 기존 DB를 백업한 뒤 새 구조로 변환합니다. 실패하면 호출부에서 전체 롤백합니다.
def initialize(db, database, day):
    db.execute("PRAGMA journal_mode=WAL")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(classroom)")}
    if "total_students" in columns:
        return
    legacy = "total" in columns
    if legacy:
        backup = Path(str(database) + ".legacy-backup.db")
        if not backup.exists():
            with sqlite3.connect(backup) as target:
                db.backup(target)
    # executescript는 자동 커밋하므로 구조 변경도 롤백할 수 있도록 execute를 사용합니다.
    db.execute("BEGIN IMMEDIATE")
    if legacy:
        db.execute("ALTER TABLE movement RENAME TO legacy_movement")
        db.execute("ALTER TABLE classroom RENAME TO legacy_classroom")
    for statement in SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    if legacy:
        db.execute("INSERT INTO classroom SELECT guild_id, class_number, total FROM legacy_classroom")
        old_day = db.execute("SELECT value FROM metadata WHERE key='day'").fetchone()
        db.execute(
            "INSERT INTO analog_count SELECT guild_id, ?, analog FROM legacy_classroom",
            (old_day[0] if old_day else day,),
        )
        for row in db.execute("SELECT * FROM legacy_movement").fetchall():
            db.executemany(
                "INSERT INTO movement(guild_id,student_number,student_name,move_date,period,place,registration_id,period_string) VALUES (?,?,?,?,?,?,?,?)",
                [(row["guild_id"], row["student_number"], row["name"], row["day"], period,
                  row["place"], row["id"], row["period_string"])
                 for period in range(row["start_period"], row["end_period"] + 1)],
            )
        db.execute("DROP TABLE legacy_movement")
        db.execute("DROP TABLE legacy_classroom")
    db.execute("PRAGMA user_version=2")
