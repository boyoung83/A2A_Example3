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


Handler = Callable[[dict, str], Awaitable[Outcome]]  # (payload, 호출한 파트너 이름)


class SkillExecutor(AgentExecutor):
    def __init__(self, handlers: dict[str, Handler], default_skill: str | None = None,
                 text_to_payload: Callable[[str], dict] | None = None, name: str = "agent",
                 auth_required: bool = False) -> None:
        self.handlers = handlers
        self.default_skill = default_skill
        self.text_to_payload = text_to_payload
        self.name = name
        self.auth_required = auth_required

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

        user = context.call_context.user if context.call_context else None
        caller = user.user_name if user is not None and user.is_authenticated else ""
        if self.auth_required and not caller:
            # 인증 실패는 일을 시작하기 전에 거절한다(Ch18). 어떤 키가 틀렸는지는 알려 주지 않는다.
            print(f"[{self.name}] 인증되지 않은 요청 거부")
            parts = data_and_text({"reason": "unauthenticated", "detail": "유효한 파트너 키가 필요합니다"},
                                  "거절: 인증되지 않은 요청입니다 (X-Partner-Key)")
            await updater.update_status(TaskState.TASK_STATE_REJECTED, message=updater.new_agent_message(parts))
            return
        await updater.update_status(TaskState.TASK_STATE_WORKING)

        skill, payload = self._parse(context)
        if self.auth_required:
            print(f"[{self.name}] 인증 통과: {caller} → {skill}")
        handler = self.handlers.get(skill or "")
        if handler is None:
            outcome = Outcome(REJECTED, {"reason": "unknown_skill", "detail": f"지원하지 않는 스킬: {skill}"},
                              f"지원하지 않는 스킬입니다: {skill}")
        else:
            try:
                outcome = await handler(payload, caller)
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


class MissingCredentials(Exception):
    """상대 Agent Card가 API Key를 요구하는데 보낼 키가 없다."""


async def _send(card: AgentCard, message: Message, api_key: str, timeout: float) -> Reply:
    """키가 있으면 X-Partner-Key 헤더를 단 httpx 클라이언트로 보낸다(Ch18 call_secure.py 방식)."""
    from a2a.client import ClientConfig

    from common.auth import API_KEY_HEADER

    headers = {API_KEY_HEADER: api_key} if api_key else {}
    async with httpx.AsyncClient(headers=headers, timeout=timeout) as http:
        client = await create_client(card, client_config=ClientConfig(httpx_client=http))
        return await asyncio.wait_for(_receive(client, SendMessageRequest(message=message)), timeout)


async def call_skill(url: str, skill: str, payload: dict, *, task_id: str = "", context_id: str = "",
                     timeout: float = 15.0, api_key: str = "") -> Reply:
    from common.auth import requires_api_key

    try:
        card = await fetch_card(url)
    except Exception as e:
        raise PartnerUnavailable(f"{url} Agent Card 조회 실패: {e}") from e
    if skill not in [s.id for s in card.skills]:
        raise SkillNotOffered(f"{card.name}에 '{skill}' 스킬이 없습니다")
    if requires_api_key(card) and not api_key:
        raise MissingCredentials(f"{card.name}은(는) 파트너 키(X-Partner-Key)가 필요한데 설정된 키가 없습니다")

    message = Message(message_id=str(uuid.uuid4()), role=Role.ROLE_USER, task_id=task_id,
                      context_id=context_id, parts=data_and_text({"skill": skill, **payload}, f"{skill} 요청"))
    try:
        # 키는 상대 카드가 요구할 때만 보낸다 — 키를 요구하지 않는 곳에 남의 회사 키를 흘리지 않는다
        return await _send(card, message, api_key if requires_api_key(card) else "", timeout)
    except TimeoutError as e:
        raise PartnerUnavailable(f"{card.name} 응답 대기시간 {timeout:.0f}초 초과") from e
    except (httpx.HTTPError, OSError) as e:
        raise PartnerUnavailable(f"{card.name} 연결 실패: {e}") from e
    except Exception as e:
        if "connect" in str(e).lower() or "timeout" in str(e).lower():
            raise PartnerUnavailable(f"{card.name} 연결 실패: {e}") from e
        raise


async def call_text(url: str, text: str, timeout: float = 15.0, api_key: str = "") -> Reply:
    """기존 실습(call_router.py)처럼 텍스트 한 줄만 보낸다."""
    from common.auth import requires_api_key

    card = await fetch_card(url)
    message = Message(message_id=str(uuid.uuid4()), role=Role.ROLE_USER, parts=[Part(text=text)])
    return await _send(card, message, api_key if requires_api_key(card) else "", timeout)


def build_a2a_app(agent_card: AgentCard, executor: AgentExecutor, verify: Callable[[str], str | None] | None = None):
    """Agent Card + JSON-RPC 경로를 가진 FastAPI 앱. 실행은 uvicorn.run(app, host=..., port=...)로 한다
    (obs_run.py가 키워드 인자에서 주소를 읽는다).
    verify(키 → 파트너 이름 또는 None, 보통 KeyRegistry.verify)를 주면 X-Partner-Key로 호출자를 확인한다.
    Agent Card 조회는 인증 없이 열어 둔다."""
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes.agent_card_routes import create_agent_card_routes
    from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
    from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
    from fastapi import FastAPI

    from common.auth import ApiKeyContextBuilder, AuthRequestHandler

    handler_cls = AuthRequestHandler if verify is not None else DefaultRequestHandler
    handler = handler_cls(agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=agent_card)
    builder = ApiKeyContextBuilder(verify) if verify is not None else None
    app = FastAPI()
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(agent_card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/", context_builder=builder),
    )
    return app
