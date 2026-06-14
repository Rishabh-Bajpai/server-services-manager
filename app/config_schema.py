"""Pydantic schemas for config.yaml.

These models validate the loaded config and surface clear error
messages at startup. They are deliberately lenient — extra fields
are allowed so the user's config can grow new sections without
breaking the app.
"""
from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow")


class ProgramConfigSchema(_Base):
    name: str = Field(min_length=1, max_length=128)
    command: str = Field(min_length=1)
    cwd: str = Field(min_length=1)
    autostart: bool = False
    environment: Dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        # systemd-style names: alphanumerics, underscore, dash, dot
        import re
        if not re.match(r"^[A-Za-z0-9_.-]+$", v):
            raise ValueError(
                f"service name {v!r} must contain only letters, "
                f"digits, underscores, dashes, or dots"
            )
        return v


class HealthCheckSchema(_Base):
    type: Literal["http", "tcp", "cmd"]
    target: str = Field(min_length=1)
    interval: int = Field(default=30, ge=5, le=86400)
    timeout: int = Field(default=5, ge=1, le=300)
    expect: Optional[int] = None


class ProgramWithHealth(ProgramConfigSchema):
    health_check: Optional[HealthCheckSchema] = None


class CommandSchema(_Base):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=128)
    command: str = Field(min_length=1)
    icon: str = "terminal"
    auth: bool = False
    description: str = ""


class NtfyConfigSchema(_Base):
    type: Literal["ntfy"]
    topic: str
    server: str = "https://ntfy.sh"
    priority: str = "default"


class WebhookConfigSchema(_Base):
    type: Literal["webhook"]
    url: str
    headers: Optional[Dict[str, str]] = None


class TelegramConfigSchema(_Base):
    type: Literal["telegram"]
    bot_token: str
    chat_id: str


class EmailConfigSchema(_Base):
    type: Literal["email"]
    host: str
    port: int = 587
    username: str = ""
    password: str = ""
    from_addr: str = ""
    to: List[str]
    use_tls: bool = True


class RootConfigSchema(_Base):
    """Top-level config.yaml shape."""
    programs: List[ProgramWithHealth] = Field(default_factory=list)
    commands: List[CommandSchema] = Field(default_factory=list)
    notifications: List[
        NtfyConfigSchema
        | WebhookConfigSchema
        | TelegramConfigSchema
        | EmailConfigSchema
    ] = Field(default_factory=list)

    @field_validator("programs")
    @classmethod
    def _unique_program_names(cls, v: List) -> List:
        seen = set()
        for p in v:
            if p.name in seen:
                raise ValueError(f"duplicate program name: {p.name!r}")
            seen.add(p.name)
        return v

    @field_validator("commands")
    @classmethod
    def _unique_command_ids(cls, v: List) -> List:
        seen = set()
        for c in v:
            if c.id in seen:
                raise ValueError(f"duplicate command id: {c.id!r}")
            seen.add(c.id)
        return v


def validate_config(data: dict) -> RootConfigSchema:
    """Validate parsed config dict; raise ValidationError on bad data."""
    return RootConfigSchema.model_validate(data or {})
