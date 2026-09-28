"""Request models shared by the REST API and the MQTT bridge."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

MAX_COPIES = 100


class LabelRequest(BaseModel):
    """A label print/render request. Unknown top-level keys are label parameters."""

    model_config = ConfigDict(extra="allow")

    params: dict[str, Any] = Field(default_factory=dict, description="Label parameters")
    printer: Optional[str] = Field(None, description="Printer id (default printer if omitted)")
    size: Optional[str] = Field(None, description="4x6 or 2x1 (printer's loaded stock if omitted)")
    dpi: Optional[Literal[203, 300, 600]] = Field(None, description="Override DPI (render only)")
    copies: int = Field(1, ge=1, le=MAX_COPIES)

    def merged(self) -> dict[str, Any]:
        return {**(self.model_extra or {}), **self.params}
