"""Tenant identifier rules shared by the API, storage, and secret layers."""

import re

DEFAULT_TENANT = "default"
_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class InvalidTenantError(ValueError):
    """Raised when a tenant identifier is malformed."""


def validate_tenant_id(tenant_id: str) -> str:
    """Return the tenant id if it is 1-64 safe characters, else raise InvalidTenantError."""
    if not isinstance(tenant_id, str) or not _TENANT_RE.fullmatch(tenant_id):
        raise InvalidTenantError("tenant id must be 1-64 characters of letters, digits, _ . -")
    return tenant_id
