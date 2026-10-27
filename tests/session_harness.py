"""Runs desktop/cairn_session.py for tests: the local test server lives on 127.0.0.1, which the
session normally refuses as a private address. Nothing else is changed."""

import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))

import cairn_session  # noqa: E402

cairn_session.blocked_reason = lambda host: None
signal.signal(signal.SIGTERM, cairn_session._on_signal)
raise SystemExit(cairn_session.main())
