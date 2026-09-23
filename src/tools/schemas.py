"""Tool specifications and a self-contained JSON-Schema subset validator.

Why not the `jsonschema` package: it is present in this venv only as a
transitive dependency of litellm and is not declared in requirements.txt.
The schemas here use a small, fixed subset (object/array/string/number/
integer/boolean, required, enum, bounds, defaults, additionalProperties), so a
120-line validator removes a dependency risk instead of adding one. If the
schemas ever outgrow this subset, swap in `jsonschema` and delete `validate()` -
the ToolSpec surface does not change.

Every spec answers four questions the legacy tool layer left implicit:

- **when_to_use / when_not_to_use**: the model-facing selection guidance. The
  MCP server previously exposed only a Python docstring, which said what the
  tool did but never when *not* to reach for it.
- **returns_summary_of**: what the caller gets back. Tools here return a
  compact projection plus an artifact pointer, never a full page body - that
  is what kept 8 KB of HTML out of every prompt.
- **side_effect_free**: whether the executor's write-safety controls apply.
- **cost_hint**: rough expense class, used by the budget-aware policy.
"""
from __future__ import annotations

import copy
from enum import Enum
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field


class ToolCategory(str, Enum):
    """What kind of thing this is.

    The distinction matters and is the reason this enum exists: the legacy repo
    had ~49 things called "tools", of which only 4 were ever selectable at
    runtime. `AGENT_SELECTABLE` is the honest count; `INTERNAL_STEP` records
    the deterministic analysis functions so they are still traceable without
    inflating the tool inventory.
    """

    SEARCH = "search"
    WEB_READ = "web_read"
    PDF_READ = "pdf_read"
    MARKET_DATA = "market_data"
    FILE_EXPORT = "file_export"
    INTERNAL_STEP = "internal_step"


#: Categories a planner/router may actually choose between.
AGENT_SELECTABLE: frozenset[ToolCategory] = frozenset({
    ToolCategory.SEARCH, ToolCategory.WEB_READ, ToolCategory.PDF_READ,
    ToolCategory.MARKET_DATA, ToolCategory.FILE_EXPORT,
})


class CostHint(str, Enum):
    FREE = "free"           # pure local computation
    CHEAP = "cheap"         # cached or single fast HTTP call
    MODERATE = "moderate"   # live network, seconds
    EXPENSIVE = "expensive" # multi-second, rate-limited, or paid


# --------------------------------------------------------------------------- #
# Minimal JSON-Schema subset validator
# --------------------------------------------------------------------------- #
_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
    "null": (type(None),),
}


def _type_ok(value: Any, expected: str) -> bool:
    types = _TYPE_MAP.get(expected)
    if types is None:
        return True
    # bool is a subclass of int in Python; an integer field must not accept True.
    if expected in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, types)


