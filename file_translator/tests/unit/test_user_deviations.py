"""Unit tests for personal deviations from a role (denied set)."""

import pytest

from file_translator.application.auth_service import AuthService
from file_translator.domain.auth import (
    Permission,
    RoleType,
    User,
    get_permissions_for_role,
)


def _user(role: RoleType, *, raised=(), denied=()) -> User:
    return User(
        user_id="u1",
        username="ivanov",
        role=role,
        permissions=set(raised),
        denied=set(denied),
    )


class TestEffectivePermissions:
    def test_no_deviations_matches_role_pool(self):
        user = _user(RoleType.USER)
        assert user.effective_permissions == get_permissions_for_role(user.role)

    def test_denial_removes_a_role_right(self):
        user = _user(RoleType.USER, denied={Permission.SEND_FEEDBACK})

        assert Permission.SEND_FEEDBACK not in user.effective_permissions
        assert Permission.TRANSLATE in user.effective_permissions

    def test_raise_survives_a_role_without_it(self):
        user = _user(RoleType.VIEWER, raised={Permission.TRANSLATE})

        assert Permission.TRANSLATE in user.effective_permissions

    def test_raise_wins_over_a_conflicting_denial(self):
        user = _user(
            RoleType.USER,
            raised={Permission.SEND_FEEDBACK},
            denied={Permission.SEND_FEEDBACK},
        )

        assert Permission.SEND_FEEDBACK in user.effective_permissions

    def test_denial_of_a_right_the_role_never_granted_changes_nothing(self):
        baseline = _user(RoleType.VIEWER).effective_permissions
        user = _user(RoleType.VIEWER, denied={Permission.MANAGE_SYSTEM})

        assert user.effective_permissions == baseline

    def test_denial_is_scoped_to_one_user(self):
        holder = _user(RoleType.USER, denied={Permission.SEND_FEEDBACK})
        peer = User(user_id="u2", username="petrov", role=RoleType.USER)

        assert Permission.SEND_FEEDBACK not in holder.effective_permissions
        assert Permission.SEND_FEEDBACK in peer.effective_permissions


class TestHasDeviations:
    def test_role_member_without_sets_has_none(self):
        assert not _user(RoleType.USER).has_deviations

    def test_raise_or_denial_marks_a_deviation(self):
        assert _user(RoleType.USER, raised={Permission.MANAGE_SYSTEM}).has_deviations
        assert _user(RoleType.USER, denied={Permission.SEND_FEEDBACK}).has_deviations


BUILT_IN_ROLES = [
    RoleType.ADMIN,
    RoleType.USER,
    RoleType.OPERATOR,
    RoleType.VIEWER,
    RoleType.API,
]


class TestBothResolutionPathsAgree:
    """The registry path gates the API, the enum path gates job ownership.

    If they drift, a right the admin UI shows as granted would be refused by
    the runtime (or the reverse), so they are pinned together for the roles
    whose pool exists in both places.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("role", BUILT_IN_ROLES)
    async def test_without_deviations(self, role):
        user = _user(role)
        registry = await AuthService().effective_permissions_for(user)

        assert registry == user.effective_permissions

    @pytest.mark.asyncio
    @pytest.mark.parametrize("role", BUILT_IN_ROLES)
    async def test_with_a_denial_and_a_raise(self, role):
        user = _user(
            role,
            raised={Permission.MANAGE_SYSTEM},
            denied={Permission.SEND_FEEDBACK},
        )
        registry = await AuthService().effective_permissions_for(user)

        assert registry == user.effective_permissions
        assert Permission.SEND_FEEDBACK not in registry
        assert Permission.MANAGE_SYSTEM in registry
