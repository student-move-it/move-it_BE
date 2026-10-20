import sqlite3
from contextlib import contextmanager
from datetime import time
from pathlib import Path

from app.schedule import SEOUL, now_seoul
from app.database import initialize


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def fail(status, code, message):
    raise APIError(status, code, message)


# 장소 약어를 추가하려면 아래 목록에 "입력 약어": "표시할 정식 이름"을 추가하세요.
def normalize_place(place):
    aliases = {
        "임베1실": "임베디드 1실",
        "임베2실": "임베디드 2실",
        "임베디드2실": "임베디드 2실",
        "SW1실": "소프트웨어 1실",
        "SW2실": "소프트웨어 2실",
        "SW3실": "소프트웨어 3실",
        "세미나2-1": "세미나실 2-1",
        "세미나2-2": "세미나실 2-2",
        "세미나2-3": "세미나실 2-3",
        "세미나2-4": "세미나실 2-4",
        "세미나3-1": "세미나실 3-1",
        "세미나3-2": "세미나실 3-2",
        "세미나3-3": "세미나실 3-3",
        "세미나3-4": "세미나실 3-4",
        "세미나4-1": "세미나실 4-1",
        "세미나4-2": "세미나실 4-2",
        "세미나4-3": "세미나실 4-3",
        "세미나4-4": "세미나실 4-4",
    }
    # 약어 비교 시 모든 공백을 무시합니다. 목록에 없는 장소의 내부 공백은 유지합니다.
    key = "".join(place.split())
    return aliases.get(key, place.strip())


