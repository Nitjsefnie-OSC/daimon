"""Single-attempt API calls; durable recruitment service owns retry policy."""

import math
from uuid import UUID

import httpx
from daimon.core.recruitment_api_models import MatchResponse, RegistrationResponse, UpdateResponse
from pydantic import BaseModel, JsonValue, SecretStr, ValidationError

_RETRYABLE_CODES = frozenset(
    {
        "request_in_progress",
        "resource_busy",
        "notion_rate_limited",
        "notion_unavailable",
        "internal_error",
    }
)
_KNOWN_CODES = _RETRYABLE_CODES | frozenset(
    {
        "validation_error",
        "unauthorized",
        "recruitment_disabled",
        "payload_too_large",
        "idempotency_key_required",
        "idempotency_key_reuse",
        "thread_not_linked",
        "candidate_not_found",
        "thread_candidate_mismatch",
        "thread_already_linked",
        "candidate_match_conflict",
    }
)


class RecruitmentAPIError(Exception):
    """Safe operational metadata only; excludes upstream messages and bodies."""

    def __init__(
        self,
        code: str,
        retryable: bool = False,
        request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"Recruitment API error: {code}")
        self.code = code
        self.retryable = retryable
        self.request_id = request_id
        self.retry_after = retry_after


class _ErrorResponse(BaseModel):
    code: str
    retryable: bool = False
    request_id: str | None = None


def _response_error(response: httpx.Response) -> RecruitmentAPIError:
    try:
        body = _ErrorResponse.model_validate_json(response.content)
    except ValidationError:
        return RecruitmentAPIError("invalid_response")
    code = body.code if body.code in _KNOWN_CODES else "unknown_error"
    request_id = body.request_id
    if request_id is not None and (
        len(request_id) > 128
        or not all(c.isascii() and (c.isalnum() or c in "-_") for c in request_id)
    ):
        request_id = None
    retry_after = None
    try:
        delay = float(response.headers.get("Retry-After", ""))
        if math.isfinite(delay) and delay >= 0:
            retry_after = delay
    except ValueError:
        pass
    return RecruitmentAPIError(
        code, body.retryable and code in _RETRYABLE_CODES, request_id, retry_after
    )


async def _send[Response: BaseModel](
    client: httpx.AsyncClient,
    base_url: str,
    api_key: SecretStr,
    path: str,
    payload: dict[str, JsonValue],
    response_model: type[Response],
    idempotency_key: str | None = None,
) -> Response:
    headers = {"Authorization": f"Bearer {api_key.get_secret_value()}"}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    try:
        response = await client.post(
            f"{base_url.rstrip('/')}{path}",
            json=payload,
            headers=headers,
            timeout=30.0,
            follow_redirects=False,
        )
    except httpx.TransportError:
        raise RecruitmentAPIError("transport_error", retryable=True) from None
    if not response.is_success:
        raise _response_error(response)
    if response.status_code not in (200, 201):
        raise RecruitmentAPIError("invalid_response")
    try:
        return response_model.model_validate_json(response.content)
    except ValidationError:
        raise RecruitmentAPIError("invalid_response") from None


async def send_match(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: SecretStr,
    payload: dict[str, JsonValue],
) -> MatchResponse:
    return await _send(
        client,
        base_url,
        api_key,
        "/recruitment/candidates/match",
        payload,
        MatchResponse,
    )


async def send_registration(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: SecretStr,
    payload: dict[str, JsonValue],
    idempotency_key: str,
) -> RegistrationResponse:
    return await _send(
        client,
        base_url,
        api_key,
        "/recruitment/registrations",
        payload,
        RegistrationResponse,
        idempotency_key,
    )


