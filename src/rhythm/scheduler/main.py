"""``rhythm-scheduler``: the scheduler process (one per deployment; extra ones stand by)."""

from __future__ import annotations

import signal
import sys
from types import FrameType

from rhythm.api.app import create_app
from rhythm.scheduler.service import Scheduler


def main() -> int:  # pragma: no cover — process entrypoint
    app = create_app()
    sched = Scheduler(app.state.rhythm)

    def stop(_sig: int, _frame: FrameType | None) -> None:
        sched.release()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    sched.run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
