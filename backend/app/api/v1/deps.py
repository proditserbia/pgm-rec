"""
FastAPI dependency functions for authentication & RBAC — Phase 4.

Usage in route handlers:

    @router.post("/start")
    def start(channel_id: str, db: DbDep, _: AdminDep):
        ...
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from ...db.session import get_db
from ...services.auth_service import decode_access_token, get_user_by_username
from ...db.models import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    payload = decode_access_token(token)
    if payload is None:
        raise credentials_exc
    username: str | None = payload.get("sub")
    if not username:
        raise credentials_exc
    user = get_user_by_username(db, username)
    if user is None or not user.is_active:
        raise credentials_exc
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


def _require_role(*allowed_roles: str):
    def dep(user: CurrentUserDep) -> User:
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role}' is not permitted for this action.",
            )
        return user
    return dep


# Convenience typed deps
AdminDep = Annotated[User, Depends(_require_role("admin"))]
ExportDep = Annotated[User, Depends(_require_role("admin", "export"))]
# preview and above = any authenticated user
AnyRoleDep = Annotated[User, Depends(get_current_user)]
