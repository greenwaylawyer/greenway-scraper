"""Token bucket rate limiter for API and scraping requests."""

import asyncio
import time
from typing import Optional


class TokenBucketRateLimiter:
    """
    Token bucket rate limiter for async operations.
    
    Usage:
        limiter = TokenBucketRateLimiter(rate=10)  # 10 requests per minute
        await limiter.acquire()  # Wait until token available
        # ... make request ...
    """

    def __init__(self, rate: int, per: int = 60):
        """
        Initialize rate limiter.
        
        Args:
            rate: Number of requests allowed
            per: Time window in seconds (default 60 = per minute)
        """
        self.rate = rate
        self.per = per
        self.allowance = rate
        self.last_check = time.time()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """
        Acquire a token. Blocks until a token is available.
        """
        async with self._lock:
            current = time.time()
            time_passed = current - self.last_check
            self.last_check = current
            
            # Replenish tokens based on time passed
            self.allowance += time_passed * (self.rate / self.per)
            
            # Cap at max rate
            if self.allowance > self.rate:
                self.allowance = self.rate
            
            # If no tokens available, wait
            if self.allowance < 1.0:
                wait_time = (1.0 - self.allowance) * (self.per / self.rate)
                await asyncio.sleep(wait_time)
                self.allowance = 0.0
            else:
                self.allowance -= 1.0

    def reset(self) -> None:
        """Reset the rate limiter (for testing)."""
        self.allowance = self.rate
        self.last_check = time.time()
