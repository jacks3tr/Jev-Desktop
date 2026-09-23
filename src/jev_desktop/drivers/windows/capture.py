"""Screen capture with recorded geometry, scale, and coordinate mapping.

Screenshots are evidence, not assertions. Every capture records the geometry epoch and
the exact source rectangle and scale so a caller (or a vision oracle) can map image
coordinates back to desktop coordinates, and so stale geometry can be detected rather
than silently reused.
"""

from __future__ import annotations

import ctypes
import hashlib
import struct
import zlib
from dataclasses import dataclass

from ...contracts import Capture, DriverError, EvidenceRef, Geometry, Rect, new_id, now
from . import win32


def _dpi_for_system() -> int:
    try:
        return int(win32.user32.GetDpiForSystem())
    except Exception:  # pragma: no cover - pre-1607
        return 96


def encode_png(width: int, height: int, bgra: bytes) -> bytes:
    """Minimal PNG writer (colour type 6, filter 0) for a BGRA top-down buffer."""
    expected = width * height * 4
    if len(bgra) != expected:
        raise DriverError(f"pixel buffer is {len(bgra)} bytes, expected {expected}")
    rows = bytearray()
    stride = width * 4
    for y in range(height):
        row = bytearray(bgra[y * stride : (y + 1) * stride])
        row[0::4], row[2::4] = row[2::4], row[0::4]  # BGRA -> RGBA
        row[3::4] = b"\xff" * width  # GDI's unused alpha byte is not image transparency.
        rows += b"\x00" + row

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + chunk(b"IEND", b"")
    )


TILE = 32


