"""Credential vault.

Managed-agent principle: tokens are never reachable from the sandbox where
generated code runs. The brain/harness references credentials by *name*
only; the vault fetches them at call time and wires them into the tool
invocation (via a proxy), so neither the model nor the sandbox sees raw
secrets.
"""
from __future__ import annotations

import os
from typing import Any


class CredentialVault:
    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        # In production, back this with Azure Key Vault, Managed Identity,
        # or an MCP OAuth broker. For the demo we read from env vars.
        self._secrets: dict[str, str] = dict(secrets or {})

    def register_env(self, logical_name: str, env_var: str) -> None:
        value = os.getenv(env_var)
        if value is not None:
            self._secrets[logical_name] = value

    def resolve(self, logical_name: str) -> str | None:
        """Internal use only — never return raw secrets to the model."""
        return self._secrets.get(logical_name)

    def has(self, logical_name: str) -> bool:
        return logical_name in self._secrets

    # Proxy helper: the brain passes a logical credential handle; this
    # method produces headers/params ready for an outbound call, so the
    # handle never appears in the model's context.
    def build_auth_headers(self, logical_name: str) -> dict[str, str]:
        token = self.resolve(logical_name)
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    def redact(self, value: Any) -> Any:
        s = str(value)
        for secret in self._secrets.values():
            if secret and secret in s:
                s = s.replace(secret, "***REDACTED***")
        return s
