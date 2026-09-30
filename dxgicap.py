"""DXGI Desktop Duplication through ctypes: the desktop image, shrunk on the
GPU to any size, plus the areas Windows reports as changed.

Used by the tiled JPEG stream (tiles.py), which needs those change hints;
ffmpeg's ddagrab does the same capture but doesn't pass them on. Vtable
indices are from the Windows SDK headers (dxgi1_2.h, d3d11.h)."""
import ctypes
import math
import os
from ctypes import wintypes

from win32 import _GUID, LPVOID

_IID_OUTPUT1 = _GUID.parse("00cddea8-939b-4b83-a340-a685226666cc")    # IDXGIOutput1
_IID_FACTORY1 = _GUID.parse("770aae78-f26f-4dba-a829-253c83d1b387")   # IDXGIFactory1
_IID_TEXTURE2D = _GUID.parse("6f15aaf2-d208-4e89-9ab4-489535d34f9c")  # ID3D11Texture2D

WAIT_TIMEOUT = 0x887A0027
ACCESS_LOST = 0x887A0026
BGRA = 87  # DXGI_FORMAT_B8G8R8A8_UNORM, what duplication hands out

_UINT = wintypes.UINT
_PP = ctypes.POINTER(LPVOID)


def _call(ptr, index, *args, argtypes=(), restype=ctypes.c_long):
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(LPVOID)))[0]
    return ctypes.WINFUNCTYPE(restype, LPVOID, *argtypes)(vtbl[index])(ptr, *args)


def _release(p):
    if p:
        _call(p, 2, restype=ctypes.c_ulong)


class _POINTER_POSITION(ctypes.Structure):
    _fields_ = [("Position", wintypes.POINT), ("Visible", wintypes.BOOL)]


class _FRAME_INFO(ctypes.Structure):
    _fields_ = [("LastPresentTime", ctypes.c_longlong), ("LastMouseUpdateTime", ctypes.c_longlong),
                ("AccumulatedFrames", _UINT), ("RectsCoalesced", wintypes.BOOL),
                ("ProtectedContentMaskedOut", wintypes.BOOL), ("PointerPosition", _POINTER_POSITION),
                ("TotalMetadataBufferSize", _UINT), ("PointerShapeBufferSize", _UINT)]


class _MOVE_RECT(ctypes.Structure):
    _fields_ = [("SourcePoint", wintypes.POINT), ("DestinationRect", wintypes.RECT)]


class _DUPL_DESC(ctypes.Structure):
    _fields_ = [("Width", _UINT), ("Height", _UINT), ("RefreshNum", _UINT), ("RefreshDen", _UINT),
                ("Format", _UINT), ("ScanlineOrdering", _UINT), ("Scaling", _UINT),
                ("Rotation", _UINT), ("DesktopImageInSystemMemory", wintypes.BOOL)]


class _TEX_DESC(ctypes.Structure):
    _fields_ = [(n, _UINT) for n in ("Width", "Height", "MipLevels", "ArraySize", "Format", "SampleCount",
                                     "SampleQuality", "Usage", "BindFlags", "CPUAccessFlags", "MiscFlags")]


class _MAPPED(ctypes.Structure):
    _fields_ = [("pData", LPVOID), ("RowPitch", _UINT), ("DepthPitch", _UINT)]


class _BOX(ctypes.Structure):
    _fields_ = [(n, _UINT) for n in ("left", "top", "front", "right", "bottom", "back")]


class _VIEWPORT(ctypes.Structure):
    _fields_ = [(n, ctypes.c_float) for n in ("TopLeftX", "TopLeftY", "Width", "Height", "MinDepth", "MaxDepth")]


class CaptureError(OSError):
    lost = False


def _check(hr, what):
    if hr != 0:
        raise CaptureError(f"{what} failed (0x{hr & 0xFFFFFFFF:08X})")


