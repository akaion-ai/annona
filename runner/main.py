"""
Runner Daemon

Annona — local-first second brain with optional one-way push to a
remote backend. The daemon serves a FastAPI UI on 127.0.0.1:<port> and runs
no background polling: it never receives tasks from the cloud, it only
pushes notes the user explicitly creates locally.
"""

import os
import signal
import time
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

from .auth import AuthManager
from .banner import print_runner_banner
from .brain.capture import capture_task_as_note
from .brain.manager import BrainManager
from .config import ConfigManager
from .executor import TaskExecutor
from .local_api import LocalAPIServer
from .service_urls import resolve_service_url
from .sync.engine import SyncEngine

DEFAULT_BRAIN_DIR = Path.home() / "akaion-brain"
DEFAULT_LOCAL_PORT = 7070


EXIT_CODES = {"completed": 0, "held": 3, "queued": 4}
"""How `annona run --once` ends, per outcome. 1 is a failure, 2 a usage error."""


def step_outcome(result: Any) -> tuple[str, str]:
    """``("completed" | "held" | "queued", reason)`` for a step that did not raise.

    A step the perimeter refused does not raise — the loop stops and returns
    what it has — so "no exception" is not "done". Read from the placement the
    enforced path attaches; the legacy path has none and is always completed.
    """
    inner = result.get("result") if isinstance(result, dict) else None
    placement = inner.get("placement") if isinstance(inner, dict) else None
    if isinstance(placement, dict) and placement.get("outcome") in ("held", "queued"):
        return str(placement["outcome"]), str(placement.get("reason", ""))
    return "completed", ""


class RunnerDaemon:
    """Local daemon — Brain UI + on-demand push to remote COT."""

    def __init__(
        self,
        config: Dict[str, Any],
        dev_mode: bool = False,
        brain_dir: Optional[Path] = None,
        local_port: int = DEFAULT_LOCAL_PORT,
    ):
        self.config = config
        self.dev_mode = dev_mode
        self.running = False
        self.tasks_executed = 0
        self.local_port = local_port

        self._setup_logging()

        self.auth_manager = AuthManager()
        self.config_manager = ConfigManager()

        cloud_enabled = bool(config.get("cloud", {}).get("enabled", False))
        is_authed = self.auth_manager.is_authenticated()
        self._cloud_enabled = cloud_enabled
        self._cloud_authed = is_authed

        self.executor = TaskExecutor(config)

        self.max_concurrent = config.get("runner", {}).get("max_concurrent_tasks", 3)

        cfg_capture = bool(config.get("runner", {}).get("capture_to_brain", True))
        env_capture = os.environ.get("AKAION_CAPTURE_TO_BRAIN")
        if env_capture is not None:
            self.capture_to_brain = env_capture.strip().lower() in ("1", "true", "yes", "on")
        else:
            self.capture_to_brain = cfg_capture

        _brain_dir = brain_dir or Path(config.get("brain", {}).get("dir", str(DEFAULT_BRAIN_DIR)))
        self.brain = BrainManager(_brain_dir)
        self.sync = SyncEngine(
            brain=self.brain,
            cot_url=resolve_service_url("cot"),
            auth=self.auth_manager,
        )

        self.local_api = LocalAPIServer(
            self.brain,
            self.sync,
            auth=self.auth_manager,
            port=local_port,
            cloud_enabled=cloud_enabled,
            # The window asks the kernel questions through this executor; without
            # it the API can read the perimeter but not run anything through it.
            executor=self.executor,
        )

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    @staticmethod
    def _resolve_log_file(configured: str) -> Path:
        """Where the log actually goes, never relative to the current directory.

        A relative path is resolved against the runner's home, not against
        wherever the process happens to have been started. That distinction is
        not pedantry: launched from a terminal the working directory is a
        project folder and `logs/runner.log` works; launched from the Dock it is
        `/`, and the daemon died on startup with

            [Errno 30] Read-only file system: 'logs'

        which is invisible in development and fatal in the packaged app.
        """
        path = Path(configured).expanduser()
        if path.is_absolute():
            return path

        home = Path(os.getenv("ANNONA_HOME") or os.getenv("AKAION_HOME") or Path.home() / ".annona")
        return home.expanduser() / path

    def _setup_logging(self):
        log_config = self.config.get("logging", {})
        log_level = log_config.get("level", "INFO")
        log_file = self._resolve_log_file(log_config.get("file", "logs/annona.log"))

        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # A daemon that cannot write a log still has work to do. Console
            # logging stays; the file sink is skipped and said so.
            logger.remove()
            logger.add(
                lambda msg: print(msg, end=""),
                format=(
                    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                    "<level>{level: <8}</level> | <level>{message}</level>"
                ),
                level=log_level,
            )
            logger.warning(f"cannot write logs to {log_file}; continuing without a log file")
            if self.dev_mode:
                logger.info("🔧 Development mode enabled")
            return

        logger.remove()
        logger.add(
            lambda msg: print(msg, end=""),
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
            level=log_level,
        )
        logger.add(
            str(log_file),
            rotation="1 day",
            retention="7 days",
            level=log_level,
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function} - {message}",
        )

        if self.dev_mode:
            logger.info("🔧 Development mode enabled")

    def _signal_handler(self, signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        self.stop()

    def start_daemon(self):
        self.running = True
        self.local_api.start()

        print_runner_banner()
        logger.info("🚀 Annona started")

        if self._cloud_enabled and self._cloud_authed:
            logger.info("Cloud push enabled — notes can be pushed to remote on demand")
        elif not self._cloud_enabled:
            logger.info("Running in local-only mode (cloud.enabled=false — no remote push)")
        else:
            logger.info(
                "Running in local-only mode (not authenticated — login via UI or `annona login`)"
            )

        logger.info("Press Ctrl+C to stop")

        try:
            while self.running:
                # Idle loop — keep the process alive so the FastAPI server in
                # the background thread stays up. No cloud polling.
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        except Exception as e:
            logger.error(f"Daemon error: {e}", exc_info=True)
        finally:
            self.stop()

    def execute_once(self, task_description: str) -> Any:
        """Run a single ad-hoc task (CLI entry point)."""
        logger.info(f"🎯 Executing task: {task_description}")

        task = {
            "id": "local-" + str(int(time.time())),
            "type": "command",
            "description": task_description,
            "payload": {"command": task_description},
        }
        return self._execute_task(task)

    def _execute_task(self, task: Dict[str, Any]) -> Any:
        task_id = task.get("id")
        logger.info(f"⚙️  Executing task {task_id}")

        try:
            result = self.executor.execute(task)
            outcome, reason = step_outcome(result)
            if outcome == "completed":
                logger.info(f"✅ Task {task_id} completed")
            else:
                logger.warning(f"⛔ Task {task_id} {outcome}: {reason}")
            self._capture_task_to_brain(task, result)
            self.tasks_executed += 1
            return result
        except Exception as e:
            logger.error(f"❌ Task {task_id} failed: {e}", exc_info=True)
            self._capture_task_to_brain(task, {"success": False, "error": str(e)})
            raise

    def _capture_task_to_brain(self, task: Dict[str, Any], result: Any) -> None:
        if not self.capture_to_brain:
            return
        try:
            capture_task_as_note(self.brain, task, result)
        except Exception as e:  # noqa: BLE001 — safety net
            logger.warning(f"Failed to capture task to brain: {e}")

    def stop(self):
        if not self.running:
            return
        self.running = False
        logger.info("🛑 Runner stopped")
        self.local_api.stop()
        self.brain.close()
        logger.info(f"📊 Total tasks executed: {self.tasks_executed}")
