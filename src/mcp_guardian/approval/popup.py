"""Always-on-top tkinter confirmation dialogs for pending MCP requests."""

from __future__ import annotations

import json
import logging
import queue
import threading
from typing import TYPE_CHECKING, Any, Callable

from mcp_guardian.messages import Decision, PendingRequest

if TYPE_CHECKING:
    import tkinter as tk

logger = logging.getLogger(__name__)


def _import_tk() -> tuple[Any, Any]:
    try:
        import tkinter as tk
        from tkinter import ttk
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on OS packages
        raise RuntimeError(
            "tkinter is not available. On Debian/Ubuntu install: sudo apt install python3-tk"
        ) from exc
    return tk, ttk


class PopupPresenter:
    """
    Runs a dedicated Tk event loop and shows serial Allow/Block dialogs.

    Callers submit a PendingRequest via ``ask``; the decision is delivered
    through the provided callback (safe to schedule onto an asyncio loop).
    """

    def __init__(self) -> None:
        self._inbox: queue.Queue[
            tuple[PendingRequest, Callable[[Decision], None]] | None
        ] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._root: Any | None = None
        self._tk: Any | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="mcp-guardian-ui", daemon=True
        )
        self._thread.start()
        if not self._started.wait(timeout=5):
            raise RuntimeError("MCP Guardian UI thread failed to start")

    def stop(self) -> None:
        self._inbox.put(None)
        if self._root is not None and self._tk is not None:
            try:
                self._root.after(0, self._root.quit)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def ask(
        self, request: PendingRequest, on_decision: Callable[[Decision], None]
    ) -> None:
        self.start()
        self._inbox.put((request, on_decision))

    def _run(self) -> None:
        try:
            tk, ttk = _import_tk()
            self._tk = tk
            root = tk.Tk()
        except Exception as exc:
            logger.error("Cannot start Tk UI: %s", exc)
            self._started.set()
            while True:
                item = self._inbox.get()
                if item is None:
                    break
                _, on_decision = item
                on_decision(Decision.BLOCK)
            return

        self._root = root
        root.withdraw()
        root.title("MCP Guardian")
        self._started.set()
        self._poll_inbox(ttk)
        try:
            root.mainloop()
        finally:
            try:
                root.destroy()
            except Exception:
                pass
            self._root = None

    def _poll_inbox(self, ttk: Any) -> None:
        assert self._root is not None
        try:
            while True:
                item = self._inbox.get_nowait()
                if item is None:
                    self._root.quit()
                    return
                request, on_decision = item
                self._show_dialog(ttk, request, on_decision)
        except queue.Empty:
            pass
        self._root.after(100, lambda: self._poll_inbox(ttk))

    def _show_dialog(
        self,
        ttk: Any,
        request: PendingRequest,
        on_decision: Callable[[Decision], None],
    ) -> None:
        assert self._root is not None and self._tk is not None
        tk = self._tk
        decided = {"done": False}

        dialog = tk.Toplevel(self._root)
        dialog.title("MCP Guardian — Confirm Request")
        dialog.attributes("-topmost", True)
        dialog.resizable(True, True)
        dialog.minsize(480, 360)
        dialog.geometry("560x420")
        try:
            dialog.lift()
            dialog.focus_force()
            dialog.grab_set()
        except Exception:
            pass

        # Use classic tk widgets for buttons — ttk themes on some Linux desktops
        # render Button as empty flat strips with invisible labels.
        bg = "#f4f4f5"
        dialog.configure(bg=bg)
        frame = tk.Frame(dialog, bg=bg, padx=16, pady=16)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            frame,
            text="An MCP tool invocation is pending your approval",
            font=("Sans", 12, "bold"),
            bg=bg,
            fg="#111827",
            anchor="w",
        ).pack(anchor=tk.W, fill=tk.X)

        meta = (
            f"Server: {request.server_name}\n"
            f"Tool:   {request.tool_name}\n"
            f"Method: {request.method}\n"
            f"Time:   {request.created_at.isoformat()}\n"
            f"ID:     {request.request_id}"
        )
        tk.Label(
            frame,
            text=meta,
            justify=tk.LEFT,
            font=("monospace", 10),
            bg=bg,
            fg="#1f2937",
            anchor="w",
        ).pack(anchor=tk.W, fill=tk.X, pady=(12, 8))

        tk.Label(frame, text="Arguments:", bg=bg, fg="#111827", anchor="w").pack(
            anchor=tk.W
        )
        args_box = tk.Text(
            frame,
            height=12,
            wrap=tk.WORD,
            font=("monospace", 10),
            bg="#ffffff",
            fg="#111827",
            relief=tk.SOLID,
            borderwidth=1,
            padx=8,
            pady=8,
        )
        args_box.pack(fill=tk.BOTH, expand=True, pady=(4, 12))
        try:
            pretty = json.dumps(request.arguments, indent=2, ensure_ascii=False)
        except TypeError:
            pretty = str(request.arguments)
        args_box.insert("1.0", pretty)
        args_box.configure(state=tk.DISABLED)

        btn_row = tk.Frame(frame, bg=bg)
        btn_row.pack(fill=tk.X, pady=(4, 0))

        def finish(decision: Decision) -> None:
            if decided["done"]:
                return
            decided["done"] = True
            try:
                dialog.grab_release()
            except Exception:
                pass
            dialog.destroy()
            on_decision(decision)

        # Explicit colors/padding so labels stay readable across themes.
        tk.Button(
            btn_row,
            text="Block",
            command=lambda: finish(Decision.BLOCK),
            font=("Sans", 11, "bold"),
            bg="#dc2626",
            fg="#ffffff",
            activebackground="#b91c1c",
            activeforeground="#ffffff",
            relief=tk.RAISED,
            borderwidth=1,
            padx=18,
            pady=8,
            cursor="hand2",
        ).pack(side=tk.RIGHT, padx=(8, 0))

        tk.Button(
            btn_row,
            text="Allow",
            command=lambda: finish(Decision.ALLOW),
            font=("Sans", 11, "bold"),
            bg="#16a34a",
            fg="#ffffff",
            activebackground="#15803d",
            activeforeground="#ffffff",
            relief=tk.RAISED,
            borderwidth=1,
            padx=18,
            pady=8,
            cursor="hand2",
        ).pack(side=tk.RIGHT)

        hint = tk.Label(
            btn_row,
            text="Enter = Allow · Esc = Block",
            font=("Sans", 9),
            bg=bg,
            fg="#6b7280",
            anchor="w",
        )
        hint.pack(side=tk.LEFT)

        dialog.protocol("WM_DELETE_WINDOW", lambda: finish(Decision.BLOCK))
        dialog.bind("<Escape>", lambda _e: finish(Decision.BLOCK))
        dialog.bind("<Return>", lambda _e: finish(Decision.ALLOW))
