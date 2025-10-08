"""
Database connection pool management.

This module provides a DatabasePool class that manages asyncpg connection pools
with proper lifecycle management and dependency injection support.
"""

import asyncio
import logging
import threading
import time
import traceback
from dataclasses import dataclass

import asyncpg

logger = logging.getLogger(__name__)


@dataclass
class ConnectionInfo:
    """Information about an acquired connection."""

    connection_id: int
    acquired_at: float
    acquired_by_task: str | None
    stack_trace: str
    caller_info: str


class DatabasePoolError(Exception):
    """Exception raised when database pool operations fail."""


class DatabasePool:
    """Manages a PostgreSQL connection pool with lifecycle management."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        database: str = "postgres",
        port: int = 5432,
        min_size: int = 5,
        max_size: int = 20,
    ) -> None:
        """Initialize the database pool configuration.

        Args:
            host: Database host
            user: Database username
            password: Database password
            database: Database name
            port: Database port
            min_size: Minimum pool size
            max_size: Maximum pool size
        """
        self.host = host
        self.user = user
        self.password = password
        self.database = database
        self.port = port
        self.min_size = min_size
        self.max_size = max_size
        self.pool: asyncpg.Pool | None = None
        self._initialized = False
        # Connection tracking for leak detection
        self._active_connections: dict[int, ConnectionInfo] = {}
        self._tracking_lock = threading.Lock()

    async def initialize(self) -> None:
        """Initialize the connection pool."""
        if self._initialized:
            logger.warning("Database pool already initialized")
            return

        try:
            self.pool = await asyncpg.create_pool(
                host=self.host,
                user=self.user,
                password=self.password,
                database=self.database,
                port=self.port,
                min_size=self.min_size,
                max_size=self.max_size,
            )
            self._initialized = True
            logger.info(f"Database pool initialized (min={self.min_size}, max={self.max_size}) for {self.host}")
        except Exception as e:
            logger.exception(f"Failed to initialize database pool for {self.host}")
            raise DatabasePoolError(f"Pool initialization failed: {e}") from e

    async def close(self) -> None:
        """Close the connection pool."""
        if not self._initialized or self.pool is None:
            logger.warning("Database pool not initialized or already closed")
            return

        # Check for unreleased connections before closing
        with self._tracking_lock:
            if self._active_connections:
                logger.error(
                    f"CONNECTION LEAK DETECTED! Pool ({self.host}) has {len(self._active_connections)} "
                    f"unreleased connections. This will cause the pool closure to hang."
                )

                current_time = time.time()
                for conn_id, conn_info in self._active_connections.items():
                    age = current_time - conn_info.acquired_at
                    logger.error(
                        f"🔍 LEAKED CONNECTION {conn_id}:\n"
                        f"  📍 Acquired by: {conn_info.caller_info}\n"
                        f"  🕐 Acquired at: {time.ctime(conn_info.acquired_at)} ({age:.1f}s ago)\n"
                        f"  🏷️  Task: {conn_info.acquired_by_task}\n"
                        f"  📚 Stack trace:\n{conn_info.stack_trace}"
                    )

                logger.error(
                    f"🔧 LEAK SUMMARY for pool ({self.host}):\n"
                    f"  • Total leaked connections: {len(self._active_connections)}\n"
                    f"  • Pool will now attempt to close (this may hang for up to 60s)\n"
                    f"  • Fix the code at the locations shown above to use proper context managers"
                )

        try:
            logger.info(f"Attempting to close database pool for {self.host}...")
            await self.pool.close()
            self.pool = None
            self._initialized = False

            # Clear tracking after successful close
            with self._tracking_lock:
                self._active_connections.clear()

            logger.info(f"Database pool closed successfully for {self.host}")
        except Exception as e:
            logger.exception(f"Error closing database pool for {self.host}")
            raise DatabasePoolError(f"Pool closure failed: {e}") from e

    async def acquire(self) -> asyncpg.Connection:
        """Acquire a connection from the pool.

        Returns:
            Database connection from the pool

        Raises:
            DatabasePoolError: If pool not initialized or acquisition fails
        """
        if not self._initialized or self.pool is None:
            raise DatabasePoolError("Database pool not initialized. Call initialize() first.")

        try:
            # Add timeout to prevent infinite waits during database issues
            conn = await asyncio.wait_for(self.pool.acquire(), timeout=30.0)

            # Track connection with caller information
            conn_id = id(conn)
            current_time = time.time()

            # Capture stack trace (exclude this method and asyncpg internals)
            stack = traceback.extract_stack()[:-1]
            stack_trace = "".join(traceback.format_list(stack))

            # Get current task name if available
            try:
                current_task = asyncio.current_task()
                task_name = current_task.get_name() if current_task else None
            except:
                task_name = None

            # Find the first meaningful caller (skip internal pool/connector methods)
            caller_info = "unknown"
            for frame_summary in reversed(stack):
                filename = frame_summary.filename
                function_name = frame_summary.name
                line_no = frame_summary.lineno

                # Skip internal pool and asyncpg methods
                if (
                    "database_pool.py" not in filename
                    and "postgres.py" not in filename
                    and "asyncpg" not in filename
                    and function_name != "acquire"
                ):
                    caller_info = f"{filename}:{function_name}():{line_no}"
                    break

            # Store connection info
            connection_info = ConnectionInfo(
                connection_id=conn_id,
                acquired_at=current_time,
                acquired_by_task=task_name,
                stack_trace=stack_trace,
                caller_info=caller_info,
            )

            with self._tracking_lock:
                self._active_connections[conn_id] = connection_info

            logger.debug(
                f"Acquired connection {conn_id} from pool ({self.host}) "
                f"by {caller_info} (task: {task_name}). "
                f"Active connections: {len(self._active_connections)}"
            )

            return conn
        except TimeoutError:
            logger.error(
                f"Connection acquisition timeout (30s) from pool ({self.host}). "
                f"This may indicate database issues or pool exhaustion."
            )
            raise DatabasePoolError(f"Connection acquisition timeout from pool ({self.host})")
        except Exception as e:
            logger.exception(f"Failed to acquire connection from pool ({self.host})")
            raise DatabasePoolError(f"Connection acquisition failed: {e}") from e

    async def release(self, connection: asyncpg.Connection) -> None:
        """Release a connection back to the pool.

        Args:
            connection: Connection to release back to the pool

        Raises:
            DatabasePoolError: If pool not initialized or release fails
        """
        if not self._initialized or self.pool is None:
            raise DatabasePoolError("Database pool not initialized")

        try:
            conn_id = id(connection)

            # Remove from tracking
            connection_info = None
            with self._tracking_lock:
                connection_info = self._active_connections.pop(conn_id, None)

            if connection_info:
                hold_time = time.time() - connection_info.acquired_at
                logger.debug(
                    f"Released connection {conn_id} to pool ({self.host}) "
                    f"acquired by {connection_info.caller_info} "
                    f"(held for {hold_time:.2f}s). Active connections: {len(self._active_connections)}"
                )
            else:
                logger.warning(
                    f"Released untracked connection {conn_id} to pool ({self.host}). "
                    f"This may indicate a connection tracking issue."
                )

            await self.pool.release(connection)
        except Exception as e:
            logger.exception(f"Failed to release connection to pool ({self.host})")
            raise DatabasePoolError(f"Connection release failed: {e}") from e

    @property
    def is_initialized(self) -> bool:
        """Check if the pool is initialized."""
        return self._initialized

    def get_active_connections_count(self) -> int:
        """Get the number of active (unreleased) connections being tracked."""
        with self._tracking_lock:
            return len(self._active_connections)

    def get_connection_stats(self) -> dict[str, any]:
        """Get detailed connection statistics for monitoring."""
        if not self.pool:
            return {"error": "pool not initialized"}

        with self._tracking_lock:
            active_count = len(self._active_connections)

            # Get the oldest connection age if any exist
            oldest_age = 0.0
            current_time = time.time()
            if self._active_connections:
                oldest_acquisition = min(conn.acquired_at for conn in self._active_connections.values())
                oldest_age = current_time - oldest_acquisition

        return {
            "pool_size": self.pool.get_size(),
            "min_size": self.pool.get_min_size(),
            "max_size": self.pool.get_max_size(),
            "idle_count": self.pool.get_idle_size(),
            "used_count": self.pool.get_size() - self.pool.get_idle_size(),
            "tracked_active": active_count,
            "oldest_connection_age_seconds": oldest_age,
        }

    def log_active_connections(self) -> None:
        """Log information about all currently active connections."""
        with self._tracking_lock:
            if not self._active_connections:
                logger.info(f"Pool ({self.host}) has no active tracked connections")
                return

            logger.info(f"Pool ({self.host}) has {len(self._active_connections)} active connections:")
            current_time = time.time()
            for conn_id, conn_info in self._active_connections.items():
                age = current_time - conn_info.acquired_at
                logger.info(
                    f"  Connection {conn_id}: acquired {age:.1f}s ago by {conn_info.caller_info} "
                    f"(task: {conn_info.acquired_by_task})"
                )

    def __repr__(self) -> str:
        """String representation of the pool."""
        status = "initialized" if self._initialized else "not initialized"
        active_count = self.get_active_connections_count()
        return f"DatabasePool(host={self.host}, database={self.database}, {status}, active={active_count})"