async def send_update(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: SecretStr,
    candidate_id: str,
    payload: dict[str, JsonValue],
    idempotency_key: str,
) -> UpdateResponse:
    try:
        candidate_uuid = UUID(candidate_id)
    except ValueError:
        raise RecruitmentAPIError("invalid_candidate_id") from None
    response = await _send(
        client,
        base_url,
        api_key,
        f"/recruitment/candidates/{candidate_uuid}/updates",
        payload,
        UpdateResponse,
        idempotency_key,
    )
    changes = payload.get("changes")
    if not isinstance(changes, list):
        raise RecruitmentAPIError("invalid_response")
    requested_fields: set[str] = set()
    for change in changes:
        if not isinstance(change, dict) or not isinstance(change.get("field"), str):
            raise RecruitmentAPIError("invalid_response")
        field = change["field"]
        if isinstance(field, str):
            requested_fields.add(field)
    if (
        response.candidate_id != candidate_uuid
        or {result.field for result in response.results} != requested_fields
    ):
        raise RecruitmentAPIError("invalid_response")
    return response


class RecruitmentAPI:
    """Service-owned client. A durable caller supplies every write key."""

    def __init__(self, client: httpx.AsyncClient, base_url: str, key: SecretStr) -> None:
        self.client, self.base_url, self.key = client, base_url, key

    async def match(self, payload: dict[str, JsonValue]) -> MatchResponse:
        return await send_match(self.client, self.base_url, self.key, payload)

    async def register(self, payload: dict[str, JsonValue], key: str) -> RegistrationResponse:
        return await send_registration(self.client, self.base_url, self.key, payload, key)

    async def update(
        self, candidate: str, payload: dict[str, JsonValue], key: str
    ) -> UpdateResponse:
        return await send_update(self.client, self.base_url, self.key, candidate, payload, key)


class SyntheticRecruitmentTransport(httpx.AsyncBaseTransport):
    """Opt-in synthetic backend; no sockets, credentials or real candidate effects.

    Retained by the process across ticks. Durable client keys deterministically
    recover fake identities after a process restart as well.
    """

    def __init__(self) -> None:
        self.effects: dict[str, tuple[bytes, dict[str, JsonValue]]] = {}
        self.links: dict[str, str] = {}
        self.fail: int | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        import json
        from uuid import NAMESPACE_URL, uuid5

        if self.fail:
            return httpx.Response(self.fail, json={"code": "notion_unavailable", "retryable": True})
        body = json.loads(request.content)
        scope = request.headers.get("Authorization", "")
        thread = f"{scope}:{body.get('discord_guild_id')}:{body.get('discord_thread_id')}"
        if request.url.path.endswith("/match"):
            candidate = self.links.get(thread)
            return httpx.Response(
                200,
                json={
                    "status": "linked" if candidate else "none",
                    "candidates": [
                        {
                            "candidate_id": candidate,
                            "notion_page_id": candidate,
                            "notion_page_url": f"https://synthetic.invalid/{candidate}",
                            "name": "Synthetic candidate",
                            "match_reasons": ["thread_link"],
                        }
                    ]
                    if candidate
                    else [],
                    "field_options": {},
                },
            )
        key = scope + ":" + request.headers.get("Idempotency-Key", "")
        if key in self.effects:
            content, response = self.effects[key]
            if content != request.content:
                return httpx.Response(409, json={"code": "idempotency_key_reuse"})
            return httpx.Response(200, json=response)
        candidate = str(body.get("candidate_id") or uuid5(NAMESPACE_URL, key))
        response: dict[str, JsonValue]
        if request.url.path.endswith("/updates"):
            candidate = request.url.path.split("/")[-2]
            response = {
                "candidate_id": candidate,
                "notion_page_url": f"https://synthetic.invalid/{candidate}",
                "results": [{"field": c["field"], "result": "applied"} for c in body["changes"]],
            }
        else:
            self.links[thread] = candidate
            response = {
                "candidate_id": candidate,
                "notion_page_url": f"https://synthetic.invalid/{candidate}",
                "outcome": "created",
            }
        self.effects[key] = (request.content, response)
        return httpx.Response(200, json=response)
