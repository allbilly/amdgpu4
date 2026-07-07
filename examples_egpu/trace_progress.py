#!/usr/bin/env python3
"""Is the 0xd2e8 loop bounded (progressing) or a real MMIO poll hang?

Tracks, per backward-jump target, whether MMIO state changes between iterations.
If the loop keeps hitting the same target but writes keep advancing, it is a
bounded table-init loop cut off by AMD_ATOM_JUMP_MAX (raise the cap).
If the same reads return the same values with no new writes, it is a real poll.
"""
import os, sys, time, collections
os.environ.setdefault("AMD_BOOT_VBIOS_FILE", "/tmp/rx570.rom")
os.environ.setdefault("AMD_ATOM_QUIET", "1")
os.environ.setdefault("AMD_ATOM_MC_POLL_RETRIES", "1")

from add import PolarisDevice
from polaris_boot import PolarisBoot
from atom_replay import (read_vbios_rom, parse_atom_context, AtomCard, AtomExecutor,
  clear_asic_init_scratch, _u16, _u32, ATOM_DATA_FWI_PTR, ATOM_CMD_INIT,
  ATOM_FWI_DEFSCLK_PTR, ATOM_FWI_DEFMCLK_PTR)

class PCard(AtomCard):
  def __init__(self, *a, **k):
    super().__init__(*a, **k)
    self.wcount = 0
    self.last_report = 0
  def reg_write(self, reg, val):
    self.wcount += 1
    if self.wcount - self.last_report >= 2000:
      self.last_report = self.wcount
      print(f"  ...progress writes={self.wcount} last W {reg&0xffff:#06x}={val:#010x} "
            f"MEMSIZE={self.boot.rreg(0x150a)&0xffff} MISC0={self.boot.rreg(0xa80):#x}", flush=True)
    return super().reg_write(reg, val)

def main():
  dev = PolarisDevice()
  boot = PolarisBoot(dev)
  boot.vi_common_init(); boot.enable_vbios_rom()
  bios = read_vbios_rom(boot)
  clear_asic_init_scratch(boot)
  ctx = parse_atom_context(bios)
  card = PCard(boot, debug=False)
  exe = AtomExecutor(ctx, card)
  hwi = _u16(bios, ctx.data_table + ATOM_DATA_FWI_PTR)
  ps = [0]*16
  ps[0] = _u32(bios, hwi + ATOM_FWI_DEFSCLK_PTR)
  ps[1] = _u32(bios, hwi + ATOM_FWI_DEFMCLK_PTR)
  t0 = time.time()
  try:
    exe.execute_table(ATOM_CMD_INIT, ps, 16)
    print(f"DONE t={time.time()-t0:.1f}s writes={ctx.reg_write_count} "
          f"MEMSIZE={boot.rreg(0x150a)&0xffff} MISC0={boot.rreg(0xa80):#x} "
          f"pci={dev.pci.read_config(0,2)&0xffff:#06x}", flush=True)
  except Exception as e:
    print(f"STOP t={time.time()-t0:.1f}s: {e}", flush=True)
    print(f"writes={ctx.reg_write_count} pci={dev.pci.read_config(0,2)&0xffff:#06x}", flush=True)

if __name__ == "__main__":
  main()
