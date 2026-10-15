"""Web demo session contracts (P1-03)."""

from .enums import AppRole, PersonaKey
from .models import ContractModel


class SessionCreateRequest(ContractModel):
    """Sign-in body. The only request body that may carry a persona (PRD §4.1)."""

    persona: PersonaKey
    passcode: str


class SessionInfo(ContractModel):
    """Current session. Carries the masked email, never the raw address."""

    persona: PersonaKey
    display_name: str
    app_role: AppRole
    masked_email: str
