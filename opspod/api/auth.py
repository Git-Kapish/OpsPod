"""API key + role header auth. Simple but enforced: a key may only assume its listed roles."""

from collections.abc import Callable

from fastapi import Cookie, Depends, Header, HTTPException, Request

from opspod.core.config import get_settings
from opspod.domain.enums import Role


def parse_keys(raw: str) -> dict[str, set[Role]]:
    keys: dict[str, set[Role]] = {}
    for part in filter(None, (p.strip() for p in raw.split(";"))):
        key, _, roles = part.partition("=")
        keys[key] = {Role(r) for r in roles.split("|") if r}
    return keys


def require_role(*allowed: Role) -> Callable[..., Role]:
    def dep(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_role: str | None = Header(default=None),
        opspod_key: str | None = Cookie(default=None),  # console login (SameSite=Strict)
        opspod_role: str | None = Cookie(default=None),
    ) -> Role:
        keys: dict[str, set[Role]] = request.app.state.api_keys
        x_api_key, x_role = x_api_key or opspod_key, x_role or opspod_role
        roles = keys.get(x_api_key or "")
        if roles is None:
            raise HTTPException(401, "invalid api key")
        try:
            role = Role(x_role) if x_role else (next(iter(roles)) if len(roles) == 1 else None)
        except ValueError:
            raise HTTPException(403, "unknown role") from None
        if role is None or role not in roles:
            raise HTTPException(403, "role not permitted for this key")
        if role not in allowed:
            raise HTTPException(403, f"role {role.value} cannot perform this action")
        request.state.role, request.state.actor = role, f"{role.value}:{x_api_key}"
        return role

    return dep


__all__ = ["Depends", "get_settings", "parse_keys", "require_role"]
