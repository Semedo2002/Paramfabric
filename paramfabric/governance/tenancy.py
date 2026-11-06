"""Tenant scope (organization + project) shared by the store, the API, and the task queue.

Every governed record belongs to exactly one organization and one project.
The API resolves the tenant per request (token `org` claim + the
`X-Paramfabric-Project` header) and sets :data:`CURRENT_TENANT`, so code
that has no request object (workbench helpers, task submission) reads the
same scope.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
DEFAULT_ORGANIZATION = "default"
DEFAULT_PROJECT = "default"


@dataclass(frozen=True)
class Tenant:
    organization: str = DEFAULT_ORGANIZATION
    project: str = DEFAULT_PROJECT

    def __post_init__(self) -> None:
        for label, value in (("organization", self.organization), ("project", self.project)):
            if not SLUG.match(value):
                raise ValueError(f"invalid {label} id {value!r}: use lowercase letters, digits, and dashes")

    @property
    def is_default(self) -> bool:
        return self.organization == DEFAULT_ORGANIZATION and self.project == DEFAULT_PROJECT

    def as_dict(self) -> dict[str, str]:
        return {"organization": self.organization, "project": self.project}


DEFAULT_TENANT = Tenant()
CURRENT_TENANT: ContextVar[Tenant] = ContextVar("paramfabric_tenant", default=DEFAULT_TENANT)


def current_tenant() -> Tenant:
    return CURRENT_TENANT.get()


@contextmanager
def tenant_scope(tenant: Tenant) -> Iterator[Tenant]:
    token = CURRENT_TENANT.set(tenant)
    try:
        yield tenant
    finally:
        CURRENT_TENANT.reset(token)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:63]
    return slug or DEFAULT_ORGANIZATION
