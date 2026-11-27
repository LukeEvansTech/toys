"""Settings from the environment. Every value has a default that works on a laptop."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from collectorkit.config import Common, env_int, env_str


@dataclass(frozen=True)
class Settings:
    common: Common
    # One sweep a day; the mail goes out at the end of a sweep that found something to show.
    schedule: str
    # Most new listings judged in one sweep; the rest stay new for the next one.
    judge_cap: int

    @property
    def img_dir(self) -> Path:
        return self.common.data_dir / "img"


def load() -> Settings:
    s = Settings(
        common=Common.load("TOYS"),
        schedule=env_str("TOYS_SCHEDULE", "0 7 * * *"),
        judge_cap=env_int("TOYS_JUDGE_CAP", 80),
    )
    s.img_dir.mkdir(parents=True, exist_ok=True)
    return s