def tile_checksums(
    width: int, height: int, bgra: bytes, *, origin: tuple[int, int] = (0, 0)
) -> dict[tuple[int, int], int]:
    """CRC32 of each TILE x TILE block of a BGRA buffer, keyed by (column, row) offset by `origin`."""
    pixels = bytearray(bgra)
    pixels[3::4] = bytes(width * height)  # GDI's unused alpha byte is not image content.
    view = memoryview(pixels)
    stride = width * 4
    sums = {}
    for top in range(0, height, TILE):
        for left in range(0, width, TILE):
            crc = 0
            for y in range(top, min(top + TILE, height)):
                crc = zlib.crc32(view[y * stride + left * 4 : y * stride + min(left + TILE, width) * 4], crc)
            sums[(origin[0] + left // TILE, origin[1] + top // TILE)] = crc
    return sums


@dataclass
class RawCapture:
    png: bytes
    source_rect: Rect
    scale: float
    geometry: Geometry
    bgra: bytes  # full-resolution pixels of source_rect, whatever scale the PNG uses


class ScreenCapture:
    """GDI screen capture for the virtual desktop, DPI-aware and epoch-tracked."""

    def __init__(self) -> None:
        self._epoch = 0
        self._signature: tuple[int, int, int, int, int] | None = None

    def geometry(self) -> Geometry:
        screen = win32.virtual_screen()
        dpi = _dpi_for_system()
        signature = (screen.left, screen.top, screen.width, screen.height, dpi)
        if signature != self._signature:
            self._signature = signature
            self._epoch += 1
        return Geometry(
            epoch=self._epoch,
            virtual_left=screen.left,
            virtual_top=screen.top,
            virtual_width=screen.width,
            virtual_height=screen.height,
            primary_dpi=dpi,
            primary_scale=dpi / 96.0,
        )

    def grab(self, region: Rect | None = None, *, max_dimension: int = 1600) -> RawCapture:
        """Capture a screen region, downscaling when it exceeds `max_dimension`."""
        screen = win32.virtual_screen()
        source = screen if region is None else region.intersect(screen)
        if source.is_empty:
            raise DriverError("capture region is off-screen")
        scale = min(1.0, max_dimension / max(source.width, source.height))
        buffer = self.grab_bgra(source)
        full = encode_png(source.width, source.height, buffer)
        if scale >= 1.0:
            return RawCapture(png=full, source_rect=source, scale=1.0, geometry=self.geometry(), bgra=buffer)

        # Downscaling through GDI applies dithering, which sometimes compresses worse than the
        # original. Measured on a 720x520 window: 9.2 kB full, 23.9 kB at 0.4. Keep whichever
        # encoding is actually smaller, and report the scale that produced it.
        target_width = max(1, int(source.width * scale))
        target_height = max(1, int(source.height * scale))
        scaled = _downscale(source.width, source.height, buffer, target_width, target_height)
        if scaled and len(scaled) < len(full):
            return RawCapture(png=scaled, source_rect=source, scale=scale, geometry=self.geometry(), bgra=buffer)
        return RawCapture(png=full, source_rect=source, scale=1.0, geometry=self.geometry(), bgra=buffer)

    def grab_bgra(self, source: Rect) -> bytes:
        """Full-resolution top-down BGRA pixels of an on-screen rectangle."""
        screen_dc = win32.user32.GetDC(0)
        if not screen_dc:
            raise DriverError(f"GetDC(desktop) failed ({ctypes.get_last_error()})")
        memory_dc = win32.gdi32.CreateCompatibleDC(screen_dc)
        bitmap = 0
        old = 0
        try:
            info = win32.BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(win32.BITMAPINFOHEADER)
            info.bmiHeader.biWidth = source.width
            info.bmiHeader.biHeight = -source.height  # top-down
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = 0  # BI_RGB
            bits = ctypes.c_void_p()
            bitmap = win32.gdi32.CreateDIBSection(memory_dc, ctypes.byref(info), 0, ctypes.byref(bits), 0, 0)
            if not bitmap:
                raise DriverError("CreateDIBSection failed")
            old = win32.gdi32.SelectObject(memory_dc, bitmap)
            if not win32.gdi32.BitBlt(
                memory_dc,
                0,
                0,
                source.width,
                source.height,
                screen_dc,
                source.left,
                source.top,
                win32.SRCCOPY | win32.CAPTUREBLT,
            ):
                raise DriverError("BitBlt failed")
            buffer = ctypes.string_at(bits, source.width * source.height * 4)
        finally:
            if old:
                win32.gdi32.SelectObject(memory_dc, old)
            if bitmap:
                win32.gdi32.DeleteObject(bitmap)
            win32.gdi32.DeleteDC(memory_dc)
            win32.user32.ReleaseDC(0, screen_dc)
        return buffer

    def capture_to(
        self,
        path: str,
        *,
        run_id: str,
        checkpoint: str | None,
        description: str,
        region: Rect | None = None,
        snapshot_id: str | None = None,
        max_dimension: int = 1600,
    ) -> tuple[Capture, bytes]:
        """Write the PNG evidence; also return the full-resolution BGRA pixels it was made from."""
        raw = self.grab(region, max_dimension=max_dimension)
        with open(path, "wb") as handle:
            handle.write(raw.png)
        evidence = EvidenceRef(
            evidence_id=new_id("ev"),
            run_id=run_id,
            kind="screenshot",
            path=path,
            media_type="image/png",
            sha256=hashlib.sha256(raw.png).hexdigest(),
            size_bytes=len(raw.png),
            created_at=now(),
            checkpoint=checkpoint,
            snapshot_id=snapshot_id,
            geometry=raw.geometry,
            source_rect=raw.source_rect,
            scale=raw.scale,
            description=description,
            image_width=struct.unpack(">I", raw.png[16:20])[0],
            image_height=struct.unpack(">I", raw.png[20:24])[0],
        )
        capture = Capture(evidence=evidence, geometry=raw.geometry, source_rect=raw.source_rect, scale=raw.scale)
        return capture, raw.bgra


def _downscale(width: int, height: int, bgra: bytes, target_width: int, target_height: int) -> bytes | None:
    """HALFTONE StretchBlt downscale; returns PNG bytes or None when unsupported."""
    dc = win32.gdi32.CreateCompatibleDC(0)
    if not dc:
        return None
    bitmap = 0
    old = 0
    try:
        info = win32.BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(win32.BITMAPINFOHEADER)
        info.bmiHeader.biWidth = target_width
        info.bmiHeader.biHeight = -target_height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        target_bits = ctypes.c_void_p()
        bitmap = win32.gdi32.CreateDIBSection(dc, ctypes.byref(info), 0, ctypes.byref(target_bits), 0, 0)
        if not bitmap:
            return None
        old = win32.gdi32.SelectObject(dc, bitmap)
        source_dc = win32.gdi32.CreateCompatibleDC(0)
        source_bitmap = 0
        source_old = 0
        try:
            source_info = win32.BITMAPINFO()
            source_info.bmiHeader.biSize = ctypes.sizeof(win32.BITMAPINFOHEADER)
            source_info.bmiHeader.biWidth = width
            source_info.bmiHeader.biHeight = -height
            source_info.bmiHeader.biPlanes = 1
            source_info.bmiHeader.biBitCount = 32
            source_info.bmiHeader.biCompression = 0
            source_bits = ctypes.c_void_p()
            source_bitmap = win32.gdi32.CreateDIBSection(
                source_dc, ctypes.byref(source_info), 0, ctypes.byref(source_bits), 0, 0
            )
            if not source_bitmap:
                return None
            source_old = win32.gdi32.SelectObject(source_dc, source_bitmap)
            ctypes.memmove(source_bits, bgra, len(bgra))
            win32.gdi32.SetStretchBltMode(dc, win32.HALFTONE)
            if not win32.gdi32.StretchBlt(
                dc, 0, 0, target_width, target_height, source_dc, 0, 0, width, height, win32.SRCCOPY
            ):
                return None
            scaled = ctypes.string_at(target_bits, target_width * target_height * 4)
            return encode_png(target_width, target_height, scaled)
        finally:
            if source_old:
                win32.gdi32.SelectObject(source_dc, source_old)
            if source_bitmap:
                win32.gdi32.DeleteObject(source_bitmap)
            win32.gdi32.DeleteDC(source_dc)
    finally:
        if old:
            win32.gdi32.SelectObject(dc, old)
        if bitmap:
            win32.gdi32.DeleteObject(bitmap)
        win32.gdi32.DeleteDC(dc)
