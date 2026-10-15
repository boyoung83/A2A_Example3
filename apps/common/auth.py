# 파일명: auth.py
"""회사 간 A2A 인증 (Ch18 방식: 계약 파트너 API Key를 헤더 X-Partner-Key에 싣는다).

서버 쪽
  - ApiKeyContextBuilder: 헤더의 키를 발급 목록과 비교해 ServerCallContext.user(파트너 이름)를 채운다.
  - SkillExecutor(auth_required=True)가 인증 안 된 메시지를 REJECTED {"reason": "unauthenticated"}로 끝낸다.
  - AuthRequestHandler: 메시지 말고 tasks/get·cancel·subscribe·list도 인증 없이는 막는다.
    (SDK의 Task 저장소는 사용자 이름별로 Task를 나눠 두므로, 다른 파트너의 Task도 보이지 않는다.)
  - card_security(): Agent Card에 "이 에이전트는 X-Partner-Key가 필요하다"고 적는다.
클라이언트 쪽
  - 상대에게 받은 키를 call_skill(..., api_key=...)로 보낸다.

키 보관 (실제 운영처럼 코드에 키를 적지 않는다)
  서버(발급한 쪽): 회사 DB의 키 장부(keystore.py) — 해시만 저장, 요청마다 확인, 교체·폐기 즉시 반영.
       장부가 처음 비어 있으면 KL_PARTNER_KEYS / KP_CLIENT_KEYS("이름=키,…") 또는 secrets.json에서 들여온다.
  클라이언트(받은 쪽): KL_API_KEY / KP_API_KEY 환경변수, 없으면 로컬 개발용 apps/data/secrets.json.
       요청할 때마다 다시 읽으므로, 교체한 키를 전달(deliver)하면 재시작 없이 새 키를 쓴다.
  secrets.json(처음 실행 때 무작위 키로 생성, git 제외)을 두 회사가 같이 읽는 것은
  "키를 발급해 전달했다"를 한 PC에서 흉내 낸 것이다.
"""

import json
import os
import secrets
from pathlib import Path

from a2a.auth.user import UnauthenticatedUser, User
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.types.a2a_pb2 import APIKeySecurityScheme, SecurityRequirement, SecurityScheme, StringList
from a2a.utils.errors import TaskNotFoundError
from starlette.requests import Request

from common.db import DATA_DIR

API_KEY_HEADER = "X-Partner-Key"
SECRETS_FILE = Path(os.getenv("A2A_SECRETS_FILE") or DATA_DIR / "secrets.json")

# 개발용 비밀 파일의 구역: (서버 키 구역, 기본 파트너)
KL_PARTNERS = "klogistics_partner_keys"   # K-Logistics가 발급한 키: 화주 이름 → 키
KP_CLIENTS = "kprecision_client_keys"     # K정밀웍스 구매 에이전트가 발급한 키: 사내 시스템 이름 → 키
DEFAULT_ISSUED = {KL_PARTNERS: ["K정밀웍스"], KP_CLIENTS: ["사내 시스템"]}


def _new_key(prefix: str) -> str:
    return f"{prefix}-{secrets.token_urlsafe(18)}"


def load_secrets() -> dict:
    """apps/data/secrets.json 을 읽는다. 없으면 무작위 키로 만든다."""
    if SECRETS_FILE.exists():
        data = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
    else:
        data = {}
    changed = False
    for section, names in DEFAULT_ISSUED.items():
        bucket = data.setdefault(section, {})
        for name in names:
            if name not in bucket:
                bucket[name] = _new_key("kl" if section == KL_PARTNERS else "kp")
                changed = True
    if changed:
        SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRETS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _parse_env(value: str) -> dict[str, str]:
    pairs = (item.split("=", 1) for item in value.split(",") if "=" in item)
    return {name.strip(): key.strip() for name, key in pairs if name.strip() and key.strip()}


def deliver(section: str, name: str, key: str) -> Path:
    """(개발용) 새로 발급한 키를 받는 쪽 키 파일에 넣어 '전달'한다. 실제로는 안전한 별도 경로로 건넨다."""
    data = load_secrets()
    data.setdefault(section, {})[name] = key
    SECRETS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return SECRETS_FILE


def client_key(env_var: str, section: str, name: str) -> str:
    """클라이언트가 상대에게 보낼 자기 키."""
    if os.getenv(env_var):
        return os.environ[env_var]
    return load_secrets()[section].get(name, "")


# --- 서버 쪽 -----------------------------------------------------------------

class PartnerUser(User):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self._name


class ApiKeyContextBuilder(DefaultServerCallContextBuilder):
    """요청 헤더의 API Key를 키 장부로 확인해 ServerCallContext.user를 채운다(Ch18).
    verify: 키 → 파트너 이름(유효) 또는 None. 장부를 요청마다 보므로 교체·폐기가 바로 반영된다."""

    def __init__(self, verify) -> None:
        super().__init__()
        self.verify = verify

    def build_user(self, request: Request) -> User:
        name = self.verify(request.headers.get(API_KEY_HEADER, ""))
        return PartnerUser(name) if name else UnauthenticatedUser()


class AuthRequestHandler(DefaultRequestHandler):
    """메시지 외의 메서드(tasks/get·list·cancel·subscribe)도 인증 없이는 '없는 Task'로 답한다."""

    @staticmethod
    def _require(context: ServerCallContext | None) -> None:
        if context is None or not context.user.is_authenticated:
            raise TaskNotFoundError

    async def on_get_task(self, params, context):
        self._require(context)
        return await super().on_get_task(params, context)

    async def on_list_tasks(self, params, context):
        self._require(context)
        return await super().on_list_tasks(params, context)

    async def on_cancel_task(self, params, context):
        self._require(context)
        return await super().on_cancel_task(params, context)

    async def on_subscribe_to_task(self, params, context):
        self._require(context)
        async for event in super().on_subscribe_to_task(params, context):
            yield event


def card_security(description: str) -> dict:
    """AgentCard(**card_security(...)) — 카드에 API Key 요구를 밝힌다."""
    return {
        "security_schemes": {
            "apiKey": SecurityScheme(api_key_security_scheme=APIKeySecurityScheme(
                name=API_KEY_HEADER, location="header", description=description))
        },
        "security_requirements": [SecurityRequirement(schemes={"apiKey": StringList(list=[])})],
    }


def requires_api_key(card) -> bool:
    return any("apiKey" in req.schemes for req in card.security_requirements)
