"""Roles and the granular permissions they grant, independent of any identity vendor."""

from collections.abc import Mapping
from enum import StrEnum


class Role(StrEnum):
    """Workspace role assigned to a browser principal."""

    VIEWER = "viewer"
    EDITOR = "editor"
    ADMIN = "admin"


class Permission(StrEnum):
    """A single authorized operation; routes ask for these, never for a role name."""

    QUERY = "query"
    INGEST = "ingest"
    AUDIT_READ = "audit.read"
    PROVIDER_MANAGE = "provider.manage"
    STORAGE_MANAGE = "storage.manage"
    DATA_EXPORT = "data.export"
    DATA_DELETE = "data.delete"


ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset({Permission.QUERY, Permission.AUDIT_READ}),
    Role.EDITOR: frozenset({Permission.QUERY, Permission.AUDIT_READ, Permission.INGEST}),
    Role.ADMIN: frozenset(Permission),
}


def parse_role(value: str) -> Role:
    """Parse a configured role name, rejecting anything else."""
    try:
        return Role(value.strip().lower())
    except ValueError as e:
        allowed = ", ".join(role.value for role in Role)
        raise ValueError(f"role must be one of: {allowed}") from e


def permissions_for(role: Role) -> frozenset[Permission]:
    """Return the permissions granted by a role."""
    return ROLE_PERMISSIONS[role]


def role_allows(role: Role, permission: Permission) -> bool:
    """Return True when the role grants the permission."""
    return permission in ROLE_PERMISSIONS[role]


__all__ = ["ROLE_PERMISSIONS", "Permission", "Role", "parse_role", "permissions_for", "role_allows"]
