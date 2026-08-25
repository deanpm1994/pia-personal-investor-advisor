"""Run the controlled Marketstack EOD schedule without an HTTP entry point."""

import asyncio
import json
from datetime import UTC, datetime

from pia_api.core.config import Settings
from pia_api.services.market_ingestion import run_market_eod


async def _main() -> None:
    result = await run_market_eod(Settings(), datetime.now(UTC))
    print(
        json.dumps(
            {
                "job_id": result.job_id,
                "status": result.status,
                "target_date": (
                    result.target_date.isoformat() if result.target_date else None
                ),
                "eligible_instruments": result.eligible_instruments,
                "fetched_instruments": result.fetched_instruments,
                "successful_instruments": result.successful_instruments,
                "diagnostics": result.diagnostics,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(_main())
