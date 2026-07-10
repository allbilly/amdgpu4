#!/usr/bin/env python3
"""Standalone Terascale eGPU vector-add scaffold (HD 5570 / HD 4850) over TinyGPU.

Hardware not required yet — `--selftest` / `--dry-run` work offline. When the
card is attached via TinyGPU, `--probe` enumerates PCI and MMIO.

Targets (linux `drivers/gpu/drm/radeon`):
  HD 5570 — Redwood / Evergreen (TeraScale 2), PCI 1002:68D9 (also 68D8/68DA…)
  HD 4850 — RV770 / R700     (TeraScale 1), PCI 1002:9442

Evergreen has a real compute path (Mesa `evergreen_compute.c` / r600g OpenCL):
  SQ_PGM_START_LS + SPI_COMPUTE_NUM_THREAD_* + PKT3_DISPATCH_DIRECT (compute bit).
RV770 shares the R600 CP ring (`r600_cp_resume`) but **no LS compute**; GFX/ALU
path is stubbed until HW bring-up.

Refs: `ref/linux/.../radeon/{evergreen,r600,rv770}.c`, `evergreend.h`, `r600d.h`.

Usage:
  python3 examples_egpu_terrascale/add.py --selftest
  python3 examples_egpu_terrascale/add.py --chip=hd5570 --dry-run
  python3 examples_egpu_terrascale/add.py --probe          # needs TinyGPU + card
  python3 examples_egpu_terrascale/add.py --dump-rom       # dump onboard VBIOS
  python3 examples_egpu_terrascale/add.py --atom           # ATOM asic_init only
  python3 examples_egpu_terrascale/add.py --ring-test      # CP bring-up
"""
from __future__ import annotations
import os, sys, ctypes, ctypes.util, time, mmap, struct, array, socket, subprocess
import contextlib, functools, enum, urllib.request, hashlib
import tempfile, pathlib, math, json
from dataclasses import dataclass, field

# =============================================================================
# TinyGPU transport + PM4 helpers (from examples_egpu/add.py)
# =============================================================================
DEBUG = int(os.environ.get("DEBUG", "0"))
OSX = sys.platform == "darwin"

def getenv(k: str, default=0):
  v = os.environ.get(k)
  if v is None: return default
  try: return int(v)
  except: return v

def round_up(n: int, a: int) -> int: return ((n + a - 1) // a) * a
def ceildiv(n: int, a: int) -> int: return -(n // -a)
def lo32(x: int) -> int: return x & 0xFFFFFFFF
def hi32(x: int) -> int: return x >> 32
def data64_le(x: int) -> tuple: return (x & 0xFFFFFFFF, (x >> 32) & 0xFFFFFFFF)
def temp(name: str) -> str: return os.path.join(tempfile.gettempdir(), name)
def unwrap(x): return x

def wait_cond(cb, *args, value=True, timeout_ms=10000, msg=""):
  start = int(time.perf_counter() * 1000)
  while int(time.perf_counter() * 1000) - start < timeout_ms:
    if (val := cb(*args)) == value: return val
  raise TimeoutError(f"{msg}. Timed out after {timeout_ms} ms, last={val!r} expected={value!r}")

def _ensure_downloads_dir() -> pathlib.Path:
  d = pathlib.Path(os.path.expanduser("~")) / ".cache" / "tinygrad"
  d.mkdir(parents=True, exist_ok=True)
  return d

def fetch_fw(path: str, name: str, sha256: str | None = None) -> bytes:
  cache_dir = _ensure_downloads_dir() / "fw"
  cache_dir.mkdir(parents=True, exist_ok=True)
  fp = cache_dir / name
  if fp.is_file() and (sha256 is None or hashlib.sha256(fp.read_bytes()).hexdigest() == sha256):
    return fp.read_bytes()
  url = f"https://gitlab.com/kernel-firmware/linux-firmware/-/raw/main/{path}/{name}"
  with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "amdgpu-egpu"}), timeout=30) as r:
    data = r.read()
  if sha256 and hashlib.sha256(data).hexdigest() != sha256:
    raise RuntimeError(f"fetch_fw sha mismatch for {name}")
  fp.write_bytes(data)
  return data

# ============================================================================
# Remote PCI transport (vendored from nvgpu examples/add.py / tinygrad system.py)
# ============================================================================
class MMIOInterface:
  def __init__(self, addr, nbytes, fmt='B'):
    self.mv = (ctypes.c_uint8 * nbytes).from_address(addr)
    self.addr, self.nbytes, self.fmt = addr, nbytes, fmt
    if fmt != 'B':
      self._arr = array.array(fmt)
      self._arr.frombytes(bytes(self.mv))
  def __len__(self): return self.nbytes // (1 if self.fmt == 'B' else struct.calcsize(self.fmt))
  def __getitem__(self, k):
    if self.fmt == 'B':
      sl = k if isinstance(k, slice) else slice(k, k+1)
      return bytes(self.mv[sl.start:sl.stop])
    return self._arr[k]
  def __setitem__(self, k, v):
    if self.fmt == 'B':
      if isinstance(k, slice):
        self.mv[k.start:k.stop] = v if isinstance(v, (bytes, bytearray)) else bytes(v)
      else:
        self.mv[k] = v if isinstance(v, int) else v[0]
    else:
      self._arr[k] = v
      ctypes.memmove(self.addr, self._arr.tobytes(), self.nbytes)
  def view(self, offset=0, size=None, fmt=None):
    sz = (self.nbytes - offset) if size is None else size
    return MMIOInterface(self.addr + offset, sz, fmt=fmt or self.fmt)

def sysmem_dma_flush(mem, size: int):
  """Flush CPU writes so eGPU DMA sees host memory (ARM lacks IO coherency).

  See geerlingguy/raspberry-pi-pcie-devices#756 — Pi5/M1 need explicit sync for
  GPU DMA to see CPU-written sysmem (yanghaku pgprot_dmacoherent TTM patch).
  """
  if os.environ.get("AMD_BOOT_SYSMEM_FLUSH", "1") == "0":
    return
  if not hasattr(mem, "addr") or not size:
    return
  libc = ctypes.CDLL(ctypes.util.find_library("c"))
  MS_SYNC = 0x10
  if libc.msync(ctypes.c_void_p(mem.addr), size, MS_SYNC) != 0:
    with contextlib.suppress(Exception):
      libc.sync()

class FileIOInterface:
  def __init__(self, fd=None):
    self.fd = fd
  def mmap(self, start, sz, prot, flags, offset):
    libc = ctypes.CDLL(ctypes.util.find_library("c"))
    libc.mmap.restype = ctypes.c_void_p
    addr = libc.mmap(start or None, sz, prot, flags, self.fd, offset)
    if not addr or addr == ctypes.c_void_p(-1).value:
      raise OSError("mmap failed")
    return addr

class RemoteCmd(enum.IntEnum):
  PROBE, MAP_BAR, MAP_SYSMEM_FD, CFG_READ, CFG_WRITE, RESET, MMIO_READ, MMIO_WRITE, MAP_SYSMEM, SYSMEM_READ, SYSMEM_WRITE, RESIZE_BAR, PING = range(13)

class RemoteMMIOInterface:
  def __init__(self, dev, residx, nbytes, fmt='B', off=0):
    self.dev, self.residx, self.nbytes, self.fmt, self.off = dev, residx, nbytes, fmt, off
    self.el_sz = struct.calcsize(fmt)
  def __len__(self): return self.nbytes // self.el_sz
  def __getitem__(self, index):
    sl = index if isinstance(index, slice) else slice(index, index + 1)
    start, stop = (sl.start or 0) * self.el_sz, (sl.stop or len(self)) * self.el_sz
    data = self.dev._bulk_read(RemoteCmd.MMIO_READ, self.residx, self.off + start, stop - start)
    if self.fmt == 'B': return data if isinstance(index, slice) else data[0]
    vals = struct.unpack(f'<{(stop-start)//self.el_sz}{self.fmt}', data)
    return vals if isinstance(index, slice) else vals[0]
  def __setitem__(self, index, val):
    start = (index.start or 0) * self.el_sz if isinstance(index, slice) else index * self.el_sz
    if self.fmt == 'B':
      data = bytes(val) if isinstance(val, (bytes, bytearray, memoryview)) else bytes([val])
    elif isinstance(index, slice):
      data = struct.pack(f'<{len(val)}{self.fmt}', *val)
    else:
      data = struct.pack(f'<{self.fmt}', val)
    self.dev._bulk_write(RemoteCmd.MMIO_WRITE, self.residx, self.off + start, data)
  def view(self, offset=0, size=None, fmt=None):
    return RemoteMMIOInterface(self.dev, self.residx, size or (self.nbytes - offset), fmt or self.fmt, self.off + offset)

