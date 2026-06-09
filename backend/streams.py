import secrets
import time
from typing import Optional

STREAM_TTL = 6 * 3600

_tokens: dict[str, dict] = {}


def create_token(url: str, headers: Optional[dict] = None, stream_type: str = 'progressive') -> str:
    token = secrets.token_urlsafe(24)
    _tokens[token] = {
        'url': url,
        'headers': headers or {},
        'stream_type': stream_type,
        'expires': time.time() + STREAM_TTL,
    }
    _cleanup()
    return token


def get_token(token: str) -> Optional[dict]:
    _cleanup()
    return _tokens.get(token)


def _cleanup():
    now = time.time()
    expired = [k for k, v in _tokens.items() if v['expires'] < now]
    for k in expired:
        del _tokens[k]