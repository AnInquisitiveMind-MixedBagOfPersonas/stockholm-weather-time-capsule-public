"""Production entry point for a WSGI server."""

import os

from app import default_app
from dispatch import BedrockDispatch

provider = os.environ.get('CAPSULE_PROVIDER', 'off')
if provider not in ('off', 'bedrock'):
    raise RuntimeError('CAPSULE_PROVIDER must be off or bedrock.')

application = default_app(BedrockDispatch.from_environment() if provider == 'bedrock' else None)
