
from fastapi import APIRouter, HTTPException, Response, status

from Schemas.role_schema import (
    RoleCreate,
    RoleUpdate,
    RoleResponse,
    PermissionCatalogItem,
    ROLE_PERMISSIONS,
)
from Services import role_service
from Services.role_service import (
    RoleNotFoundError,
    DuplicateRoleError,
    RoleInUseError,
)

router = APIRouter(
    prefix="/api/roles",
    tags=["Roles"],
)


@router.get("/permissions", response_model=list[PermissionCatalogItem])
def get_permissions():
    """Return supported permission keys and labels."""
    return [
        {"key": key, "label": label}
        for key, label in ROLE_PERMISSIONS.items()
    ]


@router.get("/", response_model=list[RoleResponse])
def get_roles():
    """List all roles."""
    return role_service.list_roles()


@router.get("/{role_id}", response_model=RoleResponse)
def get_role(role_id: int):
    """Get one role by ID."""
    try:
        return role_service.get_role(role_id)
    except RoleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post(
    "/",
    response_model=RoleResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_role(payload: RoleCreate):
    """Create a role with configurable permissions."""
    try:
        return role_service.create_role(payload.model_dump())
    except DuplicateRoleError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.put("/{role_id}", response_model=RoleResponse)
def update_role(role_id: int, payload: RoleUpdate):
    """Update a role, including its permissions."""
    try:
        return role_service.update_role(
            role_id,
            payload.model_dump(),
        )
    except RoleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except DuplicateRoleError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.delete(
    "/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_role(role_id: int):
    """Delete a role if no users reference it."""
    try:
        role_service.delete_role(role_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except RoleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except RoleInUseError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
