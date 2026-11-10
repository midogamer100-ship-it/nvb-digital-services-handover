"""Re-export: statement dataset verification (contract statement-v2) lives in ``nvb_platform``."""

from nvb_platform.dataset import (  # noqa: F401
    DATASET_VERSION,
    Dataset,
    DatasetIncomplete,
    DatasetTooLarge,
    entries_digest,
    fetch_verified,
)