class Service:
    def __init__(self, database, schedule, clock=now_seoul):
        self.database, self.schedule, self.clock = str(database), schedule, clock
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            initialize(db, database, self.clock().astimezone(SEOUL).date().isoformat())

    @contextmanager
    # DB 연결과 외래키 검사를 활성화합니다. 성공 시 커밋, 오류 시 롤백 후 연결을 닫습니다.
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    # 쓰기를 직렬화하고 날짜 변경 시 이동·아날로그 기록을 초기화합니다.
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = self.clock().astimezone(SEOUL)
            day = now.date().isoformat()
            old = db.execute("SELECT value FROM metadata WHERE key='day'").fetchone()
            # 재시작이나 조회 시에도 날짜를 확인합니다. 교실 설정과 전체 학생 수는 유지합니다.
            if old is None or old[0] != day or now.time().replace(tzinfo=None) >= time(23, 59, 59):
                db.execute("DELETE FROM movement")
                db.execute("DELETE FROM analog_count")
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('day', ?)", (day,))
            yield db, now

    # 등록된 반 목록을 번호순으로 반환합니다. 디스코드 서버 ID는 프론트에 필요하지 않습니다.
    def list_classes(self):
        with self.connect() as db:
            rows = db.execute("SELECT class_number,total_students FROM classroom ORDER BY class_number").fetchall()
            return {"success": True, "classes": [
                {"classNumber": row["class_number"],
                 "className": f"{row['class_number'] // 10}학년 {row['class_number'] % 10}반",
                 "totalStudents": row["total_students"]}
                for row in rows
            ]}

    # 일일 초기화 조건을 확인합니다.
    def tick(self):
        with self.transaction():
            pass

    # 서버 ID 또는 반 번호로 교실과 당일 아날로그 인원을 조회합니다.
    def classroom(self, db, guild=None, class_number=None):
        if guild is not None:
            row = db.execute("SELECT c.*, c.total_students AS total, COALESCE(a.analog_count, 0) AS analog FROM classroom c LEFT JOIN analog_count a ON a.guild_id=c.guild_id AND a.count_date=(SELECT value FROM metadata WHERE key='day') WHERE c.guild_id=?", (guild,)).fetchone()
        else:
            row = db.execute("SELECT c.*, c.total_students AS total, COALESCE(a.analog_count, 0) AS analog FROM classroom c LEFT JOIN analog_count a ON a.guild_id=c.guild_id AND a.count_date=(SELECT value FROM metadata WHERE key='day') WHERE c.class_number=?", (class_number,)).fetchone()
        if row is None:
            fail(404, "CLASSROOM_NOT_FOUND", "등록되지 않은 반입니다.")
        return row

    # 교시 입력을 시작·종료 교시로 변환하고 시간표에 존재하는지 검사합니다.
    def parse(self, value):
        try:
            return self.schedule.parse(value)
        except ValueError as exc:
            fail(400, "INVALID_REQUEST", str(exc))

    # 현재 교시에 해당하는 이동만 조회합니다. 쉬는시간에는 빈 목록입니다.
    def active(self, db, guild, now):
        period = self.schedule.current(now).get("period")
        if period is None:
            return []
        return db.execute(
            "SELECT * FROM movement_ranges WHERE guild_id=? AND day=? AND start_period<=? AND end_period>=? ORDER BY student_number",
            (guild, now.date().isoformat(), period, period),
        ).fetchall()

    # 오늘 아직 끝나지 않은 교시의 이동을 조회합니다. 쉬는시간에는 이후 교시 예약이 남습니다.
    def visible_movements(self, db, guild, now):
        current_time = now.time().replace(tzinfo=None)
        periods = []
        for slot in self.schedule.slots:
            if slot.get("period") is not None and time.fromisoformat(slot["endTime"]) > current_time:
                periods.append(slot["period"])

        if not periods:
            return []

        placeholders = ",".join("?" for _ in periods)
        # 묶음의 실제 교시 행을 확인하므로 기간이 지난 이동은 목록에서 제외됩니다.
        return db.execute(
            f"""SELECT r.* FROM movement_ranges AS r
                WHERE r.guild_id=? AND r.day=?
                AND EXISTS (
                    SELECT 1 FROM movement AS m
                    WHERE m.registration_id=r.id
                    AND m.guild_id=r.guild_id AND m.move_date=r.day
                    AND m.period IN ({placeholders})
                )""",
            (guild, now.date().isoformat(), *periods),
        ).fetchall()

    # 디지털 이동 인원과 아날로그 인원의 합이 정원을 넘는지 검사합니다.
    def check_capacity(self, db, room, analog=None, total=None):
        # 예약된 모든 교시의 정원을 검사하여 교시 전환 시에도 인원이 초과하지 않게 합니다.
        analog = room["analog"] if analog is None else analog
        total = room["total"] if total is None else total
        for period in self.schedule.numbers:
            count = db.execute(
                "SELECT COUNT(DISTINCT student_number) FROM movement_ranges WHERE guild_id=? AND start_period<=? AND end_period>=?",
                (room["guild_id"], period, period),
            ).fetchone()[0]
            if count + analog > total:
                fail(409, "CAPACITY_EXCEEDED", "디지털 이동 인원과 아날로그 이동 인원의 합이 전체 학생 수를 초과합니다.")

    # 반 중복과 정원을 확인한 뒤 교실 설정을 등록하거나 변경합니다.
    def save_classroom(self, data):
        with self.transaction() as (db, now):
            old = db.execute("SELECT c.*, c.total_students AS total, COALESCE(a.analog_count, 0) AS analog FROM classroom c LEFT JOIN analog_count a ON a.guild_id=c.guild_id AND a.count_date=(SELECT value FROM metadata WHERE key='day') WHERE c.guild_id=?", (data.guildId,)).fetchone()
            duplicate = db.execute("SELECT guild_id FROM classroom WHERE class_number=? AND guild_id<>?", (data.classNumber, data.guildId)).fetchone()
            if duplicate:
                fail(409, "CLASSROOM_ALREADY_EXISTS", "다른 서버에 이미 설정된 반입니다.")
            if old:
                self.check_capacity(db, old, total=data.totalStudents)
                if old["class_number"] != data.classNumber and db.execute("SELECT 1 FROM movement_ranges WHERE guild_id=?", (data.guildId,)).fetchone():
                    fail(409, "CLASSROOM_HAS_MOVEMENTS", "이동 정보가 있는 반의 번호는 변경할 수 없습니다.")
                db.execute("UPDATE classroom SET class_number=?, total_students=? WHERE guild_id=?", (data.classNumber, data.totalStudents, data.guildId))
            else:
                db.execute("INSERT INTO classroom(guild_id,class_number,total_students) VALUES (?,?,?)", (data.guildId, data.classNumber, data.totalStudents))
        return {"success": True, "message": "교실 정보가 저장되었습니다.", "classNumber": data.classNumber, "totalStudents": data.totalStudents}, 200 if old else 201

    # 등록된 교실의 반 번호와 전체 학생 수를 조회합니다.
    def get_classroom(self, guild):
        with self.transaction() as (db, now):
            room = self.classroom(db, guild=guild)
            return {"success": True, "message": "교실 정보 조회 성공", "classNumber": room["class_number"], "totalStudents": room["total"]}

    # 중복과 정원을 검사하고 모든 학생을 한 트랜잭션으로 등록합니다.
    def create_moves(self, data, guild):
        # 저장과 응답에 같은 정식 장소 이름을 사용합니다.
        data = data.model_copy(update={"place": normalize_place(data.place)})
        start, end = self.parse(data.periodString)
        with self.transaction() as (db, now):
            self.ensure_open(now)
            # 봇이 보낸 서버 ID로 등록된 반을 한 번 조회합니다. 학번으로 반을 추정하지 않습니다.
            room = self.classroom(db, guild=guild)
            for student in data.students:
                overlap = db.execute(
                    "SELECT 1 FROM movement_ranges WHERE guild_id=? AND student_number=? AND start_period<=? AND end_period>=?",
                    (room["guild_id"], student.studentNumber, end, start),
                ).fetchone()
                if overlap:
                    fail(409, "MOVEMENT_ALREADY_EXISTS", "해당 교시에 이미 이동 등록된 학생이 있습니다.")
                self.insert_movement(db, room["guild_id"], student.studentNumber,
                                     student.name, now.date().isoformat(), start, end,
                                     data.periodString, data.place)
            self.check_capacity(db, room)
        return {"success": True, "message": "이동 현황이 등록되었습니다.", **data.model_dump()}

    @staticmethod
    # 교시마다 한 행씩 저장하고 같은 범위 이동에 등록 묶음 ID를 부여합니다.
    def insert_movement(db, guild, student, name, day, start, end, period_string, place, registration=None):
        if registration is None:
            registration = db.execute("SELECT COALESCE(MAX(registration_id),0)+1 FROM movement").fetchone()[0]
        db.executemany(
            "INSERT INTO movement(guild_id,student_number,student_name,move_date,period,place,registration_id,period_string) VALUES (?,?,?,?,?,?,?,?)",
            [(guild, student, name, day, period, place, registration, period_string)
             for period in range(start, end + 1)],
        )

    @staticmethod
    # 23:59:59 이후 일일 초기화 시간에는 신규 등록을 막습니다.
    def ensure_open(now):
        if now.time().replace(tzinfo=None) >= time(23, 59, 59):
            fail(409, "DAY_CLOSED", "일일 초기화 중입니다. 다음 날짜에 등록해 주세요.")

    # sourcePeriod 또는 현재 교시로 수정·복귀할 기존 이동 묶음을 찾습니다.
    def target(self, db, now, room, student, source_period=None):
        if source_period is not None and source_period not in self.schedule.numbers:
            fail(400, "INVALID_REQUEST", "설정된 시간표에 없는 sourcePeriod입니다.")
        period = source_period if source_period is not None else self.schedule.current(now).get("period")
        if period not in self.schedule.numbers:
            fail(404, "MOVEMENT_NOT_FOUND", "현재 교시에 처리할 이동 정보가 없습니다. sourcePeriod를 지정해 주세요.")
        row = db.execute(
            "SELECT * FROM movement_ranges WHERE guild_id=? AND student_number=? AND start_period<=? AND end_period>=?",
            (room["guild_id"], student, period, period),
        ).fetchone()
        if row is None:
            fail(404, "MOVEMENT_NOT_FOUND", "처리할 이동 정보가 없습니다.")
        return row

    # 기존 이동 교시와 장소를 수정합니다. 한 대상이라도 실패하면 전체 변경을 취소합니다.
    def edit_move(self, guild, students, data, source_period=None):
        # 저장과 응답에 같은 정식 장소 이름을 사용합니다.
        data = data.model_copy(update={"place": normalize_place(data.place)})
        start, end = self.parse(data.periodString)
        results = []
        with self.transaction() as (db, now):
            room = self.classroom(db, guild=guild)
            for student in students:
                row = self.target(db, now, room, student, source_period)
                if db.execute("SELECT 1 FROM movement_ranges WHERE guild_id=? AND student_number=? AND id<>? AND start_period<=? AND end_period>=?", (guild, student, row["id"], end, start)).fetchone():
                    fail(409, "MOVEMENT_ALREADY_EXISTS", "해당 교시에 이미 이동 등록된 학생이 있습니다.")
                db.execute("DELETE FROM movement WHERE registration_id=?", (row["id"],))
                self.insert_movement(db, guild, student, row["name"], row["day"],
                                     start, end, data.periodString, data.place, row["id"])
                results.append({"studentNumber": student, "name": row["name"], **data.model_dump()})
            self.check_capacity(db, room)
        return {"success": True, "message": "이동 현황이 수정되었습니다.", **({"movement": results[0]} if len(results) == 1 else {"movements": results})}

    # 교시 미지정 시 학생의 당일 이동을 모두 삭제합니다. 일부 대상이 없으면 전체 롤백합니다.
    def return_moves(self, guild, students, source_period=None):
        with self.transaction() as (db, now):
            room = self.classroom(db, guild=guild)
            for student in students:
                if source_period is not None:
                    # 기존 클라이언트가 교시를 보내면 해당 이동 묶음만 삭제합니다.
                    row = self.target(db, now, room, student, source_period)
                    db.execute("DELETE FROM movement WHERE registration_id=?", (row["id"],))
                else:
                    deleted = db.execute(
                        "DELETE FROM movement WHERE guild_id=? AND student_number=? AND move_date=?",
                        (guild, student, now.date().isoformat()),
                    )
                    if deleted.rowcount == 0:
                        fail(404, "MOVEMENT_NOT_FOUND", "복귀 처리할 이동 정보가 없습니다.")
        return {"success": True, "message": "복귀 처리 성공"}

    # 음수와 정원 초과를 막고 날짜별 아날로그 인원을 저장합니다.
    def change_analog(self, guild, change):
        with self.transaction() as (db, now):
            self.ensure_open(now)
            room = self.classroom(db, guild=guild)
            value = room["analog"] + change
            if value < 0:
                fail(409, "ANALOG_COUNT_BELOW_ZERO", "아날로그 이동 인원은 0명 미만이 될 수 없습니다.")
            self.check_capacity(db, room, analog=value)
            db.execute("INSERT INTO analog_count(guild_id,count_date,analog_count) VALUES (?,?,?) ON CONFLICT(guild_id,count_date) DO UPDATE SET analog_count=excluded.analog_count", (guild, now.date().isoformat(), value))
        return {"success": True, "message": "아날로그 이동 인원이 변경되었습니다.", "analogCount": value}

    # 교시·인원·이동 목록을 한 번에 조회해 일관된 화면 데이터를 만듭니다.
    def snapshot(self, class_number):
        with self.transaction() as (db, now):
            room = self.classroom(db, class_number=class_number)
            # 인원 계산은 현재 교시만 사용하고, 표시 목록은 미래 예약까지 포함합니다.
            active = self.active(db, room["guild_id"], now)
            visible = self.visible_movements(db, room["guild_id"], now)
            visible = [dict(row, place=normalize_place(row["place"])) for row in visible]
            visible.sort(key=lambda row: (
                row["start_period"], row["end_period"], row["place"], row["student_number"]
            ))
            moved = len(active) + room["analog"]
            slot = self.schedule.current(now)
            return {
                "success": True,
                "classNumber": room["class_number"],
                "date": now.date().isoformat(),
                "period": {"success": True, "currentPeriod": slot["label"], "startTime": slot["startTime"], "endTime": slot["endTime"]},
                "counts": {"success": True, "message": "학생 수 조회 성공", "totalStudents": room["total"], "remainingStudents": room["total"] - moved, "movedStudents": moved},
                "movements": {"success": True, "message": "현재 이동 현황 조회 성공", "movedStudents": [
                    {"name": f"{r['student_number']} {r['name']}", "startPeriod": r["start_period"], "endPeriod": r["end_period"], "place": r["place"]} for r in visible
                ]},
            }

    # 현재 교시 이름과 시작·종료 시각을 반환합니다.
    def period(self):
        slot = self.schedule.current(self.clock())
        return {"success": True, "currentPeriod": slot["label"], "startTime": slot["startTime"], "endTime": slot["endTime"]}
