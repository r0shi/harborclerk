import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, uuid_pk, created_at
from harbor_clerk.models.enums import JobStage, JobStatus


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"

    job_id: Mapped[uuid_pk]
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document_versions.version_id", ondelete="CASCADE"),
        nullable=False,
    )
    stage: Mapped[JobStage] = mapped_column(
        Enum(JobStage, name="job_stage", create_type=False),
        nullable=False,
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", create_type=False),
        nullable=False,
        server_default="queued",
    )
    progress_current: Mapped[Optional[int]] = mapped_column(
        Integer, server_default=text("0"),
    )
    progress_total: Mapped[Optional[int]] = mapped_column(
        Integer, server_default=text("0"),
    )
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"),
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[created_at]
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        UniqueConstraint("version_id", "stage", name="uq_jobs_version_stage"),
    )
