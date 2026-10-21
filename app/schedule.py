import json
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo("Asia/Seoul")


# 임시 테스트: 한국 시간의 오늘 날짜를 유지하고 시각만 17:00으로 고정합니다.
# 테스트 후 return datetime.now(SEOUL)로 되돌리세요.
def now_seoul():
    return datetime.now(SEOUL).replace(hour=17, minute=0, second=0, microsecond=0)


class Schedule:
    def __init__(self, path: str | Path):
        self.slots = json.loads(Path(path).read_text(encoding="utf-8"))
        numbers = set()
        previous_end = time.min
        for slot in self.slots:
            start, end = time.fromisoformat(slot["startTime"]), time.fromisoformat(slot["endTime"])
            if not (previous_end <= start < end):
                raise ValueError("시간표는 시간순이며 겹치지 않아야 합니다.")
            previous_end = end
            n = slot.get("period")
            if n is not None:
                if type(n) is not int or n < 1 or n in numbers:
                    raise ValueError("교시 번호는 중복 없는 양의 정수입니다.")
                numbers.add(n)
        if not numbers:
            raise ValueError("하나 이상의 교시가 필요합니다.")
        self.numbers = numbers

    # 시작 시각은 포함하고 종료 시각은 제외하여 현재 시간 구간을 찾습니다.
    def current(self, now):
        t = now.astimezone(SEOUL).time().replace(tzinfo=None)
        for slot in self.slots:
            if time.fromisoformat(slot["startTime"]) <= t < time.fromisoformat(slot["endTime"]):
                return slot
        return {"label": "교시 외 시간", "period": None, "startTime": None, "endTime": None}

    # 교시 입력을 시작·종료 교시로 변환하고 시간표에 존재하는지 검사합니다.
    def parse(self, value):
        text = value.strip().replace("-", "~")
        parts = text.split("~")

        if len(parts) > 2:
            raise ValueError("교시 범위를 확인해 주세요.")

        def read_number(part):
            number = part.strip().removesuffix("교시").strip()

            if not number.isascii() or not number.isdecimal():
                raise ValueError("교시 번호는 숫자로 입력해 주세요.")

            return int(number)

        start = read_number(parts[0])
        end = read_number(parts[1]) if len(parts) == 2 else start

        if start > end:
            raise ValueError("시작 교시는 종료 교시보다 클 수 없습니다.")

        # 큰 범위를 만들기 전에 길이를 검사합니다.
        if end - start + 1 > len(self.numbers):
            raise ValueError("설정된 시간표에 없는 교시입니다.")

        for period in range(start, end + 1):
            if period not in self.numbers:
                raise ValueError("설정된 시간표에 없는 교시입니다.")

        return start, end
