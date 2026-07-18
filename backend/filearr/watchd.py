"""Watch-mode supervisor process entrypoint (T5).

Run alongside the Procrastinate worker (e.g. as a dedicated compose service):

    python -m filearr.watchd

It runs :func:`filearr.worker.run_watch_supervisor`, which starts a watchfiles
watcher per enabled library that has ``watch_mode=True`` on a *local* root and
reconciles that set against library config on a timer (so toggling watch_mode or
editing a root takes effect without a restart). Filesystem changes debounce and
defer a normal full scan through the existing scan pipeline. Network roots are
refused (inotify is unreliable over SMB/NFS/FUSE-remote).
"""

from __future__ import annotations

import asyncio
import logging

from filearr.config import get_settings
from filearr.worker import run_watch_supervisor


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    try:
        asyncio.run(run_watch_supervisor())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