class RemotePCIDevice:
  def __init__(self, devpref, pcibus, sock):
    self.sock, self.pcibus, self.dev_id = sock, pcibus, 0
    for buft in [socket.SO_SNDBUF, socket.SO_RCVBUF]: self.sock.setsockopt(socket.SOL_SOCKET, buft, 64 << 20)
    self._lock_fd = self._flock_acquire(f"{devpref.lower()}_{pcibus.lower()}.lock")
    self._mmio_writes_since_drain = 0
    self._mmio_drain_every = max(1, int(os.environ.get("AMD_MMIO_DRAIN_EVERY", "128")))

  @staticmethod
  def _flock_acquire(name):
    import fcntl
    lock_name = temp(name)
    lock_fd = os.open(lock_name, os.O_RDWR | os.O_CREAT, 0o666)
    try: fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: raise RuntimeError(f"eGPU lock held: {lock_name} (only one TinyGPU client at a time)")
    return lock_fd

  @staticmethod
  def _recvall(sock, n):
    data = b''
    while len(data) < n and (chunk := sock.recv(n - len(data))): data += chunk
    if len(data) < n: raise RuntimeError("TinyGPU connection closed")
    return data

  @staticmethod
  def _rpc(sock, dev_id, cmd, *args, bar=0, readout_size=0, payload=b'', has_fd=False):
    sock.sendall(struct.pack('<BIIQQQ', cmd, dev_id, bar, *(*args, 0, 0, 0)[:3]) + payload)
    if has_fd:
      msg, anc, _, _ = sock.recvmsg(17, socket.CMSG_LEN(4))
      fd = struct.unpack('<i', anc[0][2][:4])[0]
    else:
      msg, fd = RemotePCIDevice._recvall(sock, 17), None
    resp = struct.unpack('<BQQ', msg)
    if resp[0] != 0:
      raise RuntimeError(f"TinyGPU RPC failed: {RemotePCIDevice._recvall(sock, resp[1]).decode('utf-8') if resp[1] > 0 else 'unknown error'}")
    return (resp[1], resp[2]) + ((RemotePCIDevice._recvall(sock, readout_size) if readout_size > 0 else None),) + (fd,)

  def _bulk_read(self, cmd, idx, offset, size):
    return unwrap(self._rpc(self.sock, self.dev_id, cmd, offset, size, bar=idx, readout_size=size)[2])
  def drain_mmio(self, bar: int = 5, reg: int = 0x2004):
    """MMIO read round-trip so TinyGPU processes prior fire-and-forget writes."""
    self._bulk_read(RemoteCmd.MMIO_READ, bar, reg * 4, 4)
    self._mmio_writes_since_drain = 0
  def _bulk_write(self, cmd, idx, offset, data):
    self.sock.sendall(struct.pack('<BIIQQQ', cmd, self.dev_id, idx, offset, len(data), 0) + data)
    if cmd != RemoteCmd.MMIO_WRITE:
      return
    self._mmio_writes_since_drain += 1
    if self._mmio_writes_since_drain >= self._mmio_drain_every:
      self.drain_mmio(bar=idx)

  def alloc_sysmem(self, size, contiguous=False):
    mapped_size, _, _, fd = self._rpc(self.sock, self.dev_id, RemoteCmd.MAP_SYSMEM_FD, size, int(contiguous), has_fd=True)
    mem = MMIOInterface(FileIOInterface(fd).mmap(0, mapped_size, mmap.PROT_READ | mmap.PROT_WRITE, mmap.MAP_SHARED, 0), mapped_size, fmt='B')
    raw = bytes(mem[0:min(mapped_size, 0x10000)])
    pairs, off = [], 0
    while off + 16 <= len(raw):
      p, sz = struct.unpack_from('<QQ', raw, off)
      if sz == 0: break
      pairs.append((p, sz)); off += 16
    page_list = [p + i for p, sz in pairs for i in range(0, sz, 0x1000)][:ceildiv(size, 0x1000)]
    return mem, page_list

  def reset(self):
    self._rpc(self.sock, self.dev_id, RemoteCmd.RESET)

  def write_config(self, offset: int, size: int, val: int):
    self._rpc(self.sock, self.dev_id, RemoteCmd.CFG_WRITE, offset, size, val)

  def mask_msi(self) -> list[str]:
    """Stop the eGPU from asserting IRQs to the macOS USB4 bridge.

    Root cause of the recurring `apciec unhandled interrupts (0x200000)` kernel
    panic: TinyGPU.app passes raw MMIO/BAR but installs no interrupt handler, so
    once CP/MEC firmware runs the GPU sends MSIs the AppleT8103PCIe bridge cannot
    route. We keep the device polling-only: disable legacy INTx (PCI command bit
    10) and clear the MSI/MSI-X enable bits in config space. Bus-master (DMA for
    GART sysmem) is left untouched."""
    cleared: list[str] = []
    with contextlib.suppress(Exception):
      cmd = self.read_config(0x04, 2)
      if not (cmd & (1 << 10)):
        self.write_config(0x04, 2, (cmd | (1 << 10)) & 0xffff)
      cleared.append("intx")
    with contextlib.suppress(Exception):
      status = self.read_config(0x06, 2)
      if not (status & (1 << 4)):
        return cleared  # no PCI capability list
      cap = self.read_config(0x34, 1) & 0xfc
      seen = 0
      while cap and cap != 0xfc and seen < 48:
        seen += 1
        cap_id = self.read_config(cap, 1) & 0xff
        nxt = self.read_config(cap + 1, 1) & 0xfc
        if cap_id == 0x05:  # MSI capability — clear MSI Enable (bit 0 of Message Control)
          mc = self.read_config(cap + 2, 2)
          if mc & 0x1:
            self.write_config(cap + 2, 2, mc & ~0x1)
          cleared.append(f"msi@{cap:#x}")
        elif cap_id == 0x11:  # MSI-X — clear MSI-X Enable (bit 15), set Function Mask (bit 14)
          mc = self.read_config(cap + 2, 2)
          self.write_config(cap + 2, 2, (mc & ~(1 << 15)) | (1 << 14))
          cleared.append(f"msix@{cap:#x}")
        cap = nxt
    return cleared

  def amd_cfg_reset(self):
    """Linux vi_asic_pci_config_reset: write AMDGPU_ASIC_RESET_DATA to cfg 0x7c."""
    self.write_config(AMDGPU_ASIC_RESET_CFG_OFF, 4, AMDGPU_ASIC_RESET_DATA)

  def poll_config(self, wait_s: float = 5.0) -> tuple[int, int]:
    deadline = time.time() + wait_s
    vid = did = 0xffff
    while time.time() < deadline:
      with contextlib.suppress(Exception):
        vid = self.read_config(0, 2) & 0xffff
        did = self.read_config(2, 2) & 0xffff
        if vid == 0x1002:
          return vid, did
      time.sleep(0.05)
    return vid, did

  def poll_memsize_ready(self, mmio, wait_s: float = 2.0) -> bool:
    """Linux vi_asic_pci_config_reset: wait until mmCONFIG_MEMSIZE != 0xffffffff."""
    deadline = time.time() + wait_s
    while time.time() < deadline:
      with contextlib.suppress(Exception):
        if int(mmio[REG_CONFIG_MEMSIZE]) != 0xffffffff:
          return True
      time.sleep(0.001)
    return False

  def software_reset(self, wait_s: float = 5.0, mode: str = "auto") -> tuple[int, int]:
    """Reset eGPU using one of several strategies (AMD_RESET_MODE)."""
    mode = mode or os.environ.get("AMD_RESET_MODE", "auto")
    wait_s = float(os.environ.get("AMD_EGPU_RESET_WAIT_S", wait_s))
    vid = self.read_config(0, 2) & 0xffff
    if mode == "mmio":
      return vid, self.read_config(2, 2) & 0xffff

    def do_pci():
      self.reset()
      self.bar_info.cache_clear()

    def try_amd_cfg():
      with contextlib.suppress(Exception):
        if (self.read_config(0, 2) & 0xffff) == 0xffff:
          return
        self.amd_cfg_reset()
        time.sleep(0.1)

    strategies = {
      "pci": [do_pci],
      "amd_cfg": [try_amd_cfg],
      "gentle": [try_amd_cfg, do_pci],
      "full": [try_amd_cfg, do_pci, try_amd_cfg],
      "aggressive": [do_pci, try_amd_cfg, do_pci],
    }
    steps = strategies.get(mode, strategies["gentle"] if vid == 0x1002 else strategies["aggressive"])

    if mode == "auto":
      if vid == 0xffff:
        steps = []  # never hot-reset a missing device
      elif vid == 0x1002:
        steps = [try_amd_cfg]
      else:
        steps = [do_pci]

    attempts = max(1, int(os.environ.get("AMD_EGPU_RESET_ATTEMPTS", 3)))
    per_try = wait_s / attempts if vid != 0xffff else wait_s
    for i in range(attempts):
      if i and steps:
        print(f"polaris: reset retry {i + 1}/{attempts} mode={mode}", flush=True)
        time.sleep(min(0.5 * (2 ** i), 2.0))
      for step in steps:
        step()
      vid, did = self.poll_config(wait_s=per_try)
      if vid == 0x1002:
        time.sleep(float(os.environ.get("AMD_BOOT_SMC_SETTLE_MS", 250)) / 1000.0)
        return vid, did
    return vid, did

  def read_config(self, offset, size): return self._rpc(self.sock, self.dev_id, RemoteCmd.CFG_READ, offset, size)[0]
  @functools.cache
  def bar_info(self, bar_idx): return self._rpc(self.sock, self.dev_id, RemoteCmd.MAP_BAR, bar=bar_idx)[:2]
  def map_bar(self, bar, off=0, size=None, fmt='B'):
    return RemoteMMIOInterface(self, bar, size or self.bar_info(bar)[1], fmt).view(off, size, fmt)

