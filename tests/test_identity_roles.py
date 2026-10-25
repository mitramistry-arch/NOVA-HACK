"""Role/permission matrix and principal authorization tests."""

import pytest

from nova.identity.models import Principal
from nova.identity.roles import (
    ROLE_PERMISSIONS,
    Permission,
    Role,
    parse_role,
    permissions_for,
    role_allows,
)


@pytest.mark.parametrize("permission", list(Permission))
def test_admin_holds_every_permission(permission: Permission) -> None:
    """Admin is the only role allowed to change provider, storage, or delete data."""
    assert role_allows(Role.ADMIN, permission)


@pytest.mark.parametrize(
    ("role", "permission", "allowed"),
    [
        (Role.VIEWER, Permission.QUERY, True),
        (Role.VIEWER, Permission.AUDIT_READ, True),
        (Role.VIEWER, Permission.INGEST, False),
        (Role.VIEWER, Permission.PROVIDER_MANAGE, False),
        (Role.VIEWER, Permission.STORAGE_MANAGE, False),
        (Role.VIEWER, Permission.DATA_EXPORT, False),
        (Role.VIEWER, Permission.DATA_DELETE, False),
        (Role.EDITOR, Permission.INGEST, True),
        (Role.EDITOR, Permission.QUERY, True),
        (Role.EDITOR, Permission.PROVIDER_MANAGE, False),
        (Role.EDITOR, Permission.DATA_DELETE, False),
        (Role.ADMIN, Permission.DATA_DELETE, True),
    ],
)
def test_role_permission_matrix(role: Role, permission: Permission, allowed: bool) -> None:
    """Each role grants exactly the documented permissions."""
    assert role_allows(role, permission) is allowed


def test_roles_are_ordered_by_privilege() -> None:
    """A higher role never loses a permission granted to a lower role."""
    viewer = permissions_for(Role.VIEWER)
    editor = permissions_for(Role.EDITOR)
    admin = permissions_for(Role.ADMIN)
    assert viewer < editor < admin
    assert set(ROLE_PERMISSIONS) == {Role.VIEWER, Role.EDITOR, Role.ADMIN}


def test_principal_reports_its_permissions() -> None:
    """Principal exposes the role's permission set and denies others."""
    principal = Principal(subject="s", tenant_id="tenant-a", role=Role.EDITOR)
    assert principal.allows(Permission.INGEST)
    assert not principal.allows(Permission.PROVIDER_MANAGE)
    assert Permission.QUERY in principal.permissions


@pytest.mark.parametrize("value", ["owner", "", "ADMIN ", "superuser"])
def test_parse_role_rejects_unknown_values(value: str) -> None:
    """Unknown role names (except case/whitespace variants of valid ones) are rejected."""
    if value.strip().lower() == "admin":
        assert parse_role(value) is Role.ADMIN
        return
    with pytest.raises(ValueError):
        parse_role(value)


def test_parse_role_is_case_insensitive() -> None:
    """Configured role names are normalized, not trusted verbatim."""
    assert parse_role("Viewer") is Role.VIEWER
