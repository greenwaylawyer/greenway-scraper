"""Token bucket rate limiter for respectful scraping."""

import time
import asyncio
from utils.logger import get_logger

logger = get_logger(__name__)


class TokenBucketRateLimiter:
    """
    Token bucket algorithm for rate limiting.

    Allows bursts up to bucket_size, then refills at rate per minute.
    """

    def __init__(self, rate: int = 20):
        """
        Initialize rate limiter.

        Args:
            rate: Tokens per minute
        """
        self.rate = rate  # tokens per minute
        self.tokens = rate
        self.last_refill = time.time()
        self.bucket_size = rate * 2  # Allow bursts up to 2x rate
        self.bucket_size = max(10, self.bucket_size)  # Minimum bucket size

    async def acquire(self):
        """
        Acquire a token, waiting if necessary.

        Implements token bucket algorithm:
        - Refills tokens based on time elapsed
        - Waits if bucket is empty
        - Returns immediately if tokens are available
        """
        now = time.time()
        elapsed = now - self.last_refill

        # Refill tokens based on elapsed time
        # Formula: new_tokens = elapsed * rate / 60
        self.tokens = min(
            self.bucket_size,
            self.tokens + elapsed * self.rate / 60
        )
        self.last_refill = now

        # Wait if bucket is empty
        if self.tokens < 1:
            wait_time = (1 - self.tokens) * 60 / self.rate
            logger.debug(
                "Rate limit reached, waiting",
                wait_seconds=wait_time,
                tokens=self.tokens
            )
            await asyncio.sleep(wait_time)
            self.tokens = 0
        else:
            self.tokens -= 1

    def can_acquire(self) -> bool:
        """
        Check if a token is available without acquiring.

        Returns:
            True if token available, False otherwise
        """
        return self.tokens >= 1
