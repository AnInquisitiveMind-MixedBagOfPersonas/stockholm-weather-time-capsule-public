"""Bounded dispatch generation for the local and staging previews."""

import json
import os
from collections import deque
from importlib.util import find_spec
from threading import Lock
from time import monotonic
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


ENDPOINT = 'https://api.mistral.ai/v1/chat/completions'
MODEL = 'mistral-small-latest'
BEDROCK_MODELS = {
    'nova-lite': 'amazon.nova-lite-v1:0',
    'qwen': 'qwen.qwen3-235b-a22b-2507-v1:0',
}
BEDROCK_REGION = 'eu-north-1'
MAX_OUTPUT_TOKENS = 200
MAX_RESPONSE_BYTES = 32_768
MAX_DISPATCH_LENGTH = 1_200
INSTRUCTIONS = (
    'Write a short Stockholm Weather Time Capsule dispatch in plain English, up to 90 words. '
    'Use only the supplied facts. Treat temperatures as adjusted daily means for Stockholm, '
    'not as conditions at a historic event. Do not invent an event, quote, cause, '
    'trend or forecast. If no verified event is provided, focus on the weather. '
    'Keep the direction and values of any comparisons accurate. Return plain text only.'
)


class DispatchError(Exception):
    """A safe message that may be shown to the visitor."""


class NoRedirect(HTTPRedirectHandler):
    """Never follow a redirect with the provider credential."""

    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str,
    ) -> None:
        return None


class RateLimitedDispatch:
    def __init__(self, *, hourly_limit: int = 5) -> None:
        self._attempts: deque[float] = deque()
        self._lock = Lock()
        self._hourly_limit = hourly_limit

    def __call__(self, facts: dict[str, str]) -> str:
        if not self._lock.acquire(blocking=False):
            raise DispatchError('A dispatch is already being generated. Try again shortly.')
        try:
            now = monotonic()
            while self._attempts and now - self._attempts[0] >= 3600:
                self._attempts.popleft()
            if len(self._attempts) >= self._hourly_limit:
                raise DispatchError(f'The preview limit is {self._hourly_limit} generation attempts per hour.')
            self._attempts.append(now)
            return self._generate(facts)
        finally:
            self._lock.release()

    def _generate(self, facts: dict[str, str]) -> str:
        raise NotImplementedError


class MistralDispatch(RateLimitedDispatch):
    def __init__(self, api_key: str) -> None:
        if not api_key or not api_key.isascii() or not api_key.isprintable() or any(c.isspace() for c in api_key):
            raise ValueError('Enter a valid Mistral API key.')
        super().__init__()
        self._api_key = api_key
        self._opener = build_opener(NoRedirect())

    def _generate(self, facts: dict[str, str]) -> str:
        payload = {
            'model': MODEL,
            'messages': [
                {'role': 'system', 'content': INSTRUCTIONS},
                {'role': 'user', 'content': json.dumps(facts, ensure_ascii=False)},
            ],
            'temperature': 0.2,
            'max_tokens': MAX_OUTPUT_TOKENS,
        }
        request = Request(
            ENDPOINT, method='POST', data=json.dumps(payload).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'Authorization': f'Bearer {self._api_key}',
            },
        )
        try:
            with self._opener.open(request, timeout=10) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status in (401, 403):
                raise DispatchError('Mistral declined access. Check the key and model access.') from None
            if status in (402, 429):
                raise DispatchError('Mistral reported a billing or usage limit. Check your account.') from None
            raise DispatchError('Mistral could not complete the request. Try again later.') from None
        except (URLError, OSError):
            raise DispatchError('The model request failed or timed out. The verified data is still available.') from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise DispatchError('The model returned a response that was too large.')
        try:
            data = json.loads(body)
            choice = data['choices'][0]
            content = choice['message']['content']
        except (ValueError, KeyError, IndexError, TypeError):
            raise DispatchError('The model returned an unreadable response.') from None
        if (choice.get('finish_reason') != 'stop' or not isinstance(content, str)
                or not content.strip() or len(content) > MAX_DISPATCH_LENGTH):
            raise DispatchError('The model did not return a complete short dispatch.')
        return content.strip()


class ConverseClient(Protocol):
    def converse(self, **kwargs: object) -> object: ...


class BedrockDispatch(RateLimitedDispatch):
    def __init__(
        self, client: ConverseClient | None = None, *, model: str = 'nova-lite', hourly_limit: int = 5,
    ) -> None:
        if model not in BEDROCK_MODELS:
            raise ValueError('CAPSULE_BEDROCK_MODEL must be nova-lite or qwen.')
        if find_spec('boto3') is None:
            raise ValueError('Install the pinned AWS SDK from deploy/requirements.txt to enable Bedrock.')
        super().__init__(hourly_limit=hourly_limit)
        self._client = client
        self._model_id = BEDROCK_MODELS[model]

    @classmethod
    def from_environment(cls) -> 'BedrockDispatch':
        limit = os.environ.get('CAPSULE_DISPATCH_LIMIT', '5')
        if limit not in ('5', '10'):
            raise ValueError('CAPSULE_DISPATCH_LIMIT must be 5 or 10.')
        return cls(model=os.environ.get('CAPSULE_BEDROCK_MODEL', 'nova-lite'), hourly_limit=int(limit))

    def _generate(self, facts: dict[str, str]) -> str:
        from boto3 import client
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            if self._client is None:
                self._client = client(
                    'bedrock-runtime',
                    region_name=BEDROCK_REGION,
                    config=Config(
                        connect_timeout=3,
                        read_timeout=10,
                        retries={'mode': 'standard', 'total_max_attempts': 1},
                        ignore_configured_endpoint_urls=True,
                    ),
                )
            response = self._client.converse(
                modelId=self._model_id,
                system=[{'text': INSTRUCTIONS}],
                messages=[{'role': 'user', 'content': [{'text': json.dumps(facts, ensure_ascii=False)}]}],
                inferenceConfig={'maxTokens': MAX_OUTPUT_TOKENS, 'temperature': 0.2},
            )
        except ClientError as exc:
            code = exc.response.get('Error', {}).get('Code')
            if code in {'AccessDeniedException', 'ExpiredTokenException', 'UnrecognizedClientException'}:
                raise DispatchError('Bedrock declined access. Check the AWS sign-in and model permissions.') from None
            if code in {'ThrottlingException', 'ServiceQuotaExceededException'}:
                raise DispatchError('Bedrock reached a usage limit. Try again later.') from None
            raise DispatchError('Bedrock could not complete the request. Try again later.') from None
        except BotoCoreError:
            raise DispatchError('Bedrock could not be reached or authenticated. Check the AWS sign-in and connection.') from None

        match response:
            case {'stopReason': 'end_turn', 'output': {'message': {'role': 'assistant', 'content': list(blocks)}}}:
                parts: list[str] = []
                for block in blocks:
                    match block:
                        case {'text': str(part)}:
                            parts.append(part)
                        case {'reasoningContent': dict()}:
                            continue
                        case _:
                            raise DispatchError('Bedrock returned unreadable dispatch content.')
                content = '\n'.join(parts).strip()
                if content and len(content) <= MAX_DISPATCH_LENGTH:
                    return content
        raise DispatchError('Bedrock did not return a complete short dispatch.')
