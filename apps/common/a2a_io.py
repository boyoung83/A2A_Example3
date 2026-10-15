# 파일명: a2a_io.py
"""회사 간 A2A 입출력을 한곳에 모은다.

서버 쪽:  SkillExecutor — 메시지의 DataPart {"skill": ..., ...} 를 보고 업무 함수로 보낸 뒤,
          결과(Outcome)를 Task 상태로 바꾼다. 텍스트만 온 요청은 default_skill로 처리한다(기존 호환).
클라이언트 쪽: call_skill — 상대 에이전트의 Agent Card에서 스킬을 확인하고(Ch13) 요청을 보내,
          최종 상태와 DataPart를 Reply로 돌려준다. 연결이 안 되면 PartnerUnavailable.
"""

import asyncio
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx
from a2a.client import A2ACardResolver, create_client
from a2a.helpers.proto_helpers import get_data_parts, new_data_part, new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types.a2a_pb2 import AgentCard, Message, Part, Role, SendMessageRequest, TaskState
from pydantic import ValidationError

COMPLETED, REJECTED, INPUT_REQUIRED, FAILED = "COMPLETED", "REJECTED", "INPUT_REQUIRED", "FAILED"
_STATE = {
    REJECTED: TaskState.TASK_STATE_REJECTED,
    INPUT_REQUIRED: TaskState.TASK_STATE_INPUT_REQUIRED,
    FAILED: TaskState.TASK_STATE_FAILED,
}


def part_text(parts) -> str:
    return "".join(p.text for p in parts if p.WhichOneof("content") == "text")


def data_and_text(data: dict, summary: str) -> list[Part]:
    """기계가 읽을 JSON과 사람이 읽을 한 줄을 함께 싣는다(관제 화면은 텍스트를 보여 준다)."""
    return [new_data_part(data), Part(text=summary)]


# --- 서버 쪽 ---------------------------------------------------------------

@dataclass
class Outcome:
    state: str
    data: dict
    summary: str


Handler = Callable[[dict], Awaitable[Outcome]]


class SkillExecutor(AgentExecutor):
    def __init__(self, handlers: dict[str, Handler], default_skill: str | None = None,
                 text_to_payload: Callable[[str], dict] | None = None, name: str = "agent") -> None:
        self.handlers = handlers
        self.default_skill = default_skill
        self.text_to_payload = text_to_payload
        self.name = name

    def _parse(self, context: RequestContext) -> tuple[str | None, dict]:
        dicts = [d for d in get_data_parts(context.message.parts) if isinstance(d, dict)]
        if dicts:
            payload = dict(dicts[0])
            return payload.pop("skill", self.default_skill), payload
        text = context.get_user_input().strip()
        if self.text_to_payload is None:
            return self.default_skill, {"text": text}
        return self.default_skill, self.text_to_payload(text)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is None:  # INPUT_REQUIRED 뒤 이어 보낸 메시지는 기존 Task에 붙는다
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.update_status(TaskState.TASK_STATE_WORKING)

        skill, payload = self._parse(context)
        handler = self.handlers.get(skill or "")
        if handler is None:
            outcome = Outcome(REJECTED, {"reason": "unknown_skill", "detail": f"지원하지 않는 스킬: {skill}"},
                              f"지원하지 않는 스킬입니다: {skill}")
        else:
            try:
                outcome = await handler(payload)
            except ValidationError as e:
                detail = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
                outcome = Outcome(REJECTED, {"reason": "invalid_request", "detail": detail},
                                  f"요청 형식 오류: {detail}")
            except Exception as e:  # 업무 코드의 예상 못 한 오류 — 사유를 남겨 FAILED로 끝낸다(E8)
                print(f"[{self.name}] {skill} 처리 실패: {e!r}")
                outcome = Outcome(FAILED, {"reason": "internal_error", "detail": str(e)},
                                  f"내부 처리 오류: {e}")

        parts = data_and_text(outcome.data, outcome.summary)
        if outcome.state == COMPLETED:
            await updater.add_artifact(parts=parts, name=skill or "result")
            await updater.complete()
        else:
            await updater.update_status(_STATE[outcome.state], message=updater.new_agent_message(parts))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("Task 취소 대신 cancel-shipment 스킬을 사용합니다")


# --- 클라이언트 쪽 ---------------------------------------------------------

