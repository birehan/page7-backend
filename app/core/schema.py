from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Base for every response model. The wire contract is camelCase
    throughout `docs/contracts/api-contract.json` (matching the frontend's
    Zod schemas); Python code stays snake_case. `populate_by_name` lets a
    model also be constructed with its Python field names, not only the
    alias — FastAPI serializes using the alias by default.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
