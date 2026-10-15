"""Export the shared contracts as one deterministic JSON Schema document.

The committed file `services/contracts/schemas/proxylead-contracts.schema.json` is the
input for the frontend's generated TypeScript types. Regenerate with:

    python -m services.contracts.schema_export --write

and verify there is no drift with:

    python -m services.contracts.schema_export --check
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic.json_schema import models_json_schema

from . import models
from .api_session import SessionCreateRequest, SessionInfo

EXPORTED_MODELS: tuple[type[models.ContractModel], ...] = (
    models.AuthenticatedUser,
    models.RequestContext,
    models.PlannedToolCall,
    models.PlanProposal,
    models.ArmorIQDecision,
    models.ApprovalStatus,
    models.ToolInvocationResult,
    models.SourceCitation,
    models.KnowledgeSearchHit,
    models.KnowledgeSearchResult,
    models.AuditEvent,
    models.HealthResponse,
    models.ErrorBody,
    models.ErrorResponse,
    SessionCreateRequest,
    SessionInfo,
)

CONTRACTS_DIR = Path(__file__).resolve().parent  # services/contracts
SCHEMA_PATH = CONTRACTS_DIR / "schemas" / "proxylead-contracts.schema.json"


def build_schema() -> dict[str, Any]:
    _, schema = models_json_schema(
        [(model, "serialization") for model in EXPORTED_MODELS],
        title="ProxyLeadContracts",
        description="Generated from services.contracts. Do not edit by hand.",
    )
    return schema


def render_schema() -> str:
    return json.dumps(build_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write the schema file")
    mode.add_argument("--check", action="store_true", help="fail if the committed schema is stale")
    args = parser.parse_args(argv)

    rendered = render_schema()
    if args.write:
        SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
        SCHEMA_PATH.write_text(rendered, encoding="utf-8", newline="\n")
        print(f"wrote {SCHEMA_PATH}")
        return 0

    current = SCHEMA_PATH.read_text(encoding="utf-8") if SCHEMA_PATH.exists() else ""
    if current != rendered:
        print(f"{SCHEMA_PATH} is stale; run: python -m services.contracts.schema_export --write")
        return 1
    print("contracts schema is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
