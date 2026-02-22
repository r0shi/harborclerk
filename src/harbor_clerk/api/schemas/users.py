"""User management schemas."""

from datetime import datetime

from pydantic import BaseModel


class CreateUserRequest(BaseModel):
    email: str
    password: str
    role: str = "user"


class UpdateUserRequest(BaseModel):
    email: str | None = None
    password: str | None = None
    role: str | None = None
    is_active: bool | None = None


class UserResponse(BaseModel):
    user_id: str
    email: str
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None
