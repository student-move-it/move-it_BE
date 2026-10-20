from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Digits = Annotated[str, StringConstraints(pattern=r"^[0-9]+$", min_length=1, max_length=32)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Student(Model):
    studentNumber: Digits
    name: Text


class Move(Model):
    students: list[Student] = Field(min_length=1, max_length=100)
    periodString: Text
    place: Text

    @field_validator("students")
    @classmethod
    # 같은 요청에 중복된 학번이 있으면 등록을 거절합니다.
    def unique_students(cls, students):
        if len({s.studentNumber for s in students}) != len(students):
            raise ValueError("중복된 학번입니다.")
        return students


class EditMove(Model):
    periodString: Text
    place: Text


class CombinedMove(Model):
    students: list[Student] = Field(min_length=1, max_length=100)
    placeAndPeriod: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]

    # 교시와 장소를 분리합니다. 장소에 포함된 공백과 숫자는 유지합니다.
    def to_move(self) -> Move:
        period, separator, place = self.placeAndPeriod.partition("교시")

        if not separator or not period.strip() or not place.strip():
            raise ValueError("교시와 장소를 입력해 주세요.")

        period = period.strip()
        place = place.strip()

        # '8교시~10교시 장소'처럼 양쪽에 교시를 쓴 범위도 처리합니다.
        if place.startswith(("~", "-")):
            end_period, separator, place = place[1:].partition("교시")
            end_period = end_period.strip()

            if not separator or not end_period or not place.strip():
                raise ValueError("종료 교시와 장소를 입력해 주세요.")

            period = period + "~" + end_period

        return Move(
            students=self.students,
            periodString=period + "교시",
            place=place.strip(),
        )


class Analog(Model):
    change: int

    @field_validator("change")
    @classmethod
    # 인원 변경 값은 0이 아닌 정수만 허용합니다.
    def nonzero(cls, value):
        if value == 0:
            raise ValueError("0이 아닌 정수여야 합니다.")
        return value


class Classroom(Model):
    guildId: Digits
    classNumber: int = Field(ge=11, le=99)
    totalStudents: int = Field(ge=1, le=1000)

    @field_validator("classNumber")
    @classmethod
    # 반 코드의 마지막 자리가 0인 잘못된 반 번호를 거절합니다.
    def valid_class(cls, value):
        if value % 10 == 0:
            raise ValueError("반 번호는 1~9입니다.")
        return value


# 실제 응답 검증과 Swagger 문서에 함께 사용하는 응답 모델입니다.
class SuccessResponse(BaseModel):
    success: Literal[True] = True
    message: str


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    success: Literal[False] = False
    error: ErrorDetail


class ClassroomResponse(SuccessResponse):
    classNumber: int
    totalStudents: int


class MoveResponse(SuccessResponse):
    students: list[Student]
    periodString: str
    place: str


class MovementResponse(Student):
    periodString: str
    place: str


class EditMoveResponse(SuccessResponse):
    movement: MovementResponse


class BatchEditMoveResponse(SuccessResponse):
    movements: list[MovementResponse]


class AnalogResponse(SuccessResponse):
    analogCount: int


class CountsResponse(SuccessResponse):
    totalStudents: int
    remainingStudents: int
    movedStudents: int


class CurrentMovement(BaseModel):
    name: str
    startPeriod: int
    endPeriod: int
    place: str


class CurrentMovementsResponse(SuccessResponse):
    movedStudents: list[CurrentMovement]


class PeriodResponse(BaseModel):
    success: Literal[True] = True
    currentPeriod: str
    startTime: str | None
    endTime: str | None


# 반별 통합 조회 응답: 한 시점의 교시, 인원, 이동 목록을 함께 전달합니다.
class ClassSnapshotResponse(BaseModel):
    success: Literal[True] = True
    classNumber: int
    date: str
    period: PeriodResponse
    counts: CountsResponse
    movements: CurrentMovementsResponse


# 프론트가 서버 ID 없이 등록된 반을 선택할 때 사용하는 목록입니다.
class ClassListItem(BaseModel):
    classNumber: int
    className: str
    totalStudents: int


class ClassListResponse(BaseModel):
    success: Literal[True] = True
    classes: list[ClassListItem]
