"""Unit tests for matrix helpers in admin_service/routers/access.py."""

from types import SimpleNamespace

from admin_service.routers.access import (
    SUBJECT_GROUP,
    SUBJECT_ROLE,
    SUBJECT_USER,
    _group_aware_cell_flags,
    _group_has_effective_rights,
    _group_matching_grants,
    _group_subject_visible,
    _iter_known_group_names,
)
from file_translator.infrastructure.auth.role_config import GrantDoc


def _grant(subject: str, collection: str, *, level: int = 1,
           subject_type: str = SUBJECT_GROUP, read: bool = True,
           write: bool = False) -> GrantDoc:
    return GrantDoc(
        subject_type=subject_type,
        subject=subject,
        collection=collection,
        level=level,
        read=read,
        write=write,
    )


class TestGroupSubjectVisible:
    def test_group_matching_collection_name_is_visible(self):
        assert _group_subject_visible("DTD", ["dtd", "default"], [])

    def test_group_matching_collection_ignores_case(self):
        assert _group_subject_visible("Cem_Users", ["CEM_USERS"], [])

    def test_group_with_grant_is_visible_case_insensitive(self):
        grants = [_grant("CEM_USERS", "dtd")]
        assert _group_subject_visible("cem_users", ["default"], grants)

    def test_level_zero_grant_still_visible(self):
        grants = [_grant("ALL_T", "dtd", level=0)]
        assert _group_subject_visible("all_t", ["default"], grants)

    def test_unmatched_group_is_hidden(self):
        assert not _group_subject_visible("HR_DEPT", ["dtd"], [])

    def test_role_grant_does_not_echo_as_group(self):
        grants = [_grant("User", "dtd", subject_type=SUBJECT_ROLE)]
        assert not _group_subject_visible("User", ["dtd"], grants)


class TestIterKnownGroupNames:
    def _user(self, groups):
        return SimpleNamespace(ldap_groups=groups)

    def test_unions_ldap_groups_and_group_grants(self):
        users = [self._user(["CEM_USERS"]), self._user(["FIN_TEAM"])]
        grants = [_grant("Dev_Audit", "dtd")]
        result = _iter_known_group_names(users, grants)
        assert result == {
            "cem_users": "CEM_USERS",
            "fin_team": "FIN_TEAM",
            "dev_audit": "Dev_Audit",
        }

    def test_dedupes_by_casefold_keep_first_canonical(self):
        users = [self._user(["CEM_USERS", "cem_users"]), self._user(["CEM_USERS"])]
        result = _iter_known_group_names(users, [])
        assert result == {"cem_users": "CEM_USERS"}

    def test_role_and_user_grants_do_not_add_group_names(self):
        grants = [
            _grant("User", "dtd", subject_type=SUBJECT_ROLE),
            _grant("alice", "dtd", subject_type=SUBJECT_USER),
        ]
        result = _iter_known_group_names([], grants)
        assert result == {}

    def test_user_without_ldap_groups_is_skipped(self):
        users = [SimpleNamespace(no_groups=True)]
        assert _iter_known_group_names(users, []) == {}


class TestGroupAwareCellFlags:
    def test_group_subjects_are_folded_to_shared_cell(self):
        grants = [
            _grant("CEM_USERS", "dtd", level=1),
            _grant("cem_users", "dtd", level=3),
        ]
        flags = _group_aware_cell_flags(grants)
        assert flags[(SUBJECT_GROUP, "cem_users", "dtd")]["level"] == 3

    def test_max_level_wins_across_read_write_flags(self):
        grants = [
            _grant("GRP", "dtd", level=2, read=True, write=False),
            _grant("grp", "dtd", level=3, read=False, write=True),
        ]
        cell = _group_aware_cell_flags(grants)[(SUBJECT_GROUP, "grp", "dtd")]
        assert cell == {"read": True, "write": True, "level": 3}

    def test_different_subject_types_stay_separate_keys(self):
        grants = [
            _grant("User", "dtd", subject_type=SUBJECT_ROLE),
            _grant("user", "dtd", subject_type=SUBJECT_USER),
        ]
        flags = _group_aware_cell_flags(grants)
        assert (SUBJECT_ROLE, "User", "dtd") in flags
        assert (SUBJECT_USER, "user", "dtd") in flags
        assert (SUBJECT_GROUP, "user", "dtd") not in flags


class TestGroupDeletionGuard:
    def test_matching_grants_collect_both_spellings(self):
        grants = [
            _grant("CEM_USERS", "dtd", level=1),
            _grant("cem_users", "dtd", level=0),
        ]
        assert len(_group_matching_grants(grants, "CEM_USERS")) == 2
        assert len(_group_matching_grants(grants, "cem_users")) == 2

    def test_matching_grants_ignores_role_and_user(self):
        grants = [
            _grant("GRP", "dtd", level=1),
            _grant("GRP", "dtd", level=1, subject_type=SUBJECT_ROLE),
            _grant("grp", "dtd", level=1, subject_type=SUBJECT_USER),
        ]
        assert {g.subject_type for g in _group_matching_grants(grants, "GRP")} == {
            SUBJECT_GROUP
        }

    def test_effective_rights_block_deletion(self):
        grants = [_grant("GRP", "dtd", level=1)]
        assert _group_has_effective_rights(grants, "grp")

    def test_only_level_zero_does_not_block(self):
        grants = [_grant("GRP", "dtd", level=0)]
        assert not _group_has_effective_rights(grants, "GRP")

    def test_no_grants_does_not_block(self):
        assert not _group_has_effective_rights([], "GRP")

    def test_level_zero_next_to_effective_blocks(self):
        grants = [
            _grant("GRP", "dtd", level=0),
            _grant("grp", "oup", level=1),
            _grant("grp", "dtd", level=3),
        ]
        assert _group_has_effective_rights(grants, "GRP")

    def test_effective_rights_are_per_group(self):
        grants = [_grant("OTHER", "dtd", level=2)]
        assert not _group_has_effective_rights(grants, "GRP")