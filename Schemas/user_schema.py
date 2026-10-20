
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


USER_ID_PATTERN = r"^USR[0-9]{5,}$"


class UserCreate(BaseModel):
    login_id: str = Field(
        min_length=8,
        max_length=20,
        pattern=USER_ID_PATTERN,
    )
    full_name: str = Field(min_length=2, max_length=150)

    # Email is optional during user creation.
    email: Optional[EmailStr] = None

    password: str = Field(min_length=8, max_length=128)
    role_id: int = Field(gt=0)
    is_active: bool = True


class UserUpdate(BaseModel):
    login_id: Optional[str] = Field(
        default=None,
        min_length=8,
        max_length=20,
        pattern=USER_ID_PATTERN,
    )
    full_name: Optional[str] = Field(
        default=None,
        min_length=2,
        max_length=150,
    )

    # Email can be omitted or explicitly set to null.
    email: Optional[EmailStr] = None

    password: Optional[str] = Field(
        default=None,
        min_length=8,
        max_length=128,
    )
    role_id: Optional[int] = Field(default=None, gt=0)
    is_active: Optional[bool] = None


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    login_id: str
    full_name: str

    # API responses must allow users without an email address.
    email: Optional[EmailStr] = None

    role_id: int
    role_name: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class UserRoleOption(BaseModel):
    id: int
    name: str
