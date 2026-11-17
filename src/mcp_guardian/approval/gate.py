"""Approval gate: hold MCP tool calls until the user Allow/Blocks."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from mcp_guardian.messages import Decision, PendingRequest

logger = logging.getLogger(__name__)

PresenterFn = Callable[[PendingRequest], Awaitable[Decision]]
_UNSET = object()


class ApprovalTimeout(Exception):
    """User did not decide before the approval timeout."""


class ApprovalGate:
    """
    Concurrent-safe gate. Many requests may wait at once; the UI shows them
    one dialog at a time via the presenter / UI daemon.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 120.0,
        presenter: PresenterFn | None = None,
        auto_decision: Decision | None | object = _UNSET,
        use_ui_daemon: bool = True,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._presenter = presenter
        self._use_ui_daemon = use_ui_daemon
        # Pass auto_decision=None to force interactive/presenter mode even if
        # MCP_GUARDIAN_AUTO_DECISION is set in the environment.
        if auto_decision is _UNSET:
            self._auto_decision = _env_auto_decision()
        else:
            self._auto_decision = auto_decision  # type: ignore[assignment]
        self._popup: Any | None = None
        self._pending: dict[str, asyncio.Future[Decision]] = {}
        self._lock = asyncio.Lock()
        self._ui_ready = False

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def request_approval(self, request: PendingRequest) -> Decision:
        if self._auto_decision is not None:
            logger.info(
                "auto-%s %s (id=%s)",
                self._auto_decision.value,
                request.summary(),
                request.request_id,
            )
            return self._auto_decision

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Decision] = loop.create_future()

        async with self._lock:
            self._pending[request.request_id] = future

        try:
            if self._presenter is not None:
                decision = await asyncio.wait_for(
                    self._presenter(request), timeout=self.timeout_seconds
                )
                if not future.done():
                    future.set_result(decision)
                return decision

            # Prefer UI daemon: Claude Desktop often strips DISPLAY from MCP children.
            if self._use_ui_daemon:
                decision = await asyncio.wait_for(
                    self._via_ui_daemon(request), timeout=self.timeout_seconds
                )
                if not future.done():
                    future.set_result(decision)
                return decision

            self._ensure_popup()
            assert self._popup is not None

            def on_decision(decision: Decision) -> None:
                loop.call_soon_threadsafe(self._resolve, request.request_id, decision)

            self._popup.ask(request, on_decision)
            return await asyncio.wait_for(future, timeout=self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            logger.warning(
                "approval timed out for %s (id=%s); blocking",
                request.summary(),
                request.request_id,
            )
            self._resolve(request.request_id, Decision.BLOCK)
            raise ApprovalTimeout(str(request.request_id)) from exc
        finally:
            async with self._lock:
                self._pending.pop(request.request_id, None)

    async def _via_ui_daemon(self, request: PendingRequest) -> Decision:
        from mcp_guardian.approval import ipc

        if not self._ui_ready:
            ok = await asyncio.to_thread(ipc.ensure_ui_daemon)
            if not ok:
                # Last resort: try in-process Tk after repairing DISPLAY.
                from mcp_guardian.approval.display import ensure_display_env

                ensure_display_env()
                self._use_ui_daemon = False
                self._ensure_popup()
                loop = asyncio.get_running_loop()
                fut: asyncio.Future[Decision] = loop.create_future()

                def on_decision(decision: Decision) -> None:
                    if not fut.done():
                        fut.set_result(decision)

                assert self._popup is not None
                self._popup.ask(request, on_decision)
                return await fut
            self._ui_ready = True

        return await asyncio.to_thread(
            ipc.request_decision, request, self.timeout_seconds
        )

    def _resolve(self, request_id: str, decision: Decision) -> None:
        fut = self._pending.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result(decision)

    def _ensure_popup(self) -> None:
        if self._popup is None:
            from mcp_guardian.approval.display import ensure_display_env
            from mcp_guardian.approval.popup import PopupPresenter

            ensure_display_env()
            self._popup = PopupPresenter()
            self._popup.start()

    async def shutdown(self) -> None:
        async with self._lock:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_result(Decision.BLOCK)
            self._pending.clear()
        if self._popup is not None:
            self._popup.stop()
            self._popup = None


def _env_auto_decision() -> Decision | None:
    raw = os.environ.get("MCP_GUARDIAN_AUTO_DECISION", "").strip().lower()
    if raw in ("allow", "block"):
        return Decision(raw)
    return None
