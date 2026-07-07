#!/usr/bin/env python3
"""After ATOM training, test BAR0 + MM_INDEX VRAM read/write paths thoroughly."""
import os, struct, time
os.environ.setdefault("AMD_BOOT_VBIOS_FILE", "/tmp/rx570.rom")

from add import PolarisDevice
from polaris_boot import PolarisBoot, mmBIF_FB_EN, mmBIF_MM_INDACCESS_CNTL, mmMM_INDEX, mmMM_INDEX_HI, mmMM_DATA
from atom_replay import run_asic_init_if_needed, vram_training_ok

def main():
  dev = PolarisDevice()
  b = PolarisBoot(dev)
  b.vi_common_init(); b.enable_vbios_rom()
  run_asic_init_if_needed(b)
  print(f"trained={vram_training_ok(b)} MEMSIZE={b.config_memsize_mb()} MISC0={b.rreg(0xa80):#x}", flush=True)
  b.gmc_sw_init()
  b.mc_program()
  print(f"FB_LOC={b.rreg(0x809):#x} BIF_FB_EN={b.rreg(mmBIF_FB_EN):#x} "
        f"vram_start={b.vram_start:#x} visible_mc={b.vram_visible_mc:#x} bar0={dev.bar0_size:#x}", flush=True)

  # --- BAR0 direct test at several offsets ---
  print("\n=== BAR0 aperture (dev.vram) ===", flush=True)
  for off in (0x1000, 0x2000, 0x10000, 0x100000):
    pat = (0xA5000000 | off) & 0xffffffff
    try:
      dev.vram[off:off+4] = struct.pack('<I', pat)
      b.hdp_flush()
      got = struct.unpack('<I', bytes(dev.vram[off:off+4]))[0]
      print(f"  off={off:#08x} wrote={pat:#010x} read={got:#010x} {'OK' if got==pat else 'FAIL'}", flush=True)
    except Exception as e:
      print(f"  off={off:#08x} EXC {e}", flush=True)

  # --- MM_INDEX test with explicit BIF setup ---
  print("\n=== MM_INDEX path ===", flush=True)
  b.wreg(mmBIF_FB_EN, 0x3)
  b.wreg(mmBIF_MM_INDACCESS_CNTL, 0)
  for pos in (0x1000, 0x4000, 0x40000):
    pat = (0x5A000000 | pos) & 0xffffffff
    b.wreg(mmMM_INDEX, (pos & 0x7fffffff) | 0x80000000)
    b.wreg(mmMM_INDEX_HI, pos >> 31)
    b.wreg(mmMM_DATA, pat)
    b.hdp_flush()
    b.wreg(mmMM_INDEX, (pos & 0x7fffffff) | 0x80000000)
    b.wreg(mmMM_INDEX_HI, pos >> 31)
    got = b.rreg(mmMM_DATA)
    print(f"  pos={pos:#08x} wrote={pat:#010x} read={got:#010x} {'OK' if got==pat else 'FAIL'}", flush=True)

if __name__ == "__main__":
  main()
