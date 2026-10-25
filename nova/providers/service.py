"""Tenant provider lifecycle: configure, read back (masked), rotate, test, disconnect, resolve."""

import logging
import time

import httpx

from nova.core.config import Settings
from nova.core.llm import FakeLLM, LLMClient
from nova.providers.capabilities import ProviderCapability, capabilities_for, require
from nova.providers.config import (
    PROVIDER_KEY_SECRET,
    ConnectionTestResult,
    ProviderConfigRequest,
    ProviderSettings,
    ProviderStatus,
    parse_stored,
)
from nova.providers.errors import ProviderConfigError, ProviderError, ProviderNotConfiguredError
from nova.providers.registry import build_adapter
from nova.security.net import UnsafeEndpointError, validate_https_url
from nova.security.secrets import ControlStore, SecretNotFoundError

logger = logging.getLogger("nova.providers.service")
_KIND = "provider"
_TEST_PROMPT = "Reply with the single word: ok"


class ProviderService:
    """Manages one deployment's tenant provider configurations."""

    def __init__(
        self,
        control: ControlStore,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Bind to the control store; ``transport`` lets tests inject a mock HTTP transport."""
        self.control = control
        self.settings = settings
        self._transport = transport

    def _check_endpoint(self, cfg: ProviderSettings) -> None:
        try:
            validate_https_url(
                cfg.effective_base_url,
                allow_private=self.settings.allow_private_endpoints,
                resolve=self._transport is None,
            )
        except UnsafeEndpointError as e:
            raise ProviderConfigError(f"unsafe provider endpoint: {e}") from e

    def status(self, tenant_id: str) -> ProviderStatus:
        """Return the masked configuration (never the API key)."""
        body = self.control.load_config(tenant_id, _KIND)
        if body is None:
            return ProviderStatus(configured=False)
        cfg = parse_stored(body)
        mask = self.control.secret_mask(tenant_id, PROVIDER_KEY_SECRET)
        return ProviderStatus(
            configured=True,
            kind=cfg.kind,
            model=cfg.model,
            base_url=cfg.effective_base_url,
            timeout_s=cfg.timeout_s,
            max_output_tokens=cfg.max_output_tokens,
            max_prompt_tokens=cfg.max_prompt_tokens,
            has_api_key=mask is not None,
            api_key_mask=mask,
            capabilities=sorted(capability.value for capability in capabilities_for(cfg.kind)),
        )

    def configure(self, tenant_id: str, req: ProviderConfigRequest) -> ProviderStatus:
        """Create or update the configuration; a supplied api_key rotates the stored key."""
        cfg = req.settings_only()
        self._check_endpoint(cfg)
        has_key = self.control.secret_mask(tenant_id, PROVIDER_KEY_SECRET) is not None
        if req.api_key is None and not has_key:
            raise ProviderConfigError("api_key is required when configuring a provider")
        if req.api_key is not None:
            self.control.put_secret(tenant_id, PROVIDER_KEY_SECRET, req.api_key.get_secret_value())
        self.control.save_config(tenant_id, _KIND, cfg.model_dump(mode="json", exclude_none=True))
        return self.status(tenant_id)

    def disconnect(self, tenant_id: str) -> bool:
        """Delete the configuration and the stored key; returns True if anything was removed."""
        removed_cfg = self.control.delete_config(tenant_id, _KIND)
        removed_key = self.control.delete_secret(tenant_id, PROVIDER_KEY_SECRET)
        return removed_cfg or removed_key

    def build_llm(self, tenant_id: str) -> LLMClient:
        """Return the tenant's LLM client, or FakeLLM only when explicitly allowed.

        A tenant without a configuration gets ProviderNotConfiguredError unless
        NOVA_ALLOW_FAKE_LLM is set; a configured tenant never falls back to FakeLLM.
        """
        body = self.control.load_config(tenant_id, _KIND)
        if body is None:
            if self.settings.allow_fake_llm:
                logger.warning("using FakeLLM for tenant (NOVA_ALLOW_FAKE_LLM is enabled)")
                return FakeLLM()
            raise ProviderNotConfiguredError("tenant has no provider configuration")
        cfg = parse_stored(body)
        try:
            key = self.control.get_secret(tenant_id, PROVIDER_KEY_SECRET)
        except SecretNotFoundError as e:
            raise ProviderNotConfiguredError("provider api key is missing") from e
        require(cfg.kind, ProviderCapability.GENERATION)
        return build_adapter(
            cfg,
            key,
            allow_private=self.settings.allow_private_endpoints,
            transport=self._transport,
        )

    def test(
        self, tenant_id: str, candidate: ProviderConfigRequest | None = None
    ) -> ConnectionTestResult:
        """Make one tiny completion with the stored or candidate config and report a safe result.

        A candidate's key is used for this call only and is never stored.
        """
        started = time.perf_counter()
        llm: LLMClient | None = None
        try:
            if candidate is not None:
                cfg = candidate.settings_only()
                self._check_endpoint(cfg)
                if candidate.api_key is not None:
                    key = candidate.api_key.get_secret_value()
                else:
                    key = self.control.get_secret(tenant_id, PROVIDER_KEY_SECRET)
            else:
                body = self.control.load_config(tenant_id, _KIND)
                if body is None:
                    raise ProviderNotConfiguredError("tenant has no provider configuration")
                cfg = parse_stored(body)
                key = self.control.get_secret(tenant_id, PROVIDER_KEY_SECRET)
            require(cfg.kind, ProviderCapability.GENERATION)
            llm = build_adapter(
                cfg,
                key,
                allow_private=self.settings.allow_private_endpoints,
                transport=self._transport,
                max_output_tokens=16,
            )
            llm.complete(_TEST_PROMPT)
        except SecretNotFoundError:
            return ConnectionTestResult(
                ok=False,
                code=ProviderNotConfiguredError.code,
                message=ProviderNotConfiguredError.public_message,
            )
        except ProviderError as e:
            logger.info("provider test failed: %s", e.code)
            return ConnectionTestResult(ok=False, code=e.code, message=e.public_message)
        finally:
            closer = getattr(llm, "close", None)
            if callable(closer):
                closer()
        return ConnectionTestResult(
            ok=True,
            code="ok",
            message="Connection succeeded.",
            latency_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
