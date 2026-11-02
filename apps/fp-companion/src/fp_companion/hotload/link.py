"""Transport adapters for :mod:`fp_companion.hotload.core`.

Two backends are provided:

``camera``
    :class:`CameraLink` wraps a live
    :class:`fp_companion.autotest.session.CameraSession`.  It adapts the
    session's ``sh``/``mem_get`` interface to the ``shl`` shape ``Hotload``
    wants, and never opens a second USB session when it is handed an existing
    one.

``direct``
    :class:`DirectLink` opens the package's own
    ``fp_companion.autotest.session.CameraSession`` and always owns it.  It has
    no dependency on the firmware checkout or ``fpsh_client.py``, so a wheel
    install works without either.

``auto`` tries the camera session first and falls back to the direct opening
path.  Both paths use the same package transport; the two names are kept for
API compatibility and for callers that want to distinguish an injected session
from one this library opened.
"""

from __future__ import annotations

from .core import TransportError, UsageError

BACKENDS = ("auto", "camera", "direct")


def _open_camera_session(*, timeout_ms: int | None = None,
                         retries: int | None = None):
    """Import and open the package's CameraSession (lazy; no pyusb at import)."""
    try:
        from fp_companion.autotest.session import CameraSession
    except Exception as exc:  # noqa: BLE001
        raise TransportError(f"cannot import CameraSession: {exc}") from exc

    kwargs: dict[str, object] = {}
    if timeout_ms is not None:
        if timeout_ms <= 0:
            raise UsageError("--timeout must be positive")
        kwargs["timeout_ms"] = timeout_ms
    if retries is not None:
        if retries < 1:
            raise UsageError("--retries must be >= 1")
        kwargs["retries"] = retries
    try:
        return CameraSession(**kwargs)
    except Exception as exc:  # noqa: BLE001
        raise TransportError(f"cannot open camera session: {exc}") from exc


def _decode(reply: object) -> str:
    if isinstance(reply, (bytes, bytearray)):
        return bytes(reply).decode("latin-1")
    return str(reply)


class _Link:
    """Shared shell helpers for the two concrete links."""

    retries: int = 1

    def shl(self, line: str) -> bytes:  # pragma: no cover - interface marker
        raise NotImplementedError

    def sh(self, line: str) -> str:
        return _decode(self.shl(line))

    def mem_get(self, addr: int, size: int = 4) -> int:
        """Read one word through the shell; the core has its own parser too."""
        from .core import Hotload

        return Hotload(self).mem_get(addr)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class CameraLink(_Link):
    """Adapt a :class:`CameraSession` (or a compatible fake) to ``Hotload``.

    ``CameraLink()`` opens a live session.  ``CameraLink(session=...)`` wraps
    an already-open session without taking ownership of it; closing the link
    then leaves the session open for the caller.
    """

    def __init__(self, session: object | None = None, *,
                 timeout_ms: int | None = None,
                 retries: int | None = None):
        if session is None:
            session = _open_camera_session(
                timeout_ms=timeout_ms, retries=retries)
            self._owns_session = True
        else:
            self._owns_session = False
        self.session = session

    @property
    def retries(self) -> int:
        return int(getattr(self.session, "retries", 1) or 1)

    @retries.setter
    def retries(self, value: int) -> None:
        # Hotload.call sets this to 1 so the `echo` line is sent exactly once.
        # The value must reach the session's own exchange loop, not just this
        # adapter: a retried echo would run the loader entry twice.
        try:
            self.session.retries = int(value)
        except Exception:  # noqa: BLE001 - a read-only fake is still usable
            pass

    def shl(self, line: str) -> bytes:
        reply = self.session.sh(line)
        return reply.encode("latin-1") if isinstance(reply, str) else bytes(reply)

    def mem_get(self, addr: int, size: int = 4) -> int:
        return self.session.mem_get(addr, size)

    def close(self) -> None:
        if self._owns_session:
            close = getattr(self.session, "close", None)
            if callable(close):
                close()
        self._owns_session = False


class DirectLink(CameraLink):
    """Standalone link: opens the package's own ``CameraSession``.

    Unlike ``CameraLink(session=...)``, this always owns the session.  It uses
    no firmware checkout and never imports ``fpsh_client``, so it works in a
    plain ``pip install fp-companion``.
    """

    def __init__(self, *, timeout_ms: int | None = None,
                 retries: int | None = None):
        super().__init__(session=None, timeout_ms=timeout_ms, retries=retries)
        # Kept for callers written against the earlier fpsh_client-backed
        # DirectLink, which exposed the camera as ``.camera``.
        self.camera = self.session

    def shl(self, line: str) -> bytes:
        return bytes(self.camera.shl(line))

    def close(self) -> None:
        close = getattr(self.camera, "close", None)
        if callable(close):
            close()


def open_link(backend: str = "auto", *, timeout_ms: int | None = None,
              retries: int | None = None) -> _Link:
    """Open a transport by name.  ``auto`` is camera-first, direct-fallback."""
    if backend not in BACKENDS:
        raise UsageError(
            f"unknown backend {backend!r} (choose from {', '.join(BACKENDS)})")

    if backend == "camera":
        return CameraLink(timeout_ms=timeout_ms, retries=retries)
    if backend == "direct":
        return DirectLink(timeout_ms=timeout_ms, retries=retries)

    errors: list[str] = []
    try:
        return CameraLink(timeout_ms=timeout_ms, retries=retries)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"camera: {exc}")
    try:
        return DirectLink(timeout_ms=timeout_ms, retries=retries)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"direct: {exc}")
    raise TransportError("no camera transport available (" + "; ".join(errors) + ")")


__all__ = [
    "BACKENDS",
    "CameraLink",
    "DirectLink",
    "open_link",
]
