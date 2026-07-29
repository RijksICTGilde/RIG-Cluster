"""Per-service Pydantic config models (RC-5 Phase 2).

Each configurable service owns a Pydantic model here (or co-located with its
provider once it grows a module). The model is the single source of truth for a
service's config shape: it is both the runtime value guardrail (``model_validate``,
fail-closed) and the source of the committed JSON-schema fragment under
``opi/schemas/services/``. Models are kept dependency-light -- Pydantic only, no
forms/managers -- to match the provider layering.
"""
