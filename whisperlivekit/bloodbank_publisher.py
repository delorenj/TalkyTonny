"""Bloodbank event publisher for WhisperLiveKit transcription events."""

import asyncio
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID

logger = logging.getLogger(__name__)


class BloodbankPublisher:
    """Publishes transcription events to Bloodbank event bus via bb CLI."""

    def __init__(
        self,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        enable_wal: bool = True,
        wal_path: Optional[Path] = None,
    ):
        """Initialize the Bloodbank publisher.

        Args:
            max_retries: Maximum number of publish retry attempts
            retry_delay: Delay in seconds between retry attempts
            enable_wal: Enable write-ahead log for durability
            wal_path: Path to WAL file (defaults to ./raw_voice_ingest.jsonl)
        """
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.enable_wal = enable_wal
        self.wal_path = wal_path or Path("raw_voice_ingest.jsonl")
        self._bb_available: Optional[bool] = None

    async def check_bb_available(self) -> bool:
        """Check if bb command is available.

        Returns:
            True if bb command exists and is executable
        """
        if self._bb_available is not None:
            return self._bb_available

        try:
            process = await asyncio.create_subprocess_exec(
                "which", "bb",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await process.communicate()
            self._bb_available = process.returncode == 0 and bool(stdout.strip())
            if not self._bb_available:
                logger.warning("bb command not found - events will only be written to WAL")
            return self._bb_available
        except Exception as e:
            logger.warning(f"Error checking bb availability: {e}")
            self._bb_available = False
            return False

    async def write_to_wal(self, event_data: dict) -> None:
        """Write event to write-ahead log for durability.

        Args:
            event_data: Event payload to persist

        Raises:
            IOError: If WAL write fails
        """
        try:
            # Ensure parent directory exists
            self.wal_path.parent.mkdir(parents=True, exist_ok=True)

            # Append to WAL (atomic on most filesystems)
            with open(self.wal_path, "a") as f:
                f.write(json.dumps(event_data) + "\n")

            logger.debug(f"Wrote event to WAL: {self.wal_path}")
        except Exception as e:
            logger.error(f"CRITICAL: Failed to write to WAL: {e}")
            raise

    async def publish_transcription(
        self,
        text: str,
        session_id: UUID,
        source: str = "whisperlivekit",
        target: Optional[str] = None,
        audio_metadata: Optional[dict] = None,
        context: Optional[dict] = None,
    ) -> bool:
        """Publish a transcription event to Bloodbank.

        Args:
            text: Transcribed text
            session_id: WebSocket session UUID
            source: Source service (default: whisperlivekit)
            target: Optional target service/consumer
            audio_metadata: Optional audio metadata dict
            context: Optional additional context

        Returns:
            True if publish succeeded, False otherwise
        """
        if not text.strip():
            logger.debug("Skipping empty transcription")
            return False

        # Construct event payload matching HolyFields schema
        timestamp = datetime.now(timezone.utc)
        event_payload = {
            "text": text.strip(),
            "timestamp": timestamp.isoformat(),
            "source": source,
            "session_id": str(session_id),
        }

        if target:
            event_payload["target"] = target

        if audio_metadata:
            event_payload["audio_metadata"] = audio_metadata

        if context:
            event_payload["context"] = context

        # Write to WAL first for durability
        if self.enable_wal:
            try:
                await self.write_to_wal(event_payload)
            except Exception as e:
                logger.error(f"WAL write failed - event may be lost: {e}")
                # Continue attempting to publish anyway

        # Check if bb is available
        if not await self.check_bb_available():
            logger.debug("bb not available, event saved to WAL only")
            return False

        # Attempt to publish via bb with retries
        for attempt in range(1, self.max_retries + 1):
            try:
                success = await self._publish_via_bb(event_payload)
                if success:
                    logger.info(
                        f"Published transcription event (session={session_id}, "
                        f"text_length={len(text)}, attempt={attempt})"
                    )
                    return True

                if attempt < self.max_retries:
                    logger.warning(
                        f"Publish attempt {attempt} failed, retrying in {self.retry_delay}s..."
                    )
                    await asyncio.sleep(self.retry_delay)
            except Exception as e:
                logger.error(f"Publish attempt {attempt} raised exception: {e}")
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_delay)

        logger.error(
            f"Failed to publish after {self.max_retries} attempts. "
            f"Event is in WAL: {self.wal_path}"
        )
        return False

    async def _publish_via_bb(self, event_payload: dict) -> bool:
        """Publish event using bb CLI command.

        Args:
            event_payload: Event payload dict

        Returns:
            True if publish succeeded, False otherwise
        """
        try:
            # Use bb publish command with JSON payload via stdin
            payload_json = json.dumps(event_payload)

            process = await asyncio.create_subprocess_exec(
                "bb", "publish",
                "transcription.voice.completed",
                "--json", "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await process.communicate(input=payload_json.encode())

            if process.returncode == 0:
                logger.debug(f"bb publish succeeded: {stdout.decode().strip()}")
                return True
            else:
                logger.error(
                    f"bb publish failed (exit={process.returncode}): "
                    f"{stderr.decode().strip()}"
                )
                return False

        except FileNotFoundError:
            logger.error("bb command not found - is Bloodbank installed?")
            return False
        except Exception as e:
            logger.exception(f"Unexpected error in bb publish: {e}")
            return False

    async def replay_wal(self) -> int:
        """Replay events from WAL (for recovery after downtime).

        Returns:
            Number of events successfully replayed
        """
        if not self.wal_path.exists():
            logger.info("No WAL file to replay")
            return 0

        if not await self.check_bb_available():
            logger.warning("Cannot replay WAL - bb not available")
            return 0

        replayed = 0
        failed_lines = []

        try:
            with open(self.wal_path, "r") as f:
                for line_num, line in enumerate(f, start=1):
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        event_data = json.loads(line)
                        success = await self._publish_via_bb(event_data)
                        if success:
                            replayed += 1
                        else:
                            failed_lines.append((line_num, line))
                    except json.JSONDecodeError:
                        logger.error(f"Invalid JSON in WAL line {line_num}")
                        failed_lines.append((line_num, line))

            # Rewrite WAL with only failed lines
            if failed_lines:
                logger.warning(f"Keeping {len(failed_lines)} failed events in WAL")
                with open(self.wal_path, "w") as f:
                    for _, line in failed_lines:
                        f.write(line + "\n")
            else:
                # All events replayed successfully - clear WAL
                self.wal_path.unlink()
                logger.info("WAL cleared after successful replay")

            logger.info(f"Replayed {replayed} events from WAL")
            return replayed

        except Exception as e:
            logger.exception(f"Error replaying WAL: {e}")
            return replayed
