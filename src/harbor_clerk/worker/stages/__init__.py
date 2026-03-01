"""Ingestion stage registry — maps JobStage to callable."""

from harbor_clerk.models.enums import JobStage
from harbor_clerk.worker.stages.chunk import run_chunk
from harbor_clerk.worker.stages.embed import run_embed
from harbor_clerk.worker.stages.entities import run_entities
from harbor_clerk.worker.stages.extract import run_extract
from harbor_clerk.worker.stages.finalize import run_finalize
from harbor_clerk.worker.stages.ocr import run_ocr
from harbor_clerk.worker.stages.summarize import run_summarize

STAGE_FUNCTIONS = {
    JobStage.extract: run_extract,
    JobStage.ocr: run_ocr,
    JobStage.chunk: run_chunk,
    JobStage.entities: run_entities,
    JobStage.embed: run_embed,
    JobStage.summarize: run_summarize,
    JobStage.finalize: run_finalize,
}
