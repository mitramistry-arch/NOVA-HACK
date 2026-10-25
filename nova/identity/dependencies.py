"""FastAPI dependencies that resolve the caller's identity and authorization server-side."""

from typing import Annotated

from fastapi import Depends, Request

from nova.identity.errors import IdentityConfigError
from nova.identity.models import Principal
from nova.identity.roles import Permission
from nova.identity.service import IdentityService


def get_identity_service() -> IdentityService:
    """Return the deployment's identity service, or fail closed when browser auth is off."""
    from nova.api.main import get_runtime

    service = get_runtime().identity
    if service is None:
        raise IdentityConfigError()
    return service


def require_session(
    request: Request,
    service: Annotated[IdentityService, Depends(get_identity_service)],
) -> Principal:
    """Resolve the authenticated principal from the session cookie alone."""
    return service.authenticate(request).principal


def require_role(permission: Permission):  # type: ignore[no-untyped-def]
    """Build a dependency that requires the authenticated principal to hold a permission."""

    def dependency(
        principal: Annotated[Principal, Depends(require_session)],
        service: Annotated[IdentityService, Depends(get_identity_service)],
    ) -> Principal:
        service.require(principal, permission)
        return principal

    dependency.__name__ = f"require_{permission.value.replace('.', '_')}"
    return dependency


__all__ = ["get_identity_service", "require_role", "require_session"]
