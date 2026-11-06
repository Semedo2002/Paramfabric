"""Tag-scoped role-based authorization"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Literal

from paramfabric.security.auth import Principal

Action = Literal["read", "edit", "approve", "merge", "export", "simulate", "admin"]


@dataclass(frozen=True)
class RBACPolicy:
    """Maps role names and actions to glob patterns matching parameter tags"""

    grants: dict[str, dict[Action, tuple[str, ...]]] = field(default_factory=dict)

    @classmethod
    def default(cls) -> "RBACPolicy":
        return cls(
            {
                "viewer": {"read": ("*",), "export": ("*",)},
                "engineer": {"read": ("*",), "edit": ("*",), "export": ("*",), "simulate": ("*",)},
                "approver": {"read": ("*",), "approve": ("*",)},
                "admin": {
                    "read": ("*",),
                    "edit": ("*",),
                    "approve": ("*",),
                    "merge": ("*",),
                    "export": ("*",),
                    "simulate": ("*",),
                    "admin": ("*",),
                },
            }
        )

    def allows(self, principal: Principal, action: Action, tags: list[str] | None = None) -> bool:
        required = tags or ["*"]
        for role in principal.roles:
            patterns = self.grants.get(role, {}).get(action, ())
            if patterns and all(
                any(fnmatch.fnmatchcase(tag.lower(), pattern.lower()) for pattern in patterns)
                for tag in required
            ):
                return True
        return False

    def require(self, principal: Principal, action: Action, tags: list[str] | None = None) -> None:
        if not self.allows(principal, action, tags):
            raise PermissionError(f"{principal.subject} is not authorized to {action} these parameters")
