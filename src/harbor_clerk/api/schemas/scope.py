"""Shared scope schema for Ask / Research / Search request bodies.

Forward-compatible wrapper: today only `folder_ids` is honored. Future axes
(collection_ids, doc_ids, topic_ids) can be added as new optional fields with
no migration and no endpoint version bump. extra='ignore' on the model lets
older servers tolerate newer clients sending unknown keys.
"""

import uuid

from pydantic import BaseModel, ConfigDict


class ScopeSpec(BaseModel):
    """A user-driven document-visibility scope.

    Empty (`{}`) or all fields None/[] means no restriction — all active
    documents are visible. When fields are populated, scoping mirrors
    KeyScope's OR-across-axes semantics (today: only folder_ids).
    """

    model_config = ConfigDict(extra="ignore")

    folder_ids: list[uuid.UUID] | None = None
    # Future axes will be added here additively. Examples (not yet active):
    #   collection_ids: list[uuid.UUID] | None = None
    #   doc_ids: list[uuid.UUID] | None = None
    #   topic_ids: list[int] | None = None