class APLRemotePCIDevice(RemotePCIDevice):
  APP_PATH = "/Applications/TinyGPU.app/Contents/MacOS/TinyGPU"

  def __init__(self, devpref="AMD", pcibus="usb4"):
    self._sock_path = os.environ.get("APL_REMOTE_SOCK", temp("tinygpu.sock"))
    self._server_proc = None
    self.sock = self._connect()
    super().__init__(devpref, pcibus, self.sock)

  def _connect(self) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    for i in range(100):
      with contextlib.suppress(ConnectionRefusedError, FileNotFoundError):
        sock.connect(self._sock_path)
        return sock
      if i == 0:
        self._server_proc = subprocess.Popen(
          [self.APP_PATH, "server", self._sock_path],
          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
      time.sleep(0.05)
    raise RuntimeError(f"Failed to connect to TinyGPU at {self._sock_path}. Run: curl -fsSL https://raw.githubusercontent.com/tinygrad/tinygrad/master/extra/setup_tinygpu_osx.sh | sh")

  def restart_server(self):
    """Restart TinyGPU server subprocess (may help recover vid=0xffff)."""
    with contextlib.suppress(Exception):
      self.sock.close()
    if self._server_proc and self._server_proc.poll() is None:
      self._server_proc.terminate()
      with contextlib.suppress(Exception):
        self._server_proc.wait(timeout=2)
    elif self._server_proc is None:
      with contextlib.suppress(Exception):
        subprocess.run(["pkill", "-f", f"TinyGPU server {self._sock_path}"], timeout=2)
    time.sleep(0.5)
    self.sock = self._connect()

# =============================================================================
# Chip table (PCI IDs from pci-ids / linux-hardware; family from radeon_family.h)
# =============================================================================
CHIP_RV770 = "rv770"       # radeon_family.h CHIP_RV770 — HD 4850
CHIP_REDWOOD = "redwood"   # radeon_family.h CHIP_REDWOOD — HD 5570

@dataclass(frozen=True)
class ChipInfo:
  name: str
  family: str                 # CHIP_* string
  pci_ids: tuple[int, ...]    # device IDs (vendor always 0x1002)
  terrascale: int             # 1 = R700, 2 = Evergreen
  has_ls_compute: bool        # Evergreen+ LS compute (Mesa evergreen_compute)
  llvm_mcpu: str
  note: str

CHIPS: dict[str, ChipInfo] = {
  "hd5570": ChipInfo(
    name="Radeon HD 5570",
    family=CHIP_REDWOOD,
    pci_ids=(0x68D8, 0x68D9, 0x68DA, 0x68C1, 0x68C7, 0x68C8, 0x68C9),
    terrascale=2,
    has_ls_compute=True,
    llvm_mcpu="redwood",
    note="Evergreen Redwood PRO/LE — Mesa r600g OpenCL / evergreen_compute.c",
  ),
  "hd4850": ChipInfo(
    name="Radeon HD 4850",
    family=CHIP_RV770,
    pci_ids=(0x9442, 0x9440, 0x944E),
    terrascale=1,
    has_ls_compute=False,
    llvm_mcpu="rv770",
    note="R700 RV770 — GFX CP only here; no Evergreen LS compute",
  ),
}

def resolve_chip(argv: list[str] | None = None) -> ChipInfo:
  argv = argv if argv is not None else sys.argv[1:]
  key = os.environ.get("TS_CHIP", "hd5570").lower()
  for i, arg in enumerate(argv):
    if arg.startswith("--chip="):
      key = arg.split("=", 1)[1].lower()
    elif arg == "--chip" and i + 1 < len(argv):
      key = argv[i + 1].lower()
  if key not in CHIPS:
    raise SystemExit(f"unknown --chip={key!r}; choose {sorted(CHIPS)}")
  return CHIPS[key]

# =============================================================================
# Registers / PM4 — ref/linux radeon evergreend.h + r600d.h
# =============================================================================
# Byte MMIO offsets (WREG32 style). SET_CONFIG/CONTEXT use these as absolute
# byte addresses; packet offset = (addr - START) >> 2.

# r600d.h / evergreend.h — CP ring (r600_cp_resume)
REG_CP_RB_BASE = 0xC100
REG_CP_RB_CNTL = 0xC104
REG_CP_RB_RPTR_WR = 0xC108
REG_CP_RB_RPTR_ADDR = 0xC10C
REG_CP_RB_RPTR_ADDR_HI = 0xC110
REG_CP_RB_WPTR = 0xC114
REG_CP_RB_WPTR_ADDR = 0xC118
REG_CP_RB_WPTR_ADDR_HI = 0xC11C
REG_CP_RB_WPTR_DELAY = 0x8704
REG_CP_RB_RPTR = 0x8700
REG_CP_ME_CNTL = 0x86D8          # R_0086D8_CP_ME_CNTL
REG_CP_SEM_WAIT_TIMER = 0x85BC
REG_CP_DEBUG = 0xC1FC
REG_SCRATCH_ADDR = 0x8544  # r600d.h SCRATCH_ADDR (not SCRATCH_REGx)
REG_SCRATCH_UMSK = 0x8540
REG_GRBM_SOFT_RESET = 0x8020     # r600d.h GRBM_SOFT_RESET
REG_GRBM_STATUS = 0x8010
SOFT_RESET_CP = 1 << 0

# evergreend.h — VGT compute (config space)
REG_VGT_NUM_INDICES = 0x8970
REG_VGT_COMPUTE_START_X = 0x899C
REG_VGT_COMPUTE_START_Y = 0x89A0
REG_VGT_COMPUTE_START_Z = 0x89A4
REG_VGT_COMPUTE_THREAD_GROUP_SIZE = 0x89AC

# evergreend.h — context regs (SET_CONTEXT_REG)
REG_SPI_COMPUTE_NUM_THREAD_X = 0x286EC
REG_SPI_COMPUTE_NUM_THREAD_Y = 0x286F0
REG_SPI_COMPUTE_NUM_THREAD_Z = 0x286F4
REG_SQ_PGM_START_LS = 0x288D0
REG_SQ_PGM_RESOURCES_LS = 0x288D4
REG_SQ_PGM_RESOURCES_LS_2 = 0x288D8
REG_SQ_LDS_ALLOC = 0x288E8

# Packet3 (evergreend.h)
PKT_TYPE3 = 3
PKT3_NOP = 0x10
PKT3_DISPATCH_DIRECT = 0x15
PKT3_INDIRECT_BUFFER = 0x32
PKT3_EVENT_WRITE = 0x46
PKT3_SET_CONFIG_REG = 0x68
PKT3_SET_CONTEXT_REG = 0x69
PKT3_ME_INITIALIZE = 0x44
PACKET3_SET_CONFIG_REG_START = 0x00008000
PACKET3_SET_CONTEXT_REG_START = 0x00028000
# Mesa / SI: compute packets set header bit1 (PACKET3_COMPUTE). Evergreen r600g
# uses the same PKT3C() for DISPATCH / SET_CONTEXT on the compute CS.
PACKET3_COMPUTE_MODE = 1 << 1
DISPATCH_INITIATOR_COMPUTE_SHADER_EN = 1

RB_RPTR_WR_ENA = 1 << 31
RB_NO_UPDATE = 1 << 27

def packet3(op: int, n: int, compute: bool = False) -> int:
  """PACKET3(op, n) — evergreend.h; optional compute bit (Mesa PKT3C)."""
  hdr = (PKT_TYPE3 << 30) | ((n & 0x3FFF) << 16) | ((op & 0xFF) << 8)
  if compute:
    hdr |= PACKET3_COMPUTE_MODE
  return hdr

def S_0288D4_NUM_GPRS(x: int) -> int:
  return (x & 0xFF) << 0

def S_0288D4_DX10_CLAMP(x: int) -> int:
  return (x & 1) << 13

def S_0288D4_STACK_SIZE(x: int) -> int:
  return (x & 0xFF) << 8

# =============================================================================
# Placeholder CF/ALU binary (Evergreen LS) — replaced when HW + llvm-mc land
# =============================================================================
# Real Evergreen compute shaders are CF + ALU clause binaries (r600 ISA), not
# GCN VOP2. Until we assemble with llvm -march=r600 -mcpu=redwood, ship a
# recognizable stub: CF END + padding. Dispatch IB still encodes correctly.
#
# Layout comment (EG): SQ_PGM_START_LS is in 256-byte units (va >> 8), same as
# Mesa evergreen_emit_cs_shader.

def build_shader_stub_evergreen_add() -> bytes:
  """Minimal placeholder program blob (not executable ALU yet).

  Word0: CF_END-ish sentinel 0x00000000; rest NOP pad to 256B alignment unit.
  Selftest only checks length/alignment + PM4; HW will need a real r600 binary.
  """
  # 64 dwords = 256 bytes — one PGM unit
  words = [0x00000000] + [0x00000000] * 63
  return b"".join(struct.pack("<I", w) for w in words)

ADD_SHADER = build_shader_stub_evergreen_add()
OP = lambda x, y: x + y
OP_NAME = "add"

# =============================================================================
# PM4 builders (Mesa evergreen_emit_cs_shader + evergreen_emit_dispatch)
# =============================================================================
class PM4Builder:
  """Evergreen compute IB builder (config + context + DISPATCH_DIRECT)."""

  def __init__(self, compute: bool = True):
    self.words: list[int] = []
    self.compute = compute

  def emit(self, *vals: int):
    self.words.extend(int(v) & 0xFFFFFFFF for v in vals)

  def pkt3(self, op: int, *vals: int, compute: bool | None = None):
    use_c = self.compute if compute is None else compute
    n = max(len(vals) - 1, 0)
    self.words.append(packet3(op, n, compute=use_c))
    self.words.extend(int(v) & 0xFFFFFFFF for v in vals)

  def set_config_reg(self, reg_byte: int, value: int):
    off = (reg_byte - PACKET3_SET_CONFIG_REG_START) >> 2
    if not (0 <= off < 0x2B00):
      raise ValueError(f"config reg {reg_byte:#x} off={off:#x} out of range")
    # SET_CONFIG is not compute-flagged in Mesa for VGT_* 
    self.pkt3(PKT3_SET_CONFIG_REG, off, value, compute=False)

  def set_config_reg_seq(self, reg_byte: int, *values: int):
    off = (reg_byte - PACKET3_SET_CONFIG_REG_START) >> 2
    n = len(values)  # PACKET3 count = n (reg + n values → count field = n)
    # evergreend: PACKET3(op, n) where n = number of following dwords - 1
    # set_config_reg_seq emits: header, offset, v0, v1, ... → following = 1+len
    self.words.append(packet3(PKT3_SET_CONFIG_REG, len(values), compute=False))
    self.words.append(off & 0xFFFFFFFF)
    self.words.extend(int(v) & 0xFFFFFFFF for v in values)

  def set_context_reg(self, reg_byte: int, value: int):
    off = (reg_byte - PACKET3_SET_CONTEXT_REG_START) >> 2
    if not (0 <= off < 0x400):
      raise ValueError(f"context reg {reg_byte:#x} off={off:#x} out of range")
    self.pkt3(PKT3_SET_CONTEXT_REG, off, value, compute=True)

  def set_context_reg_seq(self, reg_byte: int, *values: int):
    off = (reg_byte - PACKET3_SET_CONTEXT_REG_START) >> 2
    self.words.append(packet3(PKT3_SET_CONTEXT_REG, len(values), compute=True))
    self.words.append(off & 0xFFFFFFFF)
    self.words.extend(int(v) & 0xFFFFFFFF for v in values)

  def emit_cs_shader(self, shader_gpu_addr: int, ngpr: int = 4, nstack: int = 1):
    """evergreen_emit_cs_shader: SQ_PGM_START_LS + RESOURCES (va >> 8)."""
    va_lo = (shader_gpu_addr >> 8) & 0xFFFFFFFF
    rsrc = S_0288D4_NUM_GPRS(ngpr) | S_0288D4_DX10_CLAMP(1) | S_0288D4_STACK_SIZE(nstack)
    self.set_context_reg_seq(REG_SQ_PGM_START_LS, va_lo, rsrc, 0)

  def emit_dispatch(self, block=(1, 1, 1), grid=(1, 1, 1), lds_dwords: int = 0, num_waves: int = 1):
    """evergreen_emit_dispatch (direct): VGT + SPI threads + DISPATCH_DIRECT."""
    group_size = int(block[0]) * int(block[1]) * int(block[2])
    self.set_config_reg(REG_VGT_NUM_INDICES, group_size)
    self.set_config_reg_seq(REG_VGT_COMPUTE_START_X, 0, 0, 0)
    self.set_config_reg(REG_VGT_COMPUTE_THREAD_GROUP_SIZE, group_size)
    self.set_context_reg_seq(REG_SPI_COMPUTE_NUM_THREAD_X, block[0], block[1], block[2])
    self.set_context_reg(REG_SQ_LDS_ALLOC, (lds_dwords & 0x3FFF) | ((num_waves & 0x3F) << 14))
    self.pkt3(PKT3_DISPATCH_DIRECT, grid[0], grid[1], grid[2], DISPATCH_INITIATOR_COMPUTE_SHADER_EN,
              compute=True)

  def build_dispatch_ib(self, shader_gpu_addr: int, out_va: int, a_va: int, b_va: int,
                        block=(1, 1, 1), grid=(1, 1, 1)) -> list[int]:
    """Full Evergreen compute IB skeleton.

    USER_DATA / RAT / global pool bindings are **not** wired yet (Mesa uses a
    compute memory pool + RAT). out/a/b VAs are recorded in trailing NOPs so
    dry-run/selftest can assert the intended buffer map; real RAT setup lands
    with HW bring-up.
    """
    self.words = []
    self.emit_cs_shader(shader_gpu_addr)
    self.emit_dispatch(block=block, grid=grid)
    # Scratch markers (NOP payloads) — not executed as regs
    self.pkt3(PKT3_NOP, lo32(out_va), hi32(out_va), compute=True)
    self.pkt3(PKT3_NOP, lo32(a_va), hi32(a_va), compute=True)
    self.pkt3(PKT3_NOP, lo32(b_va), hi32(b_va), compute=True)
    return self.words

def build_cp_resume_regs(ring_gpu_addr: int, ring_size: int = 0x10000,
                         wb_gpu_addr: int = 0) -> list[tuple[int, int]]:
  """Ordered MMIO writes mirroring r600_cp_resume (r600.c) — for dry-run dump.

  Returns [(reg_byte, value), ...]. Does not touch hardware.
  """
  # order_base_2(ring_size/8)
  rb_bufsz = (ring_size // 8).bit_length() - 1
  page_log = (4096 // 8).bit_length() - 1
  tmp = (page_log << 8) | rb_bufsz
  seq = [
    (REG_GRBM_SOFT_RESET, SOFT_RESET_CP),
    (REG_GRBM_SOFT_RESET, 0),
    (REG_CP_RB_CNTL, tmp),
    (REG_CP_SEM_WAIT_TIMER, 0),
    (REG_CP_RB_WPTR_DELAY, 0),
    (REG_CP_RB_CNTL, tmp | RB_RPTR_WR_ENA),
    (REG_CP_RB_RPTR_WR, 0),
    (REG_CP_RB_WPTR, 0),
    (REG_CP_RB_RPTR_ADDR, lo32(wb_gpu_addr) & 0xFFFFFFFC),
    (REG_CP_RB_RPTR_ADDR_HI, hi32(wb_gpu_addr) & 0xFF),
    (REG_SCRATCH_ADDR, (wb_gpu_addr >> 8) & 0xFFFFFFFF),
    (REG_SCRATCH_UMSK, 0),
    (REG_CP_RB_CNTL, tmp | RB_NO_UPDATE),
    (REG_CP_RB_BASE, ring_gpu_addr >> 8),
    (REG_CP_DEBUG, (1 << 27) | (1 << 28)),
    (REG_CP_ME_CNTL, 0xFF),  # after ME_INITIALIZE on ring
  ]
  return seq

def build_me_initialize(family: str, max_hw_contexts: int = 8) -> list[int]:
  """r600_cp_start ME_INITIALIZE packet (ring contents)."""
  # r600.c: family >= CHIP_RV770 → contexts path
  words = [
    packet3(PKT3_ME_INITIALIZE, 5, compute=False),
    0x1,
    0x0 if family in (CHIP_RV770, CHIP_REDWOOD) else 0x3,
    max_hw_contexts - 1,
    (1 << 16),  # PACKET3_ME_INITIALIZE_DEVICE_ID(1)
    0,
    0,
  ]
  return words

# =============================================================================
# Host PCI helpers + R700 boot / add (HD 4850)
# =============================================================================
PCI_VID_AMD = 0x1002
PAGE_SIZE = 0x1000
R700_PFP_UCODE_SIZE = 848
R700_PM4_UCODE_SIZE = 1360
CP_ME_HALT = 1 << 28
CP_PFP_HALT = 1 << 26
REG_CP_ME_RAM_DATA = 0xC160
REG_CP_ME_RAM_RADDR = 0xC158
REG_CP_ME_RAM_WADDR = 0xC15C
REG_CP_PFP_UCODE_ADDR = 0xC150
REG_CP_PFP_UCODE_DATA = 0xC154
REG_SCRATCH_REG0 = 0x8500
# RV770 MC regs are at 0x202x (rv770d.h) — NOT R600's 0x218x.
REG_MC_VM_FB_LOCATION = 0x2024
REG_MC_VM_AGP_TOP = 0x2028
REG_MC_VM_AGP_BOT = 0x202C
REG_MC_VM_AGP_BASE = 0x2030
REG_MC_VM_SYSTEM_APERTURE_LOW = 0x2034
REG_MC_VM_SYSTEM_APERTURE_HIGH = 0x2038
REG_MC_VM_SYSTEM_APERTURE_DEFAULT = 0x203C
REG_MC_VM_MB_L1_TLB0_CNTL = 0x2234
REG_MC_VM_MB_L1_TLB1_CNTL = 0x2238
REG_MC_VM_MB_L1_TLB2_CNTL = 0x223C
REG_MC_VM_MB_L1_TLB3_CNTL = 0x2240
REG_MC_VM_MD_L1_TLB0_CNTL = 0x2654
REG_MC_VM_MD_L1_TLB1_CNTL = 0x2658
REG_MC_VM_MD_L1_TLB2_CNTL = 0x265C
REG_HDP_NONSURFACE_BASE = 0x2C04
REG_HDP_NONSURFACE_INFO = 0x2C08
REG_HDP_NONSURFACE_SIZE = 0x2C0C
REG_HDP_DEBUG1 = 0x2F34  # rv770d.h (r7xx coherency quirk)
REG_VM_L2_CNTL = 0x1400
REG_VM_L2_CNTL2 = 0x1404
REG_VM_L2_CNTL3 = 0x1408
REG_VM_CONTEXT0_CNTL = 0x1410
REG_CONFIG_MEMSIZE = 0x5428
R700_MC_CITF_CNTL = 0x25c0          # r600_reg.h — MC blackout control
R600_BIF_FB_EN = 0x5490
R600_BLACKOUT_MASK = 0x3
R600_FB_READ_EN = 1 << 0
R600_FB_WRITE_EN = 1 << 1
# VBIOS ROM dump via MMIO index/data (radeon-style; TinyGPU has no ROM BAR map)
REG_BUS_CNTL = 0x5420
REG_ROM_CNTL = 0x1600
R600_BIOS_ROM_DIS = 1 << 1
R600_SCK_OVERWRITE = 1 << 1
REG_ROM_INDEX = 0xA8
REG_ROM_DATA = 0xAC
REG_CG_SPLL_STATUS = 0x60c
SPLL_CHG_STATUS = 1 << 1
REG_MC_SEQ_MISC0 = 0x2a00
FW_DIR = pathlib.Path(__file__).resolve().parent / "fw"
DEFAULT_VBIOS = FW_DIR / "hd4850_174b_e810.rom"
# rv770d.h TLB / L2 bits (rv770_agp_enable)
ENABLE_L1_TLB = 1 << 0
ENABLE_L1_FRAGMENT_PROCESSING = 1 << 1
SYSTEM_ACCESS_MODE_NOT_IN_SYS = 3 << 3
SYSTEM_APERTURE_UNMAPPED_ACCESS_PASS_THRU = 0 << 5
EFFECTIVE_L1_TLB_SIZE = lambda x: (x) << 15
EFFECTIVE_L1_QUEUE_SIZE = lambda x: (x) << 18
ENABLE_L2_CACHE = 1 << 0
ENABLE_L2_FRAGMENT_PROCESSING = 1 << 1
ENABLE_L2_PTE_CACHE_LRU_UPDATE_BY_WRITE = 1 << 9
EFFECTIVE_L2_QUEUE_SIZE = lambda x: ((x) & 7) << 14
BANK_SELECT = lambda x: (x) << 0
CACHE_UPDATE_MODE = lambda x: (x) << 6
PKT3_MEM_WRITE = 0x3D
PKT3_ME_INITIALIZE = 0x44

def chip_from_pci_did(did: int) -> ChipInfo | None:
  for c in CHIPS.values():
    if did in c.pci_ids:
      return c
  return None

def host_pci_scan() -> list[tuple[str, int, int]]:
  if not OSX:
    return []
  try:
    out = subprocess.check_output(["ioreg", "-r", "-c", "IOPCIDevice", "-l"],
                                  text=True, errors="replace", timeout=10)
  except Exception:
    return []
  import re
  blocks = re.split(r"\+-o ", out)
  found = []
  for b in blocks:
    mname = re.search(r"^(\S+)", b)
    name = mname.group(1) if mname else "?"
    vid_m = re.search(r'"vendor-id"\s*=\s*<([0-9a-fA-F]+)>', b)
    did_m = re.search(r'"device-id"\s*=\s*<([0-9a-fA-F]+)>', b)
    if not vid_m or not did_m:
      continue
    def _le(h: str) -> int:
      bs = bytes.fromhex(h)
      return int.from_bytes(bs[:2] if len(bs) >= 2 else bs, "little")
    v, d = _le(vid_m.group(1)), _le(did_m.group(1))
    if v in (PCI_VID_AMD, 0x174C, 0x1B21):
      found.append((name, v, d))
  return found

def diagnose_host() -> str:
  amd = [(n, v, d) for n, v, d in host_pci_scan() if v == PCI_VID_AMD]
  bridges = [(n, v, d) for n, v, d in host_pci_scan() if v != PCI_VID_AMD]
  lines = []
  if amd:
    lines.append("host PCI AMD: " + ", ".join(f"{n} {v:04x}:{d:04x}" for n, v, d in amd))
  else:
    lines.append("host PCI: no AMD (1002:*) device — GPU not enumerated")
  if bridges:
    lines.append("host PCIe bridges (dock?): " +
                 ", ".join(f"{v:04x}:{d:04x}" for _, v, d in bridges[:6]))
  return "\n".join(lines)

def fetch_radeon_fw(name: str) -> bytes:
  """Fetch radeon/*.bin from linux-firmware (cached under ~/.cache/tinygrad/fw)."""
  return fetch_fw("radeon", name)

class TerrascaleDevice:
  """TinyGPU + R700 (HD 4850) CP bring-up. VRAM BAR0 dead → AGP/sysmem rings."""

  def __init__(self, chip: ChipInfo | None = None, wait_s: float = 0.0):
    self.chip = chip
    self.pci = APLRemotePCIDevice()
    self.mmio = None
    self.vram = None
    self.agp_start = 0
    self.agp_end = 0
    self.agp_size = 0
    self.ring_size = 0x10000
    self.ring_mem = None
    self.ring_gpu = 0
    self.wb_mem = None
    self.wb_gpu = 0
    self.wptr = 0
    self._booted = False
    self.vid, self.did = self._open_config(wait_s=wait_s)
    if self.chip is None:
      self.chip = chip_from_pci_did(self.did) or CHIPS["hd4850"]
      if chip_from_pci_did(self.did) is None and self.vid == PCI_VID_AMD:
        print(f"warning: unknown AMD did={self.did:04x}; using {self.chip.name}",
              file=sys.stderr)
    self.map_mmio()
    with contextlib.suppress(Exception):
      self.vram = self.pci.map_bar(0)
    if OSX and getenv("AMD_BOOT_MASK_INTERRUPTS", 1):
      with contextlib.suppress(Exception):
        masked = self.pci.mask_msi()
        if DEBUG:
          print(f"terrascale: masked IRQs {masked}", flush=True)

  def _read_ids(self, retries: int = 5, delay_s: float = 0.25) -> tuple[int, int]:
    vid = did = 0xFFFF
    last_err = None
    for _ in range(max(1, retries)):
      try:
        vid = self.pci.read_config(0, 2) & 0xFFFF
        did = self.pci.read_config(2, 2) & 0xFFFF
        if vid == PCI_VID_AMD or vid != 0xFFFF:
          return vid, did
      except RuntimeError as e:
        last_err = e
        if "Driver not available" in str(e):
          raise RuntimeError(
            "TinyGPU: Driver not available (dext loaded, but no GPU bound).\n"
            f"{diagnose_host()}\n"
            "Check GPU power/seating in dock."
          ) from e
      time.sleep(delay_s)
    if last_err:
      raise last_err
    return vid, did

  def _open_config(self, wait_s: float = 0.0) -> tuple[int, int]:
    deadline = time.time() + max(0.0, wait_s)
    while True:
      try:
        vid, did = self._read_ids()
        if vid == PCI_VID_AMD:
          return vid, did
        if vid == 0xFFFF and getenv("AMD_EGPU_RESTART_SERVER", 1):
          print("terrascale: pci=0xffff — restarting TinyGPU server", flush=True)
          self.pci.restart_server()
          time.sleep(1.5)
          vid, did = self._read_ids(retries=8, delay_s=0.4)
          if vid == PCI_VID_AMD:
            return vid, did
        if time.time() >= deadline:
          raise RuntimeError(f"no AMD GPU (pci={vid:04x}:{did:04x})\n{diagnose_host()}")
      except RuntimeError as e:
        if "Driver not available" in str(e) and time.time() < deadline:
          print("terrascale: waiting for GPU…", flush=True)
          time.sleep(2.0)
          continue
        raise
      time.sleep(1.0)

  def map_mmio(self):
    for bar in (2, 5, 0):
      try:
        base, size = self.pci.bar_info(bar)
        if size and size >= 0x8000:
          self.mmio = self.pci.map_bar(bar)
          self.mmio_bar, self.mmio_size = bar, size
          return bar, base, size
      except Exception:
        continue
    raise RuntimeError("no suitable MMIO BAR")

  def rreg(self, byte_off: int) -> int:
    if self.mmio is None:
      self.map_mmio()
    return struct.unpack("<I", bytes(self.mmio[byte_off:byte_off + 4]))[0]

  def wreg(self, byte_off: int, val: int):
    if self.mmio is None:
      self.map_mmio()
    self.mmio[byte_off:byte_off + 4] = struct.pack("<I", val & 0xFFFFFFFF)

  def probe(self) -> dict:
    bars = {}
    for i in range(6):
      with contextlib.suppress(Exception):
        bars[i] = self.pci.bar_info(i)
    info = {
      "vendor": self.vid, "device": self.did,
      "rev": self.pci.read_config(8, 1) & 0xFF,
      "chip": self.chip.name, "family": self.chip.family,
      "id_match": self.did in self.chip.pci_ids,
      "bars": bars, "host_diag": diagnose_host(),
      "grbm_status": self.rreg(REG_GRBM_STATUS),
      "cp_me_cntl": self.rreg(REG_CP_ME_CNTL),
      "config_memsize": self.rreg(REG_CONFIG_MEMSIZE),
      "mmio_bar": getattr(self, "mmio_bar", None),
    }
    return info

  # ----- AGP / sysmem (VRAM BAR0 writes are dead on this eGPU) -----
  def program_agp(self):
    """Program MC AGP aperture so host DMA addrs are GPU-reachable.

    R700 AGP_TOP/BOT are 16-bit (mc_addr >> 16). RV770 regs are rv770d.h 0x202x.
    AGP_BASE=0 → host_dma = mc_addr.

    Critical: do NOT leave FB_LOCATION at 0 while AGP also covers 0 — CP ring
    fetches then hit dead FB (zeros) instead of host. Park a stub FB high and
    keep AGP on the low DMA range (rv770_mc_program non-overlap).
    """
    self.agp_start = 0
    self.agp_end = 0xDFFFFFFF  # leave 0xE0000000+ for stub FB
    self.agp_size = self.agp_end - self.agp_start + 1
    _ = self.rreg(REG_HDP_DEBUG1)
    for i in range(32):
      base = 0x2C14 + i * 0x18
      for off in (0, 4, 8, 12, 16):
        with contextlib.suppress(Exception):
          self.wreg(base + off, 0)
    # Stub FB at 0xE0000000–0xE0FFFFFF (16MB) — unused; keeps AGP/FB disjoint.
    fb_start_24 = 0xE0
    fb_end_24 = 0xE0
    self.wreg(REG_MC_VM_FB_LOCATION, (fb_end_24 << 16) | fb_start_24)
    self.wreg(REG_MC_VM_SYSTEM_APERTURE_LOW, self.agp_start >> 12)
    self.wreg(REG_MC_VM_SYSTEM_APERTURE_HIGH, self.agp_end >> 12)
    self.wreg(REG_MC_VM_SYSTEM_APERTURE_DEFAULT, (fb_start_24 << 24) >> 12)
    self.wreg(REG_HDP_NONSURFACE_BASE, (fb_start_24 << 24) >> 8)
    self.wreg(REG_HDP_NONSURFACE_INFO, (2 << 7))
    self.wreg(REG_HDP_NONSURFACE_SIZE, 0x3FFFFFFF)
    self.wreg(REG_MC_VM_AGP_BASE, 0)
    self.wreg(REG_MC_VM_AGP_TOP, (self.agp_end >> 16) & 0xFFFF)
    self.wreg(REG_MC_VM_AGP_BOT, (self.agp_start >> 16) & 0xFFFF)
    self.agp_enable()
    # VBIOS leaves MC blacked out (CITF & 3 == 3). Without clearing, AGP/host
    # fetches return zeros and CP rptr advances on fake PACKET0s. Only clear
    # blackout — do NOT poke BIF_FB_EN until VRAM is trained (can hang MC).
    citf = self.rreg(R700_MC_CITF_CNTL)
    if citf != 0xFFFFFFFF and (citf & R600_BLACKOUT_MASK):
      self.wreg(R700_MC_CITF_CNTL, citf & ~R600_BLACKOUT_MASK)
      time.sleep(0.001)
      citf2 = self.rreg(R700_MC_CITF_CNTL)
      if DEBUG:
        print(f"terrascale: MC blackout {citf:#x} → {citf2:#x}", flush=True)
      if citf2 == 0xFFFFFFFF:
        raise RuntimeError("MC hung after clearing blackout — power-cycle the eGPU dock")
    if DEBUG:
      top, bot = self.rreg(REG_MC_VM_AGP_TOP), self.rreg(REG_MC_VM_AGP_BOT)
      fb = self.rreg(REG_MC_VM_FB_LOCATION)
      print(f"terrascale: AGP MC {self.agp_start:#x}-{self.agp_end:#x} "
            f"(TOP={top:#x} BOT={bot:#x}) FB_LOC={fb:#x}", flush=True)

  def agp_enable(self):
    """rv770_agp_enable — L2 + L1 TLB pass-through, VM contexts off."""
    self.wreg(REG_VM_L2_CNTL,
              ENABLE_L2_CACHE | ENABLE_L2_FRAGMENT_PROCESSING |
              ENABLE_L2_PTE_CACHE_LRU_UPDATE_BY_WRITE | EFFECTIVE_L2_QUEUE_SIZE(7))
    self.wreg(REG_VM_L2_CNTL2, 0)
    self.wreg(REG_VM_L2_CNTL3, BANK_SELECT(0) | CACHE_UPDATE_MODE(2))
    tmp = (ENABLE_L1_TLB | ENABLE_L1_FRAGMENT_PROCESSING |
           SYSTEM_ACCESS_MODE_NOT_IN_SYS |
           SYSTEM_APERTURE_UNMAPPED_ACCESS_PASS_THRU |
           EFFECTIVE_L1_TLB_SIZE(5) | EFFECTIVE_L1_QUEUE_SIZE(5))
    for reg in (REG_MC_VM_MD_L1_TLB0_CNTL, REG_MC_VM_MD_L1_TLB1_CNTL,
                REG_MC_VM_MD_L1_TLB2_CNTL,
                REG_MC_VM_MB_L1_TLB0_CNTL, REG_MC_VM_MB_L1_TLB1_CNTL,
                REG_MC_VM_MB_L1_TLB2_CNTL, REG_MC_VM_MB_L1_TLB3_CNTL):
      self.wreg(reg, tmp)
    for i in range(7):
      self.wreg(REG_VM_CONTEXT0_CNTL + i * 4, 0)

  def agp_mc_addr(self, paddr: int) -> int:
    if paddr > self.agp_end - self.agp_start:
      raise ValueError(f"DMA addr {paddr:#x} outside AGP size {self.agp_size:#x}")
    return self.agp_start + paddr

  def alloc_agp(self, size: int) -> tuple[int, object, list[int]]:
    nbytes = round_up(size, PAGE_SIZE)
    mem, paddrs = self.pci.alloc_sysmem(nbytes, contiguous=True)
    if not paddrs:
      raise RuntimeError("alloc_sysmem returned no paddrs")
    for i in range(1, len(paddrs)):
      if paddrs[i] != paddrs[i - 1] + PAGE_SIZE:
        raise RuntimeError("need physically contiguous sysmem for AGP")
    if self.agp_size == 0:
      self.program_agp()
    gpu = self.agp_mc_addr(paddrs[0])
    sysmem_dma_flush(mem, nbytes)
    return gpu, mem, paddrs

  # ----- CP firmware + ring -----
  def cp_stop(self):
    self.wreg(REG_CP_ME_CNTL, CP_ME_HALT | CP_PFP_HALT)
    self.wreg(REG_SCRATCH_UMSK, 0)

  def load_cp_fw(self):
    """rv770_cp_load_microcode — big-endian words in RV770_{pfp,me}.bin."""
    pfp = fetch_radeon_fw("RV770_pfp.bin")
    me = fetch_radeon_fw("RV770_me.bin")
    if len(pfp) != R700_PFP_UCODE_SIZE * 4:
      raise RuntimeError(f"bad PFP fw size {len(pfp)} expect {R700_PFP_UCODE_SIZE*4}")
    if len(me) != R700_PM4_UCODE_SIZE * 4:
      raise RuntimeError(f"bad ME fw size {len(me)} expect {R700_PM4_UCODE_SIZE*4}")
    self.cp_stop()
    self.wreg(REG_CP_RB_CNTL, RB_NO_UPDATE | (15 << 8) | 3)  # BLKSZ=15 BUFSZ=3
    self.wreg(REG_GRBM_SOFT_RESET, SOFT_RESET_CP)
    _ = self.rreg(REG_GRBM_SOFT_RESET)
    time.sleep(0.015)
    self.wreg(REG_GRBM_SOFT_RESET, 0)
    # PFP — be32 in file
    self.wreg(REG_CP_PFP_UCODE_ADDR, 0)
    for i in range(R700_PFP_UCODE_SIZE):
      self.wreg(REG_CP_PFP_UCODE_DATA, struct.unpack_from(">I", pfp, i * 4)[0])
    self.wreg(REG_CP_PFP_UCODE_ADDR, 0)
    # ME
    self.wreg(REG_CP_ME_RAM_WADDR, 0)
    for i in range(R700_PM4_UCODE_SIZE):
      self.wreg(REG_CP_ME_RAM_DATA, struct.unpack_from(">I", me, i * 4)[0])
    self.wreg(REG_CP_PFP_UCODE_ADDR, 0)
    self.wreg(REG_CP_ME_RAM_WADDR, 0)
    self.wreg(REG_CP_ME_RAM_RADDR, 0)
    print(f"terrascale: loaded RV770 PFP={len(pfp)} ME={len(me)}", flush=True)

  def _ring_write_words(self, words: list[int]):
    assert self.ring_mem is not None
    off = (self.wptr * 4) % self.ring_size
    blob = b"".join(struct.pack("<I", w & 0xFFFFFFFF) for w in words)
    end = off + len(blob)
    if end <= self.ring_size:
      self.ring_mem[off:end] = blob
    else:
      first = self.ring_size - off
      self.ring_mem[off:self.ring_size] = blob[:first]
      self.ring_mem[0:len(blob) - first] = blob[first:]
    self.wptr = (self.wptr + len(words)) % (self.ring_size // 4)
    sysmem_dma_flush(self.ring_mem, self.ring_size)

  def _commit_wptr(self):
    self.wreg(REG_CP_RB_WPTR, self.wptr)
    _ = self.rreg(REG_CP_RB_RPTR)  # posting read

  def cp_start(self):
    """r600_cp_start: ME_INITIALIZE on ring, then unhalt."""
    me = build_me_initialize(self.chip.family, max_hw_contexts=8)
    # fix device id already in builder
    self._ring_write_words(me)
    self._commit_wptr()
    self.wreg(REG_CP_ME_CNTL, 0xFF)  # unhalt
    time.sleep(0.01)

  def cp_resume(self):
    """r600_cp_resume with AGP ring + writeback."""
    if self.agp_size == 0:
      self.program_agp()
    self.ring_gpu, self.ring_mem, _ = self.alloc_agp(self.ring_size)
    self.wb_gpu, self.wb_mem, _ = self.alloc_agp(PAGE_SIZE)
    # zero ring/wb
    self.ring_mem[0:self.ring_size] = bytes(self.ring_size)
    self.wb_mem[0:PAGE_SIZE] = bytes(PAGE_SIZE)
    sysmem_dma_flush(self.ring_mem, self.ring_size)
    sysmem_dma_flush(self.wb_mem, PAGE_SIZE)

    self.wreg(REG_GRBM_SOFT_RESET, SOFT_RESET_CP)
    _ = self.rreg(REG_GRBM_SOFT_RESET)
    time.sleep(0.015)
    self.wreg(REG_GRBM_SOFT_RESET, 0)

    rb_bufsz = (self.ring_size // 8).bit_length() - 1
    page_log = (PAGE_SIZE // 8).bit_length() - 1
    tmp = (page_log << 8) | rb_bufsz
    self.wreg(REG_CP_RB_CNTL, tmp)
    self.wreg(REG_CP_SEM_WAIT_TIMER, 0)
    self.wreg(REG_CP_RB_WPTR_DELAY, 0)
    self.wreg(REG_CP_RB_CNTL, tmp | RB_RPTR_WR_ENA)
    self.wreg(REG_CP_RB_RPTR_WR, 0)
    self.wptr = 0
    self.wreg(REG_CP_RB_WPTR, 0)
    # rptr writeback at RADEON_WB_CP_RPTR_OFFSET; scratch wb at offset 0
    rptr_wb = self.wb_gpu + 1024
    self.wreg(REG_CP_RB_RPTR_ADDR, lo32(rptr_wb) & 0xFFFFFFFC)
    self.wreg(REG_CP_RB_RPTR_ADDR_HI, hi32(rptr_wb) & 0xFF)
    self.wreg(REG_SCRATCH_ADDR, (self.wb_gpu >> 8) & 0xFFFFFFFF)
    self.wreg(REG_SCRATCH_UMSK, 0xFF)
    time.sleep(0.001)
    self.wreg(REG_CP_RB_CNTL, tmp)  # enable rptr update
    self.wreg(REG_CP_RB_BASE, self.ring_gpu >> 8)
    self.wreg(REG_CP_DEBUG, (1 << 27) | (1 << 28))
    print(f"terrascale: CP_RB_BASE gpu={self.ring_gpu:#x} size={self.ring_size:#x}", flush=True)
    self.cp_start()

  def ring_test(self, timeout_s: float = 2.0) -> bool:
    """r600_ring_test: SET_CONFIG_REG scratch ← 0xDEADBEEF."""
    scratch = REG_SCRATCH_REG0
    self.wreg(scratch, 0xCAFEDEAD)
    off = (scratch - PACKET3_SET_CONFIG_REG_START) >> 2
    wptr_before = self.wptr
    self._ring_write_words([
      packet3(PKT3_SET_CONFIG_REG, 1, compute=False),
      off,
      0xDEADBEEF,
    ])
    self._commit_wptr()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
      got = self.rreg(scratch)
      if got == 0xDEADBEEF:
        print(f"terrascale: ring_test PASS scratch={got:#x}", flush=True)
        return True
      time.sleep(0.001)
    rptr = self.rreg(REG_CP_RB_RPTR)
    got = self.rreg(scratch)
    print(f"terrascale: ring_test FAIL scratch={got:#x} "
          f"rptr={rptr:#x} wptr={self.wptr:#x}", flush=True)
    if rptr == self.wptr and got == 0xCAFEDEAD:
      print("terrascale: hint — CP consumed dwords but scratch unchanged; "
            "often means ring fetch saw zeros (MC blackout / AGP/FB overlap / "
            "no host DMA). Check CITF blackout and AGP vs FB_LOCATION.", flush=True)
    return False

  def dump_vbios_rom(self, path: pathlib.Path | None = None) -> bytes:
    """Read onboard ATOM ROM via REG_ROM_INDEX/DATA (exact card SSID)."""
    bus, rom = self.rreg(REG_BUS_CNTL), self.rreg(REG_ROM_CNTL)
    self.wreg(REG_BUS_CNTL, bus & ~R600_BIOS_ROM_DIS)
    self.wreg(REG_ROM_CNTL, rom | R600_SCK_OVERWRITE)
    try:
      self.wreg(REG_ROM_INDEX, 0)
      w0 = self.rreg(REG_ROM_DATA)
      hdr = struct.pack("<I", w0)
      if hdr[0] != 0x55 or hdr[1] != 0xAA:
        raise RuntimeError(f"VBIOS bad magic {hdr[:2].hex()}")
      size = max(hdr[2] * 512, 512)
      out = bytearray()
      for off in range(0, size, 4):
        self.wreg(REG_ROM_INDEX, off)
        out += struct.pack("<I", self.rreg(REG_ROM_DATA))
      bios = bytes(out[:size])
    finally:
      self.wreg(REG_BUS_CNTL, bus)
      self.wreg(REG_ROM_CNTL, rom)
    dest = path or DEFAULT_VBIOS
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(bios)
    print(f"terrascale: dumped VBIOS {len(bios)}B → {dest}", flush=True)
    return bios

  def clear_mc_blackout(self):
    citf = self.rreg(R700_MC_CITF_CNTL)
    if citf != 0xFFFFFFFF and (citf & R600_BLACKOUT_MASK):
      self.wreg(R700_MC_CITF_CNTL, citf & ~R600_BLACKOUT_MASK)
      time.sleep(0.001)
      if self.rreg(R700_MC_CITF_CNTL) == 0xFFFFFFFF:
        raise RuntimeError("MC hung after clearing blackout — power-cycle the eGPU dock")

  def prepare_spll_refclk(self) -> dict:
    """Best-effort SPLL reference clock setup before ATOM (R700 / eGPU).

    Discrete RV770 normally uses onboard XTAL (CG_CLKPIN_CNTL). On TB eGPU,
    SPLL_CHG_STATUS often never asserts — try XTAL path and SI-style BIF refclk
    bits (may be no-ops on R700). Returns a status dict for diagnostics.
    """
    CG_CLKPIN_CNTL = 0x660
    CG_CLKPIN_CNTL_2 = 0x664  # SI; may be unused on RV770
    MUX_TCLK_TO_XCLK = 1 << 8
    XTALIN_DIVIDE = 1 << 9
    BCLK_AS_XCLK = 1 << 2
    FORCE_BIF_REFCLK_EN = 1 << 3
    SCLK_MUX_UPDATE = 1 << 26

    pin = self.rreg(CG_CLKPIN_CNTL)
    pin2 = self.rreg(CG_CLKPIN_CNTL_2)
    # Prefer raw XTALIN (no /4, no TCLK mux)
    self.wreg(CG_CLKPIN_CNTL, (pin | BCLK_AS_XCLK) & ~(MUX_TCLK_TO_XCLK | XTALIN_DIVIDE))
    # SI-era: force BIF/PCIe refclk into clock pin block (harmless if RAZ on R700)
    self.wreg(CG_CLKPIN_CNTL_2, pin2 | FORCE_BIF_REFCLK_EN)

    # Request an SPLL mux update and see if CHG ever asserts
    f2 = self.rreg(0x604)
    self.wreg(0x604, (f2 & ~0x1FF) | 1 | SCLK_MUX_UPDATE)
    chg = False
    for _ in range(50):
      st = self.rreg(REG_CG_SPLL_STATUS)
      if st & SPLL_CHG_STATUS:
        chg = True
        break
      time.sleep(0.001)
    self.wreg(0x604, self.rreg(0x604) & ~SCLK_MUX_UPDATE)
    info = {
      "clkpin_before": pin,
      "clkpin_after": self.rreg(CG_CLKPIN_CNTL),
      "clkpin2_before": pin2,
      "clkpin2_after": self.rreg(CG_CLKPIN_CNTL_2),
      "spll_status": self.rreg(REG_CG_SPLL_STATUS),
      "chg": chg,
    }
    if DEBUG or not chg:
      print(f"terrascale: SPLL refclk probe CHG={chg} "
            f"STATUS={info['spll_status']:#x} "
            f"CLKPIN {pin:#x}→{info['clkpin_after']:#x} "
            f"CLKPIN2 {pin2:#x}→{info['clkpin2_after']:#x}", flush=True)
    return info

  def atom_asic_init(self, bios: bytes | None = None) -> None:
    """Run ATOM ASIC_Init via examples_egpu/neural.py (dword index → byte*4).

    On this TB eGPU, CG_SPLL_STATUS.SPLL_CHG_STATUS never asserts, so ATOM's
    SPLL wait is satisfied by synthesizing bit1 on dword index 0x183 reads.
    Do NOT poke BIF_FB_EN here — let ATOM own it.
    """
    egpu = pathlib.Path(__file__).resolve().parents[1] / "examples_egpu"
    if str(egpu) not in sys.path:
      sys.path.insert(0, str(egpu))
    import neural as nl  # noqa: PLC0415

    if bios is None:
      vbios_path = os.environ.get("AMD_BOOT_VBIOS_FILE", str(DEFAULT_VBIOS))
      if os.path.isfile(vbios_path):
        bios = open(vbios_path, "rb").read()
      else:
        bios = self.dump_vbios_rom(pathlib.Path(vbios_path))
    if not nl.check_atom_bios(bios):
      raise RuntimeError("ATOM BIOS header check failed")
    self.clear_mc_blackout()
    self.prepare_spll_refclk()
    os.environ.setdefault("AMD_ATOM_QUIET", "1")
    os.environ.setdefault("AMD_ATOM_JUMP_MAX", "200000")
    # JUMP_BAIL=1: eGPU SPLL never locks; without bail, long waits can hang MC
    # (pci=ffff). SPLL_CHG is still synthesized below so most waits exit cleanly.
    os.environ.setdefault("AMD_ATOM_JUMP_BAIL", "1")
    os.environ.setdefault("AMD_ATOM_JUMP_TIMEOUT_SEC", "15")

    class BootAdapter:
      def __init__(self, d: "TerrascaleDevice"): self.dev = d; self._wcount = 0
      def rreg(self, reg: int) -> int:
        return self.dev.rreg((reg & 0xFFFF) * 4)
      def wreg(self, reg: int, val: int):
        self.dev.wreg((reg & 0xFFFF) * 4, val & 0xFFFFFFFF)
        self._wcount += 1
        # Some regs RAZ as 0xffffffff — only trust PCI vid for liveness.
        if (self._wcount & 0x3F) == 0:
          if self.dev.pci.read_config(0, 2) == 0xFFFF:
            raise RuntimeError("PCI vid=ffff mid-ATOM — power-cycle eGPU")
      def mmio_sync_safe(self):
        with contextlib.suppress(Exception):
          _ = self.dev.rreg(REG_CONFIG_MEMSIZE)
      def post_atom_sync(self):
        self.mmio_sync_safe()
        time.sleep(0.05)

    class R700AtomCard(nl.AtomCard):
      def reg_read(self, reg: int) -> int:
        reg = self._mmio_reg(reg)
        val = super().reg_read(reg)
        if reg == 0x183:  # CG_SPLL_STATUS
          val |= SPLL_CHG_STATUS
        return val

    boot = BootAdapter(self)
    card = R700AtomCard(boot, debug=bool(DEBUG))
    ctx = nl.parse_atom_context(bios)
    exe = nl.AtomExecutor(ctx, card)
    hwi = nl._u16(bios, ctx.data_table + nl.ATOM_DATA_FWI_PTR)
    ps = [0] * 16
    ps[0] = nl._u32(bios, hwi + nl.ATOM_FWI_DEFSCLK_PTR)
    ps[1] = nl._u32(bios, hwi + nl.ATOM_FWI_DEFMCLK_PTR)
    t0 = time.time()
    ret = exe.execute_table(nl.ATOM_CMD_INIT, ps, 16)
    if ret:
      raise RuntimeError(f"atom asic_init failed ret={ret}")
    mem = self.rreg(REG_CONFIG_MEMSIZE)
    misc0 = self.rreg(REG_MC_SEQ_MISC0)
    spll = self.rreg(REG_CG_SPLL_STATUS)
    print(f"terrascale: ATOM done writes={ctx.reg_write_count} "
          f"MEMSIZE={mem:#x} ({mem >> 20}MB) MISC0={misc0:#x} "
          f"SPLL_STATUS={spll:#x} CHG={bool(spll & SPLL_CHG_STATUS)} "
          f"t={time.time() - t0:.1f}s", flush=True)
    self._wake_mrdck()

  def _wake_mrdck(self):
    """Clear MRDCK SLEEP/RESET left set by incomplete ATOM memory bring-up.

    Does not reprogram MPLL dividers (wrong CLKF can hang the MC on eGPU).
    """
    mclk = self.rreg(0x648)
    sleep, reset = (mclk >> 8) & 0xFF, (mclk >> 16) & 0xFF
    if sleep == 0 and reset == 0:
      return
    # Keep DLL_SPEED / DLL_READY / MC_INT; clear sleep+reset; keep READY_READ if set
    base = (mclk & 0x1F) | (mclk & (1 << 6)) | (mclk & (1 << 7)) | (mclk & (1 << 24))
    self.wreg(0x648, base | (0xFF << 16))  # hold reset briefly
    time.sleep(0.01)
    self.wreg(0x648, base)
    m2 = self.rreg(0x648)
    if DEBUG:
      print(f"terrascale: MRDCK wake MCLK {mclk:#x} → {m2:#x} "
            f"(was SLEEP={sleep:#x} RESET={reset:#x})", flush=True)

  def probe_bar0(self) -> bool:
    """Return True if BAR0 write/readback sticks (VRAM usable from host)."""
    if self.vram is None:
      return False
    pat = 0xA5A55A5A
    self.vram[0:4] = struct.pack("<I", pat)
    self.wreg(REG_HDP_DEBUG1, 0)
    _ = self.vram[0]
    time.sleep(0.02)
    got = struct.unpack("<I", bytes(self.vram[0:4]))[0]
    ok = got == pat
    if DEBUG or not ok:
      print(f"terrascale: BAR0 probe wrote={pat:#x} got={got:#x} ok={ok}", flush=True)
    return ok

  def boot(self):
    if self._booted:
      return
    if self.chip.family != CHIP_RV770 and not self.chip.has_ls_compute:
      raise RuntimeError(f"boot path only implemented for RV770/Evergreen; got {self.chip.family}")
    print(f"terrascale: boot {self.chip.name} pci={self.vid:04x}:{self.did:04x}", flush=True)
    if getenv("AMD_BOOT_ATOM", 1):
      try:
        self.atom_asic_init()
      except Exception as e:
        print(f"terrascale: ATOM warning: {e}", flush=True)
      self.probe_bar0()
    self.program_agp()
    if self.chip.family == CHIP_RV770:
      self.load_cp_fw()
      self.cp_resume()
      if not self.ring_test():
        raise RuntimeError("CP ring test failed")
    else:
      # Evergreen LS path still TODO for real ALU; share CP bring-up later
      raise RuntimeError("Evergreen LS compute boot not implemented yet — use HD 4850 path")
    self._booted = True

  def run_add(self, a=(1.0, 2.0, 3.0, 4.0), b=(10.0, 20.0, 30.0, 40.0)) -> list[float]:
    """Vector-add smoke on R700: CP MEM_WRITE of a[i]+b[i] into AGP buffer.

    R700 has no Evergreen LS compute; this validates CP + AGP memory path.
    Host computes sums; GPU CP writes them (same bring-up pattern as ring_test).
    """
    if not self._booted:
      self.boot()
    expected = [float(x) + float(y) for x, y in zip(a, b)]
    out_gpu, out_mem, _ = self.alloc_agp(0x1000)
    out_mem[0:16] = bytes(16)
    sysmem_dma_flush(out_mem, 16)
    # MEM_WRITE: header + addr_lo + addr_hi|flags + lo + hi (two dwords per write)
    # Write 4 floats as 2 MEM_WRITE packets (2 dwords each) or 4 single-dword packs.
    # r600 uses count=3: addr_lo, addr_hi| (1<<18), data0, data1
    words: list[int] = []
    raw = struct.pack("4f", *expected)
    for i in range(0, 16, 8):
      addr = out_gpu + i
      d0, d1 = struct.unpack_from("<II", raw, i)
      words += [
        packet3(PKT3_MEM_WRITE, 3, compute=False),
        lo32(addr) & 0xFFFFFFFC,
        (hi32(addr) & 0xFF) | (1 << 18),
        d0,
        d1,
      ]
    self._ring_write_words(words)
    self._commit_wptr()
    deadline = time.time() + float(os.environ.get("AMD_BOOT_ADD_WAIT_S", "2"))
    result = [0.0, 0.0, 0.0, 0.0]
    while time.time() < deadline:
      sysmem_dma_flush(out_mem, 16)
      result = list(struct.unpack("4f", bytes(out_mem[0:16])))
      if all(abs(r - e) < 1e-5 for r, e in zip(result, expected)):
        result = list(expected)
        break
      time.sleep(0.01)
    print(f"result={result} expected={expected} path=cp_mem_write", flush=True)
    if not all(abs(r - e) < 1e-4 for r, e in zip(result, expected)):
      raise RuntimeError(f"add failed: got {result} expected {expected}")
    return result

def selftest(chip: ChipInfo):
  assert chip.pci_ids
  shader = build_shader_stub_evergreen_add()
  assert len(shader) == 256
  me = build_me_initialize(CHIP_RV770)
  assert me[4] == (1 << 16)
  ib = PM4Builder().build_dispatch_ib(0x10000, 0x20000, 0x30000, 0x40000)
  assert ib[0] >> 30 == PKT_TYPE3
  print(f"selftest=ok chip={chip.name} me_words={len(me)} eg_ib={len(ib)} "
        f"ls_compute={int(chip.has_ls_compute)}")

def dry_run(chip: ChipInfo):
  print(f"chip={chip.name} family={chip.family} terrascale={chip.terrascale}")
  print(f"note={chip.note}")
  if chip.family == CHIP_RV770:
    for reg, val in build_cp_resume_regs(0xAB0000, 0x10000, 0xAC0000):
      print(f"  WREG32({reg:#06x}, {val:#010x})")
    print("  ME_INITIALIZE:", " ".join(f"{w:08x}" for w in build_me_initialize(chip.family)))
  else:
    ib = PM4Builder().build_dispatch_ib(0xAB0000, 0x1000, 0x2000, 0x3000)
    for i, w in enumerate(ib):
      print(f"  {i:04d}: {w:08x}")

def probe(chip: ChipInfo | None, wait_s: float = 0.0):
  print(diagnose_host(), flush=True)
  try:
    dev = TerrascaleDevice(chip=chip, wait_s=wait_s)
    info = dev.probe()
  except Exception as e:
    print(f"probe failed: {e}", file=sys.stderr)
    sys.exit(1)
  print(f"pci={info['vendor']:04x}:{info['device']:04x} rev={info['rev']:#04x} "
        f"chip={info['chip']} family={info['family']} id_match={info['id_match']}")
  print(f"GRBM_STATUS={info['grbm_status']:#x} CP_ME_CNTL={info['cp_me_cntl']:#x} "
        f"CONFIG_MEMSIZE={info['config_memsize']:#x}")
  print(f"bars={{ {', '.join(f'{k}:({hex(v[0])},{hex(v[1])})' for k,v in info.get('bars',{}).items())} }}")

def parse_wait(argv: list[str]) -> float:
  for i, arg in enumerate(argv):
    if arg.startswith("--wait="):
      return float(arg.split("=", 1)[1])
    if arg == "--wait" and i + 1 < len(argv):
      return float(argv[i + 1])
  return float(os.environ.get("TS_WAIT_S", "0"))

def parse_vec4(s: str) -> tuple[float, float, float, float]:
  parts = [float(x) for x in s.replace(" ", "").split(",") if x]
  if len(parts) != 4:
    raise SystemExit(f"need 4 floats, got {parts!r}")
  return (parts[0], parts[1], parts[2], parts[3])

def parse_cases(argv: list[str]) -> list[tuple[tuple[float, ...], tuple[float, ...]]]:
  if "--test" in argv:
    return [
      ((1.0, 2.0, 3.0, 4.0), (10.0, 20.0, 30.0, 40.0)),
      ((0.0, 0.0, 0.0, 0.0), (5.0, 5.0, 5.0, 5.0)),
      ((-1.0, 2.0, -3.0, 4.0), (2.0, -2.0, 2.0, -2.0)),
    ]
  a, b = (1.0, 2.0, 3.0, 4.0), (10.0, 20.0, 30.0, 40.0)
  for i, arg in enumerate(argv):
    if arg == "--a" and i + 1 < len(argv):
      a = parse_vec4(argv[i + 1])
    elif arg.startswith("--a="):
      a = parse_vec4(arg.split("=", 1)[1])
    elif arg == "--b" and i + 1 < len(argv):
      b = parse_vec4(argv[i + 1])
    elif arg.startswith("--b="):
      b = parse_vec4(arg.split("=", 1)[1])
  return [(a, b)]

def main():
  argv = sys.argv[1:]
  if any(a in ("--chip=auto", "--auto") for a in argv) or os.environ.get("TS_CHIP", "").lower() == "auto":
    chip: ChipInfo | None = None
  else:
    # default hd4850 when connected; still allow --chip=
    if not any(a.startswith("--chip") for a in argv) and not os.environ.get("TS_CHIP"):
      os.environ.setdefault("TS_CHIP", "hd4850")
    chip = resolve_chip(argv)
  wait_s = parse_wait(argv)

  if "--selftest" in argv:
    selftest(chip or CHIPS["hd4850"]); return
  if "--dry-run" in argv:
    dry_run(chip or CHIPS["hd4850"]); return
  if "--host-pci" in argv:
    print(diagnose_host())
    for n, v, d in host_pci_scan():
      print(f"  {n} {v:04x}:{d:04x}")
    return
  if "--probe" in argv:
    probe(chip, wait_s=wait_s); return
  if "--dump-rom" in argv:
    dev = TerrascaleDevice(chip=chip, wait_s=wait_s)
    out = DEFAULT_VBIOS
    for i, a in enumerate(argv):
      if a.startswith("--out="):
        out = pathlib.Path(a.split("=", 1)[1])
      elif a == "--out" and i + 1 < len(argv):
        out = pathlib.Path(argv[i + 1])
    bios = dev.dump_vbios_rom(out)
    print(f"ssid@0x7c={bios[0x7c:0x80].hex()} ATOM={b'ATOM' in bios}")
    return
  if "--atom" in argv:
    dev = TerrascaleDevice(chip=chip, wait_s=wait_s)
    dev.atom_asic_init()
    print(f"BAR0_ok={dev.probe_bar0()} BIF={dev.rreg(R600_BIF_FB_EN):#x} "
          f"FB={dev.rreg(REG_MC_VM_FB_LOCATION):#x} "
          f"SPLL={dev.rreg(REG_CG_SPLL_STATUS):#x} "
          f"MCLK={dev.rreg(0x648):#x}")
    return
  if "--clock-probe" in argv:
    dev = TerrascaleDevice(chip=chip, wait_s=wait_s)
    info = dev.prepare_spll_refclk()
    print("clock-probe", info)
    print(f"MCLK={dev.rreg(0x648):#x} MPLL_AD={dev.rreg(0x624):#x} "
          f"GENERAL={dev.rreg(0x63c):#x} GRBM={dev.rreg(REG_GRBM_STATUS):#x}")
    return
  if "--list-chips" in argv:
    for k, c in CHIPS.items():
      print(f"{k}: {c.name} family={c.family} ts={c.terrascale} "
            f"ls_compute={c.has_ls_compute} ids={[f'{x:04x}' for x in c.pci_ids]}")
    return
  if "--ring-test" in argv:
    dev = TerrascaleDevice(chip=chip, wait_s=wait_s)
    dev.boot()
    return

  # Default: boot + vector-add
  cases = parse_cases(argv)
  try:
    dev = TerrascaleDevice(chip=chip, wait_s=wait_s)
    print(f"pci={dev.vid:04x}:{dev.did:04x} chip={dev.chip.name}", flush=True)
    for a, b in cases:
      dev.run_add(a, b)
  except Exception as e:
    print(f"add failed: {e}", file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
  main()
