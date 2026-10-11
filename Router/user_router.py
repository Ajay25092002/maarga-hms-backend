from typing import Optional

from fastapi import APIRouter, Query, status

from Schemas.user_schema import UserCreate, UserUpdate
from Services.user_service import (
    create_user_service,
    deactivate_user_service,
    get_user_service,
    list_user_roles_service,
    list_users_service,
    update_user_service,
)

router = APIRouter(
    prefix="/api/users",
    tags=["Users"],
)


@router.get("/roles")
def get_assignable_roles():
    """Return active roles that can be assigned to users."""
    return list_user_roles_service()


@router.get("")
def list_users(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    search: Optional[str] = Query(default=None, max_length=100),
    role_id: Optional[int] = Query(default=None, ge=1),
    is_active: Optional[bool] = Query(default=None),
):
    """List users with pagination and optional filters."""
    return list_users_service(
        page=page,
        page_size=page_size,
        search=search,
        role_id=role_id,
        is_active=is_active,
    )


@router.get("/{user_id}")
def get_user(user_id: int):
    """Retrieve a single user."""
    return get_user_service(user_id)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate):
    """Create a user and assign an existing active role."""
    return create_user_service(
        payload.model_dump()
    )


@router.patch("/{user_id}")
def update_user(user_id: int, payload: UserUpdate):
    """Update supplied user fields without replacing omitted fields."""
    return update_user_service(
        user_id,
        payload.model_dump(exclude_unset=True),
    )


@router.delete("/{user_id}")
def deactivate_user(user_id: int):
    """Deactivate a user without permanently deleting the record."""
    return deactivate_user_service(user_id)