# A full-screen triangle, and an exact area-average shrink: each output
# pixel is the average of the source pixels under it, each weighted by how
# much of it the output pixel covers (what ffmpeg's "area" scaler computes,
# on the GPU).
_VS = b"""
float4 main(uint id : SV_VertexID) : SV_Position {
    float2 uv = float2((id << 1) & 2, id & 2);
    return float4(uv * float2(2, -2) + float2(-1, 1), 0, 1);
}"""
_PS = """
Texture2D src : register(t0);
float4 main(float4 pos : SV_Position) : SV_Target {{
    const float2 R = float2({rx:.9f}, {ry:.9f});
    float2 a = (pos.xy - 0.5) * R, b = a + R;
    int2 i0 = int2(floor(a));
    float4 acc = 0;
    [unroll] for (int j = 0; j < {ny}; j++) {{
        float wy = max(0, min(b.y, i0.y + j + 1) - max(a.y, i0.y + j));
        [unroll] for (int i = 0; i < {nx}; i++) {{
            float wx = max(0, min(b.x, i0.x + i + 1) - max(a.x, i0.x + i));
            acc += wx * wy * src.Load(int3(min(i0 + int2(i, j), int2({mw}, {mh})), 0));
        }}
    }}
    return acc / (R.x * R.y);
}}"""

_d3d11 = _compiler = None


def _compile(src: bytes, target: bytes):
    global _compiler
    if _compiler is None:
        # by full path: it isn't a KnownDLL, and a packaged app looks for bare names in its own folder
        _compiler = ctypes.WinDLL(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "d3dcompiler_47.dll"))
        _compiler.D3DCompile.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, LPVOID, LPVOID,
                                         ctypes.c_char_p, ctypes.c_char_p, _UINT, _UINT, _PP, _PP]
    code, err = LPVOID(), LPVOID()
    hr = _compiler.D3DCompile(src, len(src), b"shrink", None, None, b"main", target, 1 << 15, 0,
                              ctypes.byref(code), ctypes.byref(err))
    if hr != 0:
        msg = ctypes.string_at(_call(err, 3, restype=LPVOID), _call(err, 4, restype=ctypes.c_size_t)) if err else b""
        _release(err)
        raise CaptureError(f"shader compile failed: {msg.decode(errors='replace')[:200]}")
    return code