def validate(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Validate `instance` against a JSON-Schema subset. Returns error strings.

    An empty list means valid. Errors are accumulated rather than raised so a
    caller can report every problem in one message instead of one per round
    trip.
    """
    errors: list[str] = []
    if not schema:
        return errors

    expected = schema.get("type")
    if expected:
        expected_list = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(instance, t) for t in expected_list):
            errors.append(f"{path}: expected type {expected}, got {type(instance).__name__}")
            return errors

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in enum {schema['enum']}")

    if isinstance(instance, str):
        if (min_len := schema.get("minLength")) is not None and len(instance) < min_len:
            errors.append(f"{path}: length {len(instance)} < minLength {min_len}")
        if (max_len := schema.get("maxLength")) is not None and len(instance) > max_len:
            errors.append(f"{path}: length {len(instance)} > maxLength {max_len}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if (minimum := schema.get("minimum")) is not None and instance < minimum:
            errors.append(f"{path}: {instance} < minimum {minimum}")
        if (maximum := schema.get("maximum")) is not None and instance > maximum:
            errors.append(f"{path}: {instance} > maximum {maximum}")

    if isinstance(instance, (list, tuple)):
        if (min_items := schema.get("minItems")) is not None and len(instance) < min_items:
            errors.append(f"{path}: {len(instance)} items < minItems {min_items}")
        if item_schema := schema.get("items"):
            for i, item in enumerate(instance):
                errors.extend(validate(item, item_schema, f"{path}[{i}]"))

    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required property {key!r}")
        for key, value in instance.items():
            if key in properties:
                errors.extend(validate(value, properties[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property {key!r}")

    return errors


def apply_defaults(arguments: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Fill in top-level `default`s for absent properties (non-mutating)."""
    out = dict(arguments)
    for key, prop in schema.get("properties", {}).items():
        if key not in out and "default" in prop:
            out[key] = copy.deepcopy(prop["default"])
    return out


def coerce(arguments: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Best-effort scalar coercion for LLM-produced arguments.

    Models routinely emit `"max_results": "8"`. Coercing a numeric string is a
    kindness; coercing anything ambiguous is not, so only string->int/float/bool
    on unambiguous inputs is attempted and everything else is left for
    `validate()` to reject.
    """
    out = dict(arguments)
    for key, prop in schema.get("properties", {}).items():
        if key not in out or not isinstance(out[key], str):
            continue
        target, raw = prop.get("type"), out[key].strip()
        try:
            if target == "integer":
                out[key] = int(raw)
            elif target == "number":
                out[key] = float(raw)
            elif target == "boolean" and raw.lower() in ("true", "false"):
                out[key] = raw.lower() == "true"
        except ValueError:
            continue  # leave as-is; validate() will produce the error
    return out


# --------------------------------------------------------------------------- #
# Tool specification
# --------------------------------------------------------------------------- #
class ToolSpec(BaseModel):
    """Everything the harness and a router need to know about one tool."""

    name: str
    namespace: str = "core"
    category: ToolCategory
    description: str

    #: Selection guidance surfaced to any model that picks tools. Written as
    #: instructions to the caller, not as prose about the implementation.
    when_to_use: str = ""
    when_not_to_use: str = ""

    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)

    #: What the caller receives back, in one line. Tools that read documents
    #: return a projection + artifact id, never the full body.
    returns_summary_of: str = ""

    side_effect_free: bool = True
    approval_required: bool = False
    idempotent: bool = True
    compensation_action: str = ""

    cost_hint: CostHint = CostHint.MODERATE
    timeout_s: float = 30.0
    max_attempts: int = 3
    #: Tool to fall back to when this one is unavailable (circuit open).
    fallback_tool: str = ""

    @property
    def qualified_name(self) -> str:
        return f"{self.namespace}.{self.name}"

    def validate_input(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Coerce + default + validate. Returns (normalized_args, errors)."""
        normalized = apply_defaults(coerce(arguments or {}, self.input_schema), self.input_schema)
        return normalized, validate(normalized, self.input_schema)

    def validate_output(self, result: Any) -> list[str]:
        return validate(result, self.output_schema) if self.output_schema else []

    def to_catalog_entry(self) -> dict[str, Any]:
        """Compact form injected into prompts / used as router training input.

        Deliberately omits timeouts, retries and fallbacks: those are harness
        concerns, and putting them in a prompt invites the model to reason
        about infrastructure instead of about the task.
        """
        return {
            "name": self.name,
            "category": self.category.value,
            "description": self.description,
            "when_to_use": self.when_to_use,
            "when_not_to_use": self.when_not_to_use,
            "input_schema": self.input_schema,
            "returns": self.returns_summary_of,
            "cost": self.cost_hint.value,
        }


class ToolResult(BaseModel):
    """Uniform envelope returned by `ToolExecutor.call()`.

    `content` is the compact projection given back to the agent. `artifact_id`
    points at the full payload for anything large, so grounding stays possible
    without paying context for it.
    """

    tool_name: str
    ok: bool
    content: Any = None
    artifact_id: str = ""
    error_class: str = ""
    error_message: str = ""
    attempts: int = 0
    duration_s: float = 0.0
    from_cache: bool = False
    deduped: bool = False
    blocked_reason: str = ""
    step_id: str = ""

    def unwrap(self, default: Any = None) -> Any:
        """The value a legacy call site expects, or `default` when the call failed."""
        return self.content if self.ok else default


#: Signature every registered handler must satisfy.
ToolHandler = Callable[..., Any]


class ToolRegistration(BaseModel):
    """A spec bound to its callable."""

    spec: ToolSpec
    handler: Any  # ToolHandler; Any keeps pydantic from trying to schema it
    #: Projects a raw handler result into (compact_content, full_payload).
    projector: Optional[Any] = None

    model_config = {"arbitrary_types_allowed": True}


ToolStatus = Literal["ok", "error", "blocked", "deduped", "cached"]
