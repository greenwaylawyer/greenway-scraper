"""
Cost protection system for Google Places API.

Prevents surprise billing by enforcing environment-specific limits.
"""

import os
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv(override=True)


class CostProtectionError(Exception):
    """Raised when cost/usage limits are exceeded."""
    pass


class CostProtectionSystem:
    """
    Enforces API usage and cost limits based on environment.
    
    Local environment (APP_ENV=local):
        - Max 50 API calls per day
        - Max $2 cost per day
        - Max 10 profiles per batch
    
    Production environment (APP_ENV=production):
        - Max 10,000 API calls per day
        - Max $100 cost per day
        - Max 1,000 profiles per batch
    """

    # API cost constants (Google Places pricing as of 2026)
    FIND_PLACE_COST = 0.017  # $17 per 1,000 requests
    PLACE_DETAILS_COST = 0.017  # $17 per 1,000 requests

    def __init__(self):
        self.env = os.getenv('APP_ENV', 'local')
        self._load_limits()

    def _load_limits(self):
        """Load environment-specific limits from config."""
        if self.env == 'production':
            self.max_calls_per_day = int(os.getenv('GOOGLE_API_MAX_CALLS_PER_DAY_PRODUCTION', 10000))
            self.max_cost_per_day = float(os.getenv('GOOGLE_API_MAX_COST_PER_DAY_PRODUCTION', 100.00))
            self.max_batch_size = int(os.getenv('GOOGLE_API_MAX_BATCH_SIZE_PRODUCTION', 1000))
        else:
            # Default to local limits for safety
            self.max_calls_per_day = int(os.getenv('GOOGLE_API_MAX_CALLS_PER_DAY_LOCAL', 50))
            self.max_cost_per_day = float(os.getenv('GOOGLE_API_MAX_COST_PER_DAY_LOCAL', 2.00))
            self.max_batch_size = int(os.getenv('GOOGLE_API_MAX_BATCH_SIZE_LOCAL', 10))
        
        self.confirm_batch_size = int(os.getenv('GOOGLE_API_CONFIRM_BATCH_SIZE', 100))

    def _get_db_connection(self):
        """Create database connection."""
        return psycopg2.connect(
            host=os.getenv('SCRAPER_DB_HOST', '127.0.0.1'),
            port=int(os.getenv('SCRAPER_DB_PORT', 5433)),
            database=os.getenv('SCRAPER_DB_NAME', 'greenway_scraper'),
            user=os.getenv('SCRAPER_DB_USER', 'scraper'),
            password=os.getenv('SCRAPER_DB_PASSWORD', 'scraper_secret'),
        )

    def get_today_usage(self) -> Dict[str, Any]:
        """
        Get today's Google Places API usage from database.
        
        Returns:
            Dict with find_place_calls, place_details_calls, total_calls, estimated_cost
        """
        conn = self._get_db_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                # Count API calls made today
                cursor.execute(
                    """
                    SELECT 
                        COUNT(*) FILTER (
                            WHERE discovery_status IN ('found', 'manual')
                            AND discovery_completed_at::date = CURRENT_DATE
                        ) as find_place_calls,
                        COUNT(*) FILTER (
                            WHERE scrape_status = 'completed'
                            AND scraped_at::date = CURRENT_DATE
                        ) as place_details_calls
                    FROM enrichment_source_requests
                    WHERE source_key = 'google_maps'
                      AND created_at >= CURRENT_DATE
                    """
                )
                
                result = cursor.fetchone()
                
                find_place_calls = result['find_place_calls'] or 0
                place_details_calls = result['place_details_calls'] or 0
                total_calls = find_place_calls + place_details_calls
                
                # Calculate cost
                cost = (find_place_calls * self.FIND_PLACE_COST) + (place_details_calls * self.PLACE_DETAILS_COST)
                
                return {
                    'find_place_calls': find_place_calls,
                    'place_details_calls': place_details_calls,
                    'total_calls': total_calls,
                    'estimated_cost': round(cost, 2),
                    'max_calls_per_day': self.max_calls_per_day,
                    'max_cost_per_day': self.max_cost_per_day,
                    'calls_remaining': max(0, self.max_calls_per_day - total_calls),
                    'cost_remaining': max(0, self.max_cost_per_day - cost),
                }
        finally:
            conn.close()

    def check_batch_allowed(self, batch_size: int) -> Dict[str, Any]:
        """
        Check if a batch of the given size is allowed.
        
        Args:
            batch_size: Number of profiles to process
        
        Returns:
            Dict with allowed (bool), reason (str), usage (dict)
        
        Raises:
            CostProtectionError: If batch exceeds limits
        """
        usage = self.get_today_usage()
        
        # Check batch size limit
        if batch_size > self.max_batch_size:
            return {
                'allowed': False,
                'reason': (
                    f"Batch size {batch_size} exceeds {self.env} environment limit "
                    f"of {self.max_batch_size}. "
                    f"To process more, set APP_ENV=production or reduce batch size."
                ),
                'usage': usage,
            }
        
        # Estimate API calls for this batch (assume worst case: all need discovery + details)
        estimated_new_calls = batch_size * 2
        projected_total_calls = usage['total_calls'] + estimated_new_calls
        
        # Check daily call limit
        if projected_total_calls > self.max_calls_per_day:
            return {
                'allowed': False,
                'reason': (
                    f"This batch would use ~{estimated_new_calls} API calls. "
                    f"Today's usage: {usage['total_calls']}/{self.max_calls_per_day} calls. "
                    f"Would exceed daily limit by {projected_total_calls - self.max_calls_per_day} calls. "
                    f"Wait until tomorrow or increase limit for {self.env} environment."
                ),
                'usage': usage,
            }
        
        # Estimate cost for this batch
        estimated_new_cost = (batch_size * 2) * self.PLACE_DETAILS_COST
        projected_total_cost = usage['estimated_cost'] + estimated_new_cost
        
        # Check daily cost limit
        if projected_total_cost > self.max_cost_per_day:
            return {
                'allowed': False,
                'reason': (
                    f"This batch would cost ~${estimated_new_cost:.2f}. "
                    f"Today's cost: ${usage['estimated_cost']:.2f}/${self.max_cost_per_day:.2f}. "
                    f"Would exceed daily budget by ${projected_total_cost - self.max_cost_per_day:.2f}. "
                    f"Wait until tomorrow or increase limit for {self.env} environment."
                ),
                'usage': usage,
            }
        
        # Check if confirmation required
        needs_confirmation = batch_size >= self.confirm_batch_size
        
        return {
            'allowed': True,
            'needs_confirmation': needs_confirmation,
            'reason': (
                f"Batch allowed. Estimated cost: ${estimated_new_cost:.2f}. "
                f"Remaining today: {usage['calls_remaining']} calls, ${usage['cost_remaining']:.2f}."
            ),
            'usage': usage,
            'estimated_new_calls': estimated_new_calls,
            'estimated_new_cost': round(estimated_new_cost, 2),
        }

    def enforce_batch_limit(self, batch_size: int, force: bool = False) -> Dict[str, Any]:
        """
        Enforce batch size limit and raise exception if exceeded.
        
        Args:
            batch_size: Requested batch size
            force: Skip checks if True (use with extreme caution!)
        
        Returns:
            Check result dict
        
        Raises:
            CostProtectionError: If batch not allowed
        """
        if force:
            return {'allowed': True, 'forced': True}
        
        result = self.check_batch_allowed(batch_size)
        
        if not result['allowed']:
            raise CostProtectionError(result['reason'])
        
        return result

    def get_safe_batch_size(self, requested_size: int) -> int:
        """
        Get the maximum safe batch size for today.
        
        Args:
            requested_size: Desired batch size
        
        Returns:
            Safe batch size (may be smaller than requested)
        """
        usage = self.get_today_usage()
        
        # Calculate how many calls we can still make today
        calls_available = usage['calls_remaining']
        cost_available = usage['cost_remaining']
        
        # Each profile uses ~2 calls (find + details)
        max_by_calls = calls_available // 2
        max_by_cost = int(cost_available / (2 * self.PLACE_DETAILS_COST))
        
        # Take the minimum of all constraints
        safe_size = min(
            requested_size,
            self.max_batch_size,
            max_by_calls,
            max_by_cost,
        )
        
        return max(0, safe_size)

    def print_usage_report(self):
        """Print formatted usage report."""
        usage = self.get_today_usage()
        
        print("\n" + "=" * 60)
        print(f"GOOGLE PLACES API USAGE — {datetime.now().strftime('%Y-%m-%d')}")
        print("=" * 60)
        print(f"Environment:        {self.env.upper()}")
        print(f"API Calls Today:    {usage['total_calls']:,} / {usage['max_calls_per_day']:,}")
        print(f"  - Find Place:     {usage['find_place_calls']:,}")
        print(f"  - Place Details:  {usage['place_details_calls']:,}")
        print(f"Estimated Cost:     ${usage['estimated_cost']:.2f} / ${usage['max_cost_per_day']:.2f}")
        print(f"Remaining Today:    {usage['calls_remaining']:,} calls, ${usage['cost_remaining']:.2f}")
        print("=" * 60 + "\n")


def check_cost_protection(batch_size: int) -> Dict[str, Any]:
    """
    Convenience function to check if a batch is allowed.
    
    Args:
        batch_size: Number of profiles to process
    
    Returns:
        Check result dict
    
    Example:
        result = check_cost_protection(500)
        if not result['allowed']:
            print(f"ERROR: {result['reason']}")
            sys.exit(1)
    """
    protection = CostProtectionSystem()
    return protection.check_batch_allowed(batch_size)


if __name__ == '__main__':
    # Test the cost protection system
    protection = CostProtectionSystem()
    
    protection.print_usage_report()
    
    # Test various batch sizes
    test_sizes = [10, 50, 100, 500, 1000]
    
    print("Testing batch sizes:")
    print("-" * 60)
    
    for size in test_sizes:
        result = protection.check_batch_allowed(size)
        status = "✅ ALLOWED" if result['allowed'] else "❌ BLOCKED"
        print(f"{status} - Batch of {size:4d}: {result['reason'][:80]}...")
        
        if result['allowed'] and 'estimated_new_cost' in result:
            print(f"         Estimated cost: ${result['estimated_new_cost']:.2f}")
        print()
