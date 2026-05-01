"""Auth request/response schemas."""

from pydantic import BaseModel


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserInfo"


class UserInfo(BaseModel):
    user_id: str
    email: str
    role: str
    preferences: dict = {}

    model_config = {"from_attributes": True}


class PreferencesUpdate(BaseModel):
    theme: str | None = None
    page_size: int | None = None
    # JSONB-stored boolean set when the user completes (or dismisses) the
    # first-run onboarding wizard. Frontend reads `preferences.onboardingComplete`
    # in Layout.tsx to decide whether to render <OnboardingWizard />.
    # Naming is camelCase to match the existing frontend type and the JSONB
    # key the wizard reads — mixing snake/camel here is intentional, not a bug.
    onboardingComplete: bool | None = None


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str
