"""Guards the OpenAPI-schema-quality finding from the API review (2026-07-18):
several Omni-Channel routes returned ``dict[str, Any]``/``dict[str, int | str]``
instead of a typed Pydantic response model, which Swagger/OpenAPI renders as an
opaque, property-less ``object`` -- no field names, no types, unusable for
client codegen. Those were fixed to typed models; this test keeps it fixed.

``receive_webhook``'s ``dict[str, str]`` is a deliberate, narrow exception
(provider-facing ack, not a REST resource -- see ``app/routers/omnichannel.py``):
it's still a genuinely *typed* map (``additionalProperties: {"type": "string"}``),
just not a named model, so it's exempted explicitly rather than by a loophole
in the general check.
"""

from __future__ import annotations

from typing import Any

from app.main import app

_UNTYPED_OBJECT_EXEMPTIONS = {
    ("/v1/omnichannel/webhooks/{channel_type}/{connection_id}", "post", "200"),
}


def _resolve(schema: dict[str, Any], components: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        resolved: dict[str, Any] = components["schemas"][name]
        return resolved
    return schema


def _is_untyped_object(schema: dict[str, Any]) -> bool:
    """True for a bare `{"type": "object"}` with no properties and no typed
    ``additionalProperties`` -- i.e. no field information a client could use."""
    if schema.get("type") != "object":
        return False
    if schema.get("properties"):
        return False
    additional = schema.get("additionalProperties")
    if isinstance(additional, dict):
        return False  # a typed map, e.g. dict[str, str] -- has real shape
    return True


def test_no_omnichannel_response_is_an_untyped_object_schema() -> None:
    spec = app.openapi()
    components = spec.get("components", {})
    offenders: list[tuple[str, str, str]] = []

    for path, path_item in spec["paths"].items():
        if "/omnichannel" not in path:
            continue
        for method, operation in path_item.items():
            for status_code, response in operation.get("responses", {}).items():
                if not status_code.startswith("2"):
                    continue
                json_content = response.get("content", {}).get("application/json")
                if json_content is None:
                    continue  # e.g. the webhook-verification route's PlainTextResponse
                schema = _resolve(json_content["schema"], components)
                if _is_untyped_object(schema):
                    key = (path, method, status_code)
                    if key not in _UNTYPED_OBJECT_EXEMPTIONS:
                        offenders.append(key)

    assert offenders == [], f"untyped object response(s) found: {offenders}"


def test_openapi_schema_builds_without_error() -> None:
    """A minimal smoke test that app.openapi() itself doesn't raise -- e.g. a
    route returning a plain, non-Pydantic type FastAPI can't introspect."""
    spec = app.openapi()
    assert spec["paths"]
    assert any("/v1/omnichannel" in p for p in spec["paths"])
    assert any("/v1/core" in p for p in spec["paths"])
    assert "/health" in spec["paths"]