class PartnerUnavailable(Exception):
    """상대 에이전트에 닿지 못했거나 시간 안에 답이 없다. 같은 request_id로 다시 보내면 된다(E4·E5)."""


class SkillNotOffered(Exception):
    """상대 Agent Card에 그 스킬이 없다."""


@dataclass
class Reply:
    state: str
    data: dict
    text: str
    task_id: str = ""
    context_id: str = ""
    raw: list = field(default_factory=list)


async def fetch_card(url: str) -> AgentCard:
    async with httpx.AsyncClient(timeout=5) as http:
        return await A2ACardResolver(http, url).get_agent_card()


async def _receive(client, request) -> Reply:
    state, task_id, context_id = None, "", ""
    status_message: Message | None = None
    artifacts: dict = {}
    immediate: Message | None = None
    stream = client.send_message(request)
    try:
        async for response in stream:
            kind = response.WhichOneof("payload")
            if kind == "message":
                immediate = deepcopy(response.message)
                continue
            item = getattr(response, kind)
            task_id = item.id if kind == "task" else item.task_id
            context_id = item.context_id
            if kind == "task":
                state = item.status.state
                status_message = deepcopy(item.status.message)
                artifacts = {a.artifact_id: deepcopy(a) for a in item.artifacts}
            elif kind == "status_update":
                state = item.status.state
                status_message = deepcopy(item.status.message)
                if state in (TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED):
                    break
            else:
                art = item.artifact
                if item.append and art.artifact_id in artifacts:
                    artifacts[art.artifact_id].parts.extend(art.parts)
                else:
                    artifacts[art.artifact_id] = deepcopy(art)
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()

    if immediate is not None and not task_id:
        parts = list(immediate.parts)
        name = COMPLETED
    else:
        name = TaskState.Name(state).removeprefix("TASK_STATE_") if state is not None else FAILED
        if name == COMPLETED:
            parts = [p for a in artifacts.values() for p in a.parts]
        else:
            parts = list(status_message.parts) if status_message is not None else []
    raw = get_data_parts(parts)
    data = next((d for d in raw if isinstance(d, dict)), {})
    return Reply(name, data, part_text(parts), task_id, context_id, raw)


async def call_skill(url: str, skill: str, payload: dict, *, task_id: str = "", context_id: str = "",
                     timeout: float = 15.0) -> Reply:
    try:
        card = await fetch_card(url)
    except Exception as e:
        raise PartnerUnavailable(f"{url} Agent Card 조회 실패: {e}") from e
    if skill not in [s.id for s in card.skills]:
        raise SkillNotOffered(f"{card.name}에 '{skill}' 스킬이 없습니다")

    message = Message(message_id=str(uuid.uuid4()), role=Role.ROLE_USER, task_id=task_id,
                      context_id=context_id, parts=data_and_text({"skill": skill, **payload}, f"{skill} 요청"))
    try:
        client = await create_client(card)
        return await asyncio.wait_for(_receive(client, SendMessageRequest(message=message)), timeout)
    except TimeoutError as e:
        raise PartnerUnavailable(f"{card.name} 응답 대기시간 {timeout:.0f}초 초과") from e
    except (httpx.HTTPError, OSError) as e:
        raise PartnerUnavailable(f"{card.name} 연결 실패: {e}") from e
    except Exception as e:
        if "connect" in str(e).lower() or "timeout" in str(e).lower():
            raise PartnerUnavailable(f"{card.name} 연결 실패: {e}") from e
        raise


async def call_text(url: str, text: str, timeout: float = 15.0) -> Reply:
    """기존 실습(call_router.py)처럼 텍스트 한 줄만 보낸다."""
    client = await create_client(url)
    message = Message(message_id=str(uuid.uuid4()), role=Role.ROLE_USER, parts=[Part(text=text)])
    return await asyncio.wait_for(_receive(client, SendMessageRequest(message=message)), timeout)


def build_a2a_app(agent_card: AgentCard, executor: AgentExecutor):
    """Agent Card + JSON-RPC 경로를 가진 FastAPI 앱. 실행은 uvicorn.run(app, host=..., port=...)로 한다
    (obs_run.py가 키워드 인자에서 주소를 읽는다)."""
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes.agent_card_routes import create_agent_card_routes
    from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
    from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
    from fastapi import FastAPI

    handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=agent_card)
    app = FastAPI()
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(agent_card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/"),
    )
    return app
