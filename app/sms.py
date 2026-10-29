"""SMS adapter contract. No provider is bundled or implicitly enabled.

Set FDE_SMS_ADAPTER=package.module:factory to an installed adapter factory.
An adapter implements send(task_id, phone, text) and returns success/failure/timeout.
The task_id must be the provider idempotency key. Exceptions are unknown outcomes.
"""
import importlib
import os
from typing import Protocol


class SMSAdapter(Protocol):
    def send(self, task_id: str, phone: str, text: str) -> str: ...


def configured_adapter() -> SMSAdapter:
    path = os.environ.get('FDE_SMS_ADAPTER', '')
    if not path or ':' not in path:
        raise ValueError('真实短信通道尚未配置')
    module, factory = path.split(':', 1)
    return getattr(importlib.import_module(module), factory)()
