"""Checkpoint manager for resume capability during long-running scrapes."""

import json
import os
from pathlib import Path
from datetime import datetime
from utils.logger import get_logger

logger = get_logger(__name__)


class CheckpointManager:
    """
    Manage checkpoints for scraper state persistence.

    Allows scrapers to resume from the last position after interruption.
    """

    def __init__(self, state: str):
        """
        Initialize checkpoint manager for a state.

        Args:
            state: State code (e.g., 'california', 'new_york')
        """
        self.state = state.lower()
        self.checkpoint_dir = Path("checkpoints")
        self.checkpoint_dir.mkdir(exist_ok=True)
        self.file_path = self.checkpoint_dir / f"{self.state}.json"

    def save(self, page: int, stats: dict = None):
        """
        Save checkpoint to disk.

        Args:
            page: Current page number
            stats: Optional statistics dictionary
        """
        checkpoint = {
            'state': self.state,
            'last_page': page,
            'timestamp': datetime.now().isoformat(),
            'stats': stats or {},
        }

        try:
            with open(self.file_path, 'w') as f:
                json.dump(checkpoint, f, indent=2)

            logger.info(
                "Checkpoint saved",
                state=self.state,
                page=page,
                stats=stats
            )
        except Exception as e:
            logger.error("Failed to save checkpoint", error=str(e))

    def load(self) -> dict | None:
        """
        Load checkpoint from disk.

        Returns:
            Checkpoint dictionary or None if not found
        """
        if not self.file_path.exists():
            logger.info("No checkpoint found", state=self.state)
            return None

        try:
            with open(self.file_path, 'r') as f:
                checkpoint = json.load(f)

            logger.info(
                "Checkpoint loaded",
                state=self.state,
                page=checkpoint.get('last_page'),
            )

            return checkpoint
        except Exception as e:
            logger.error("Failed to load checkpoint", error=str(e))
            return None

    def clear(self):
        """Remove checkpoint file."""
        if self.file_path.exists():
            self.file_path.unlink()
            logger.info("Checkpoint cleared", state=self.state)
