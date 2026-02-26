import enum


class UserRole(str, enum.Enum):
    admin = "admin"
    user = "user"


class UploadSource(str, enum.Enum):
    web = "web"
    watch_folder = "watch_folder"


class VersionStatus(str, enum.Enum):
    queued = "queued"
    extracting = "extracting"
    extracted = "extracted"
    ocr_running = "ocr_running"
    ocr_done = "ocr_done"
    chunking = "chunking"
    chunked = "chunked"
    embedding = "embedding"
    embedded = "embedded"
    summarizing = "summarizing"
    summarized = "summarized"
    finalizing = "finalizing"
    ready = "ready"
    error = "error"


class JobStage(str, enum.Enum):
    extract = "extract"
    ocr = "ocr"
    chunk = "chunk"
    embed = "embed"
    summarize = "summarize"
    finalize = "finalize"


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    done = "done"
    error = "error"
