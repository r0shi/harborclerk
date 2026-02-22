"""Password validation rules for user creation and setup."""


def validate_password(password: str, email: str) -> list[str]:
    """Validate password strength. Returns list of error messages (empty = valid)."""
    errors: list[str] = []

    if len(password) < 12:
        errors.append("Password must be at least 12 characters")

    if email and email.lower() in password.lower():
        errors.append("Password cannot contain your email address")

    if not any(c.isupper() for c in password):
        errors.append("Password must contain at least one uppercase letter")

    if not any(c.islower() for c in password):
        errors.append("Password must contain at least one lowercase letter")

    if not any(c.isdigit() for c in password):
        errors.append("Password must contain at least one digit")

    if len(password) > 0 and len(set(password)) == 1:
        errors.append("Password cannot be a single repeated character")

    return errors
