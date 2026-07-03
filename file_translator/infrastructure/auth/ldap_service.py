"""LDAP authentication service for Active Directory integration."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import ldap3
from ldap3 import SUBTREE, Server, Tls

from file_translator.domain.auth import RoleType

logger = logging.getLogger(__name__)


@dataclass
class LdapUserInfo:
    username: str
    display_name: str
    email: str = ""
    groups: list[str] = None


class LdapService:
    """Authenticates users against Active Directory via LDAP.

    Usage:
        ldap = LdapService.from_env()
        info = await ldap.authenticate("username", "password")
        if info:
            role = ldap.map_to_role(info.groups)
    """

    def __init__(
        self,
        ldap_url: str,
        bind_dn: str,
        bind_credentials: str,
        user_search_base: str,
        search_filter: str,
        group_admin: str = "",
        group_operator: str = "",
        group_viewer: str = "",
        connect_timeout: int = 10,
    ):
        self._ldap_url = ldap_url
        self._bind_dn = bind_dn
        self._bind_credentials = bind_credentials
        self._user_search_base = user_search_base
        self._search_filter_template = search_filter
        self._group_admin = group_admin
        self._group_operator = group_operator
        self._group_viewer = group_viewer
        self._connect_timeout = connect_timeout

    @classmethod
    def from_env(cls) -> LdapService | None:
        ldap_url = os.getenv("LDAP_URL", "")
        if not ldap_url:
            logger.info("LDAP_URL not set — LDAP authentication disabled")
            return None
        return cls(
            ldap_url=ldap_url,
            bind_dn=os.getenv("LDAP_BIND_DN", ""),
            bind_credentials=os.getenv("LDAP_BIND_CREDENTIALS", ""),
            user_search_base=os.getenv("LDAP_USER_SEARCH_BASE", ""),
            search_filter=os.getenv("LDAP_SEARCH_FILTER", "(sAMAccountName={{username}})"),
            group_admin=os.getenv("LDAP_GROUP_ADMIN", ""),
            group_operator=os.getenv("LDAP_GROUP_OPERATOR", ""),
            group_viewer=os.getenv("LDAP_GROUP_VIEWER", ""),
        )

    async def authenticate(self, username: str, password: str) -> LdapUserInfo | None:
        """Authenticate user against AD and return user info on success."""
        if not username or not password:
            return None

        search_filter = self._search_filter_template.replace("{{username}}", username)

        def _sync() -> LdapUserInfo | None:
            try:
                server = Server(self._ldap_url, connect_timeout=self._connect_timeout)
                conn = ldap3.Connection(
                    server,
                    user=self._bind_dn,
                    password=self._bind_credentials,
                    auto_bind=True,
                    receive_timeout=self._connect_timeout,
                )

                conn.search(
                    search_base=self._user_search_base,
                    search_filter=search_filter,
                    search_scope=SUBTREE,
                    attributes=["sAMAccountName", "displayName", "mail", "memberOf"],
                    size_limit=1,
                )

                if not conn.entries:
                    logger.warning(f"LDAP user not found: {username}")
                    conn.unbind()
                    return None

                entry = conn.entries[0]
                user_dn = str(entry.entry_dn)

                conn.unbind()

                user_conn = ldap3.Connection(
                    server,
                    user=user_dn,
                    password=password,
                    receive_timeout=self._connect_timeout,
                )

                if not user_conn.bind():
                    logger.warning(f"LDAP bind failed for {username}")
                    user_conn.unbind()
                    return None

                user_conn.unbind()

                if hasattr(entry, "displayName"):
                    display_name = str(entry.displayName)
                else:
                    display_name = str(entry.sAMAccountName) if hasattr(entry, "sAMAccountName") else username
                if hasattr(entry, "mail"):
                    email = str(entry.mail)
                else:
                    email = ""
                groups = []
                if hasattr(entry, "memberOf"):
                    for g in entry.memberOf:
                        dn_str = str(g)
                        cn_match = re.search(r"CN=([^,]+)", dn_str)
                        if cn_match:
                            groups.append(cn_match.group(1))

                return LdapUserInfo(
                    username=username,
                    display_name=display_name,
                    email=email,
                    groups=groups,
                )

            except ldap3.core.exceptions.LDAPBindError as e:
                logger.warning(f"LDAP bind error for {username}: {e}")
                return None
            except ldap3.core.exceptions.LDAPException as e:
                logger.warning(f"LDAP connection error: {e}")
                return None
            except Exception as e:
                logger.warning(f"LDAP unexpected error for {username}: {e}")
                return None

        return await asyncio.to_thread(_sync)

    def map_to_role(self, groups: list[str] | None) -> RoleType:
        """Map AD groups to application role."""
        if not groups:
            return RoleType.OPERATOR

        group_set = set(groups)
        if self._group_admin and self._group_admin in group_set:
            return RoleType.ADMIN
        if self._group_operator and self._group_operator in group_set:
            return RoleType.OPERATOR
        if self._group_viewer and self._group_viewer in group_set:
            return RoleType.VIEWER

        return RoleType.OPERATOR
