import asyncio
import json
import logging
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated

from fastapi import Body, Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app.schedule import Schedule, now_seoul
from app.schemas import (
    Analog, AnalogResponse, BatchEditMoveResponse, Classroom, ClassroomResponse,
    CombinedMove, CountsResponse, CurrentMovementsResponse, EditMove, EditMoveResponse,
    ClassListResponse, ClassSnapshotResponse, ErrorResponse, Move, MoveResponse, PeriodResponse, SuccessResponse,
)
from app.service import APIError, Service, fail

ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)


# DB, 시간표, 인증 및 API 경로를 구성해 서버 앱을 만듭니다.
def create_app(database=None, schedule_path=None, clock=now_seoul, api_key=None, admin_key=None):
    service = Service(database or os.getenv("DATABASE_PATH", str(ROOT / "data" / "classroom.db")),
                      Schedule(schedule_path or os.getenv("SCHEDULE_PATH", str(ROOT / "config" / "schedule.json"))), clock)
    api_key = os.getenv("API_KEY", "") if api_key is None else api_key
    admin_key = os.getenv("ADMIN_API_KEY", "") if admin_key is None else admin_key
    if bool(api_key) != bool(admin_key):
        raise ValueError("API_KEY와 ADMIN_API_KEY는 함께 설정해야 합니다.")
    if api_key and api_key == admin_key:
        raise ValueError("API_KEY와 ADMIN_API_KEY는 서로 다른 값이어야 합니다.")

    # 0.5초마다 날짜를 확인하여 일일 초기화를 수행합니다.
    async def maintenance():
        while True:
            try:
                await run_in_threadpool(service.tick)
            except Exception:
                logger.exception("일일 초기화 확인 실패")
            await asyncio.sleep(0.5)

    @asynccontextmanager
    # 시작 시 초기화를 확인하고 종료 시 백그라운드 작업을 정리합니다.
    async def lifespan(app):
        await run_in_threadpool(service.tick)
        task = asyncio.create_task(maintenance())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    error_responses = {
        status: {"model": ErrorResponse, "description": description}
        for status, description in {
            400: "요청 값 또는 교시 형식 오류 (INVALID_REQUEST)",
            401: "인증키 누락 또는 오류 (UNAUTHORIZED)",
            403: "교실 설정 권한 없음 (FORBIDDEN)",
            404: "등록되지 않은 교실 또는 이동 정보",
            409: "중복 이동, 정원 초과 또는 현재 상태와 충돌",
            500: "서버 내부 오류 (INTERNAL_SERVER_ERROR)",
            503: "일시적인 데이터베이스 사용 불가 (SERVICE_UNAVAILABLE)",
        }.items()
    }
    app = FastAPI(title="교실 이동 관리 API", version="1.1.0", lifespan=lifespan,
                  responses=error_responses)

    # 실제 오류 처리와 일치하도록 Swagger 문서를 구성합니다.
    def openapi():
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            # 입력 검증 오류를 Notion 명세에 맞춰 HTTP 400으로 표시합니다.
            for path in schema["paths"].values():
                for operation in path.values():
                    if isinstance(operation, dict):
                        operation.get("responses", {}).pop("422", None)
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi
    app.state.service = service
    origins = [s.strip() for s in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",") if s.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"], allow_headers=["Content-Type", "X-API-Key"])

    # API 키로 일반 사용자와 관리자를 구분합니다. 키 미설정 시 개발용으로 인증을 생략합니다.
    def authenticate(x_api_key: Annotated[str | None, Header()] = None):
        if not api_key:
            return "admin"
        if x_api_key and secrets.compare_digest(x_api_key, admin_key):
            return "admin"
        if x_api_key and secrets.compare_digest(x_api_key, api_key):
            return "user"
        fail(401, "UNAUTHORIZED", "유효한 API 인증키가 필요합니다.")

    # 교실 설정 변경은 관리자만 허용합니다.
    def admin(role=Depends(authenticate)):
        if role != "admin":
            fail(403, "FORBIDDEN", "교실 설정 권한이 없습니다.")

    @app.exception_handler(APIError)
    # 업무 처리 오류를 공통 JSON 응답으로 변환합니다.
    async def api_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"success": False, "error": {"code": exc.code, "message": exc.message}})

    @app.exception_handler(RequestValidationError)
    # 필수 값 누락이나 형식 오류를 HTTP 400으로 반환합니다.
    async def validation_error(request, exc):
        return await api_error(request, APIError(400, "INVALID_REQUEST", "필수 값과 입력 형식을 확인해 주세요."))

    @app.exception_handler(HTTPException)
    # HTTP 오류를 공통 JSON 응답으로 반환합니다.
    async def http_error(request, exc):
        return await api_error(request, APIError(exc.status_code, "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR", str(exc.detail)))

    @app.exception_handler(sqlite3.OperationalError)
    # DB 잠금은 503, 그 밖의 DB 오류는 500으로 처리합니다.
    async def database_error(request, exc):
        logger.error("데이터베이스 오류", exc_info=exc)
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return await api_error(request, APIError(503, "SERVICE_UNAVAILABLE", "서버가 사용 중입니다. 잠시 후 다시 시도해 주세요."))
        return await api_error(request, APIError(500, "INTERNAL_SERVER_ERROR", "데이터를 처리하지 못했습니다."))

    @app.exception_handler(Exception)
    # 예상하지 못한 오류를 로그에 남기고 공통 오류 응답을 반환합니다.
    async def internal_error(request, exc):
        logger.error("서버 오류", exc_info=exc)
        return await api_error(request, APIError(500, "INTERNAL_SERVER_ERROR", "요청을 처리하지 못했습니다."))

    # 쿼리를 우선 사용하고 없으면 JSON 본문에서 읽어 형식을 검사합니다.
    def get_parameter(query, body, name, kind):
        # 명세의 GET JSON 본문과 브라우저용 쿼리 입력을 모두 허용합니다.
        raw = query if query is not None else (body or {}).get(name)
        if kind is int:
            if isinstance(raw, bool) or not (type(raw) is int or isinstance(raw, str) and re.fullmatch(r"[0-9]+", raw)):
                fail(400, "INVALID_REQUEST", f"{name} 값을 확인해 주세요.")
            value = int(raw)
            if not 11 <= value <= 99 or value % 10 == 0:
                fail(400, "INVALID_REQUEST", "반 번호를 확인해 주세요.")
            return value
        if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,32}", raw):
            fail(400, "INVALID_REQUEST", f"{name} 값을 확인해 주세요.")
        return raw

    # 수정·복귀 대상 학번 목록의 형식과 중복을 검사합니다.
    def targets(studentNumber: Annotated[list[str], Query(min_length=1, max_length=100)]):
        if any(not re.fullmatch(r"[0-9]{1,32}", s) for s in studentNumber) or len(set(studentNumber)) != len(studentNumber):
            fail(400, "INVALID_REQUEST", "중복 없이 대상 학번을 입력해 주세요.")
        return studentNumber

    @app.get("/health")
    # 서버가 응답 가능한 상태인지 확인합니다.
    def health():
        return {"success": True, "status": "ok"}

    @app.put("/classroom", dependencies=[Depends(admin)], response_model=ClassroomResponse,
             responses={201: {"model": ClassroomResponse, "description": "교실 최초 설정"}})
    # 교실 설정을 저장합니다. 최초 등록은 201, 변경은 200입니다.
    def put_classroom(data: Classroom, response: Response):
        result, status = service.save_classroom(data)
        response.status_code = status
        return result

    @app.get("/classroom", dependencies=[Depends(authenticate)], response_model=ClassroomResponse)
    # 등록된 교실의 반 번호와 전체 학생 수를 조회합니다.
    def get_classroom(guildId: str | None = None, body: dict | None = Body(default=None)):
        return service.get_classroom(get_parameter(guildId, body, "guildId", str))

    @app.post("/move", status_code=201, dependencies=[Depends(authenticate)], response_model=MoveResponse)
    # 통합 입력 또는 분리 입력을 받아 학생 이동을 등록합니다.
    def create_move(data: Move | CombinedMove, guildId: Annotated[str, Query(pattern=r"^[0-9]{1,32}$")]):
        if isinstance(data, CombinedMove):
            try:
                data = data.to_move()
            except ValueError:
                fail(400, "INVALID_REQUEST", "학생 정보, 교시, 이동 장소를 올바르게 입력해 주세요.")
        return service.create_moves(data, guildId)

    @app.patch("/move", dependencies=[Depends(authenticate)], response_model=EditMoveResponse | BatchEditMoveResponse)
    # 기존 이동 교시와 장소를 수정합니다. 한 대상이라도 실패하면 전체 변경을 취소합니다.
    def edit_move(data: EditMove, guildId: Annotated[str, Query(pattern=r"^[0-9]{1,32}$")], students=Depends(targets), sourcePeriod: Annotated[int | None, Query(ge=1)] = None):
        return service.edit_move(guildId, students, data, sourcePeriod)

    @app.delete("/move", dependencies=[Depends(authenticate)], response_model=SuccessResponse)
    # 대상 학생의 이동 기록을 삭제하여 복귀 처리합니다.
    def return_move(guildId: Annotated[str, Query(pattern=r"^[0-9]{1,32}$")], students=Depends(targets), sourcePeriod: Annotated[int | None, Query(ge=1)] = None):
        return service.return_moves(guildId, students, sourcePeriod)

    @app.patch("/analog-count", dependencies=[Depends(authenticate)], response_model=AnalogResponse)
    # 아날로그 이동 인원을 요청한 수만큼 변경합니다.
    def analog(data: Analog, guildId: Annotated[str, Query(pattern=r"^[0-9]{1,32}$")]):
        return service.change_analog(guildId, data.change)

    @app.get("/student-counts", dependencies=[Depends(authenticate)], response_model=CountsResponse)
    # 현재 교시의 이동 인원과 잔류 인원을 조회합니다.
    def counts(classNumber: str | None = None, body: dict | None = Body(default=None)):
        return service.snapshot(get_parameter(classNumber, body, "classNumber", int))["counts"]

    @app.get("/movedStudents", dependencies=[Depends(authenticate)], response_model=CurrentMovementsResponse)
    # 현재 이동과 오늘의 미래 교시 예약 목록을 반환합니다. 인원 집계는 현재 교시 기준입니다.
    def movements(classNumber: str | None = None, body: dict | None = Body(default=None)):
        return service.snapshot(get_parameter(classNumber, body, "classNumber", int))["movements"]

    @app.get("/classes", dependencies=[Depends(authenticate)],
             response_model=ClassListResponse, summary="등록된 반 목록 조회")
    # 프론트는 이 목록에서 반을 선택한 뒤 /classes/{반 코드}를 호출합니다.
    def get_classes():
        return service.list_classes()

    @app.get("/classes/{class_number}", dependencies=[Depends(authenticate)],
             response_model=ClassSnapshotResponse, summary="반별 통합 현황 조회")
    # 반 코드(1학년 2반=12)를 받아 해당 반의 데이터만 조회합니다.
    def get_class_data(class_number: str):
        return service.snapshot(get_parameter(class_number, None, "classNumber", int))

    @app.get("/period", dependencies=[Depends(authenticate)], response_model=PeriodResponse)
    # 현재 교시 이름과 시작·종료 시각을 반환합니다.
    def period():
        return service.period()

    @app.get("/events", dependencies=[Depends(authenticate)], response_class=StreamingResponse,
             responses={200: {"description": "교시·이동·인원 스냅샷 스트림", "content": {"text/event-stream": {"schema": {"type": "string"}}}}})
    # 전자칠판에 실시간 상태를 전달하는 SSE 연결을 제공합니다.
    async def events(request: Request, classNumber: str = Query()):
        classNumber = get_parameter(classNumber, None, "classNumber", int)
        initial = await run_in_threadpool(service.snapshot, classNumber)

        # 첫 스냅샷을 보내고 1초마다 변경을 확인합니다. 15초마다 연결 유지 신호를 보냅니다.
        async def stream():
            previous = json.dumps(initial, ensure_ascii=False)
            yield f"event: snapshot\ndata: {previous}\n\n"
            heartbeat = 0
            while not await request.is_disconnected():
                await asyncio.sleep(1)
                try:
                    snapshot = await run_in_threadpool(service.snapshot, classNumber)
                except APIError as exc:
                    yield 'event: error\ndata: ' + json.dumps({"success": False, "error": {"code": exc.code, "message": exc.message}}, ensure_ascii=False) + '\n\n'
                    return
                current = json.dumps(snapshot, ensure_ascii=False)
                if current != previous:
                    yield f"event: snapshot\ndata: {current}\n\n"
                    previous = current
                heartbeat += 1
                if heartbeat % 15 == 0:
                    yield ": heartbeat\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


app = create_app()