class Target:
    """One output size: a GPU render target when it's a shrink, and a CPU-readable copy."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.rt = self.rtv = self.ps = self.staging = None


class Duplication:
    """One IDXGIOutputDuplication. Per frame: acquire() -> change hints
    (native pixels), render(t) + copy(t, regions) for each output size,
    release(), then map(t) / unmap(t) to read each one."""

    def __init__(self, adapter: int, output: int):
        global _d3d11
        self.dev = self.ctx = self.dup = self._frame = self._vs = None
        self._targets: dict = {}
        self.width = self.height = 0
        self.rotation = 1
        self._meta = ctypes.create_string_buffer(1 << 16)
        factory, adapter_p, output_p, out1 = LPVOID(), LPVOID(), LPVOID(), LPVOID()
        try:
            _check(ctypes.WinDLL("dxgi").CreateDXGIFactory1(ctypes.byref(_IID_FACTORY1), ctypes.byref(factory)),
                   "CreateDXGIFactory1")
            _check(_call(factory, 12, adapter, ctypes.byref(adapter_p), argtypes=(_UINT, _PP)), "EnumAdapters1")
            if _d3d11 is None:
                _d3d11 = ctypes.WinDLL("d3d11")
                _d3d11.D3D11CreateDevice.argtypes = [LPVOID, _UINT, LPVOID, _UINT, LPVOID, _UINT, _UINT,
                                                     _PP, ctypes.POINTER(_UINT), _PP]
            dev, ctx, level = LPVOID(), LPVOID(), _UINT()
            # D3D_DRIVER_TYPE_UNKNOWN (an explicit adapter), SDK version 7
            _check(_d3d11.D3D11CreateDevice(adapter_p, 0, None, 0, None, 0, 7, ctypes.byref(dev),
                                            ctypes.byref(level), ctypes.byref(ctx)), "D3D11CreateDevice")
            self.dev, self.ctx = dev, ctx
            _check(_call(adapter_p, 7, output, ctypes.byref(output_p), argtypes=(_UINT, _PP)), "EnumOutputs")
            _check(_call(output_p, 0, ctypes.byref(_IID_OUTPUT1), ctypes.byref(out1),
                         argtypes=(ctypes.POINTER(_GUID), _PP)), "QueryInterface(IDXGIOutput1)")
            dup = LPVOID()
            _check(_call(out1, 22, dev, ctypes.byref(dup), argtypes=(LPVOID, _PP)), "DuplicateOutput")
            self.dup = dup
            d = _DUPL_DESC()
            _call(self.dup, 7, ctypes.byref(d), argtypes=(ctypes.POINTER(_DUPL_DESC),))
            self.width, self.height, self.rotation = d.Width, d.Height, d.Rotation
        except BaseException:
            self.close()
            raise
        finally:
            for p in (out1, output_p, adapter_p, factory):
                _release(p)

    # ---- per frame -------------------------------------------------------
    def acquire(self, timeout_ms: int):
        """Waits for a new frame: None = nothing new; [] = only the mouse
        moved (no image: release() it); else the changed areas, or ALL when
        Windows gave no list. Raises CaptureError (.lost: recreate)."""
        info, res = _FRAME_INFO(), LPVOID()
        hr = _call(self.dup, 8, timeout_ms, ctypes.byref(info), ctypes.byref(res),
                   argtypes=(_UINT, ctypes.POINTER(_FRAME_INFO), _PP)) & 0xFFFFFFFF
        if hr == WAIT_TIMEOUT:
            return None
        if hr != 0:
            e = CaptureError(f"AcquireNextFrame failed (0x{hr:08X})")
            e.lost = hr == ACCESS_LOST
            raise e
        tex = LPVOID()
        try:
            _check(_call(res, 0, ctypes.byref(_IID_TEXTURE2D), ctypes.byref(tex),
                         argtypes=(ctypes.POINTER(_GUID), _PP)), "QueryInterface(Texture2D)")
        finally:
            _release(res)
        self._frame = tex
        if info.LastPresentTime == 0:
            return []  # we draw the cursor ourselves
        return self._hints(info)

    ALL = "all"

    def _hints(self, info):
        size = info.TotalMetadataBufferSize
        if not size:
            return self.ALL
        if size > len(self._meta):
            self._meta = ctypes.create_string_buffer(size)
        out, need = [], _UINT()
        # move rects: their sources and destinations both changed
        if _call(self.dup, 10, len(self._meta), self._meta, ctypes.byref(need),
                 argtypes=(_UINT, LPVOID, ctypes.POINTER(_UINT))) != 0:
            return self.ALL
        for m in (_MOVE_RECT * (need.value // ctypes.sizeof(_MOVE_RECT))).from_buffer(self._meta):
            r = m.DestinationRect
            out.append((r.left, r.top, r.right, r.bottom))
            sx, sy = m.SourcePoint.x, m.SourcePoint.y
            out.append((sx, sy, sx + r.right - r.left, sy + r.bottom - r.top))
        if _call(self.dup, 9, len(self._meta), self._meta, ctypes.byref(need),
                 argtypes=(_UINT, LPVOID, ctypes.POINTER(_UINT))) != 0:
            return self.ALL
        out += [(r.left, r.top, r.right, r.bottom)
                for r in (wintypes.RECT * (need.value // ctypes.sizeof(wintypes.RECT))).from_buffer(self._meta)]
        return out

    def target(self, w, h) -> Target:
        t = self._targets.get((w, h))
        if t is None:
            t = self._targets[(w, h)] = Target(w, h)
            try:
                t.staging = self._texture(w, h, usage=3, bind=0, cpu=0x20000)  # STAGING, CPU read
                if (w, h) != (self.width, self.height):
                    self._make_shrink(t)
            except BaseException:
                self._free(t)
                del self._targets[(w, h)]
                raise
        return t

    def render(self, t: Target):
        """Draws the acquired frame, shrunk, into t's render target (a no-op at native size)."""
        if not t.rt:
            return
        srv = LPVOID()
        _check(_call(self.dev, 7, self._frame, None, ctypes.byref(srv), argtypes=(LPVOID, LPVOID, _PP)),
               "CreateShaderResourceView")
        try:
            c, vp = self.ctx, _VIEWPORT(0, 0, t.w, t.h, 0, 1)
            _call(c, 17, None, argtypes=(LPVOID,))                                   # IASetInputLayout
            _call(c, 24, 4, argtypes=(_UINT,))                                       # TRIANGLELIST
            _call(c, 11, self._vs, None, 0, argtypes=(LPVOID, LPVOID, _UINT))        # VSSetShader
            _call(c, 9, t.ps, None, 0, argtypes=(LPVOID, LPVOID, _UINT))             # PSSetShader
            _call(c, 8, 0, 1, ctypes.byref(srv), argtypes=(_UINT, _UINT, _PP))       # PSSetShaderResources
            _call(c, 33, 1, ctypes.byref(t.rtv), None, argtypes=(_UINT, _PP, LPVOID))  # OMSetRenderTargets
            _call(c, 44, 1, ctypes.byref(vp), argtypes=(_UINT, ctypes.POINTER(_VIEWPORT)))
            _call(c, 13, 3, 0, argtypes=(_UINT, _UINT))                              # Draw
            _call(c, 8, 0, 1, ctypes.byref(LPVOID()), argtypes=(_UINT, _UINT, _PP))  # unbind the frame
        finally:
            _release(srv)

    def copy(self, t: Target, regions=None):
        """GPU-copies t's image (or just `regions`, in t's pixels) to its CPU copy."""
        src = t.rt or self._frame
        if regions is None:
            _call(self.ctx, 47, t.staging, src, argtypes=(LPVOID, LPVOID))  # CopyResource
            return
        box = _BOX(0, 0, 0, 0, 0, 1)
        for x0, y0, x1, y1 in regions:
            box.left, box.top, box.right, box.bottom = x0, y0, x1, y1
            _call(self.ctx, 46, t.staging, 0, x0, y0, 0, src, 0, ctypes.byref(box),  # CopySubresourceRegion
                  argtypes=(LPVOID, _UINT, _UINT, _UINT, _UINT, LPVOID, _UINT, ctypes.POINTER(_BOX)))

    def release(self):
        if self._frame:
            _release(self._frame)
            self._frame = None
            _call(self.dup, 14)  # ReleaseFrame

    def map(self, t: Target):
        """-> (address, row pitch) of t's CPU copy; unmap(t) after."""
        m = _MAPPED()
        _check(_call(self.ctx, 14, t.staging, 0, 1, 0, ctypes.byref(m),  # D3D11_MAP_READ
                     argtypes=(LPVOID, _UINT, _UINT, _UINT, ctypes.POINTER(_MAPPED))), "Map")
        return m.pData, m.RowPitch

    def unmap(self, t: Target):
        _call(self.ctx, 15, t.staging, 0, argtypes=(LPVOID, _UINT))

    # ---- resources -------------------------------------------------------
    def _texture(self, w, h, usage, bind, cpu):
        d = _TEX_DESC(w, h, 1, 1, BGRA, 1, 0, usage, bind, cpu, 0)
        tex = LPVOID()
        _check(_call(self.dev, 5, ctypes.byref(d), None, ctypes.byref(tex),
                     argtypes=(ctypes.POINTER(_TEX_DESC), LPVOID, _PP)), "CreateTexture2D")
        return tex

    def _shader(self, code, index):
        sh = LPVOID()
        try:
            _check(_call(self.dev, index, _call(code, 3, restype=LPVOID), _call(code, 4, restype=ctypes.c_size_t),
                         None, ctypes.byref(sh), argtypes=(LPVOID, ctypes.c_size_t, LPVOID, _PP)), "CreateShader")
        finally:
            _release(code)
        return sh

    def _make_shrink(self, t: Target):
        if self._vs is None:
            self._vs = self._shader(_compile(_VS, b"vs_4_0"), 12)
        rx, ry = self.width / t.w, self.height / t.h
        # source pixels an output pixel can touch per axis
        nx, ny = math.floor(rx - 1e-9) + 2, math.floor(ry - 1e-9) + 2
        src = _PS.format(rx=rx, ry=ry, nx=nx, ny=ny, mw=self.width - 1, mh=self.height - 1)
        t.ps = self._shader(_compile(src.encode(), b"ps_4_0"), 15)
        t.rt = self._texture(t.w, t.h, usage=0, bind=0x20, cpu=0)  # DEFAULT, render target
        rtv = LPVOID()
        _check(_call(self.dev, 9, t.rt, None, ctypes.byref(rtv), argtypes=(LPVOID, LPVOID, _PP)),
               "CreateRenderTargetView")
        t.rtv = rtv

    @staticmethod
    def _free(t: Target):
        for name in ("rtv", "rt", "ps", "staging"):
            _release(getattr(t, name))
            setattr(t, name, None)

    def drop(self, w, h):
        t = self._targets.pop((w, h), None)
        if t:
            self._free(t)

    def close(self):
        try:
            self.release()
        except OSError:
            pass
        for t in self._targets.values():
            self._free(t)
        self._targets.clear()
        for name in ("_vs", "dup", "ctx", "dev"):
            _release(getattr(self, name, None))
            setattr(self, name, None)
