#!/usr/bin/env python3
"""Find the ATOM asic_init stuck backward-jump loop and what it polls."""
import os, sys, time, collections
os.environ.setdefault("AMD_BOOT_VBIOS_FILE", "/tmp/rx570.rom")
os.environ.setdefault("AMD_ATOM_QUIET", "1")
os.environ.setdefault("AMD_ATOM_MC_POLL_RETRIES", "1")  # don't internally spin

from add import PolarisDevice
from polaris_boot import PolarisBoot
import atom_replay as ar
from atom_replay import (read_vbios_rom, parse_atom_context, AtomCard, AtomExecutor,
  clear_asic_init_scratch, _u16, _u32, ATOM_DATA_FWI_PTR, ATOM_CMD_INIT,
  ATOM_FWI_DEFSCLK_PTR, ATOM_FWI_DEFMCLK_PTR)

recent = collections.deque(maxlen=40)
reads = collections.Counter()
writes = collections.Counter()

class TCard(AtomCard):
  def reg_read(self, reg):
    v = super().reg_read(reg)
    reads[reg & 0xffff] += 1
    recent.append(("R", reg & 0xffff, v))
    return v
  def reg_write(self, reg, val):
    writes[reg & 0xffff] += 1
    recent.append(("W", reg & 0xffff, val))
    return super().reg_write(reg, val)

def main():
  dev = PolarisDevice()
  boot = PolarisBoot(dev)
  boot.vi_common_init(); boot.enable_vbios_rom()
  bios = read_vbios_rom(boot)
  clear_asic_init_scratch(boot)
  ctx = parse_atom_context(bios)
  card = TCard(boot, debug=False)
  exe = AtomExecutor(ctx, card)
  hwi = _u16(bios, ctx.data_table + ATOM_DATA_FWI_PTR)
  ps = [0]*16
  ps[0] = _u32(bios, hwi + ATOM_FWI_DEFSCLK_PTR)
  ps[1] = _u32(bios, hwi + ATOM_FWI_DEFMCLK_PTR)
  print(f"asic_init ps0={ps[0]:#x} ps1={ps[1]:#x}", flush=True)
  try:
    exe.execute_table(ATOM_CMD_INIT, ps, 16)
    print(f"DONE writes={ctx.reg_write_count} MEMSIZE={boot.rreg(0x150a)&0xffff} MISC0={boot.rreg(0xa80):#x}", flush=True)
  except Exception as e:
    print(f"STOP: {e}", flush=True)
    print(f"writes={ctx.reg_write_count} pci={dev.pci.read_config(0,2)&0xffff:#06x}", flush=True)
  print("\n=== recent 40 MMIO ops (loop body) ===", flush=True)
  for k, r, v in recent:
    print(f"  {k} {r:#06x} = {v:#010x}", flush=True)
  print("\n=== top read regs ===", flush=True)
  for r, n in reads.most_common(12):
    print(f"  {r:#06x}: {n}", flush=True)

if __name__ == "__main__":
  main()
