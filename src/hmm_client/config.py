from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    hmm_host: str
    hmm_user: str
    hmm_password: str
    ibmc_user: str
    ibmc_password: str
    verify_tls: bool
    snapshots_dir: Path
    git_host: str
    git_owner: str
    git_repo: str
    git_token: str

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            hmm_host=os.environ["HMM_HOST"],
            hmm_user=os.environ["HMM_USER"],
            hmm_password=os.environ["HMM_PASSWORD"],
            ibmc_user=os.environ.get("IBMC_USER", os.environ["HMM_USER"]),
            ibmc_password=os.environ.get("IBMC_PASSWORD", os.environ["HMM_PASSWORD"]),
            verify_tls=os.environ.get("HMM_VERIFY_TLS", "false").lower() == "true",
            snapshots_dir=Path(os.environ.get("SNAPSHOTS_DIR", "./snapshots")).resolve(),
            git_host=os.environ.get("GIT_HOST", ""),
            git_owner=os.environ.get("GIT_OWNER", ""),
            git_repo=os.environ.get("GIT_REPO", ""),
            git_token=os.environ.get("GIT_TOKEN", ""),
        )
