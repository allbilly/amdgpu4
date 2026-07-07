#!/usr/bin/env python3
"""Capture the poll register(s) inside the 0xd2e8 loop and whether they change."""
import os, sys, time, collections
os.environ.setdefault("AMD_BOOT_VBIOS_FILE", "/tmp/rx570.rom")
os.environ.setdefault("AMD_ATOM_QUIET", "1")
os.environ.setdefault("AMD_ATOM_MC_POLL_RETRIES", "1")

from add import PolarisDevice
from polaris_boot import PolarisBoot
from atom_replay import (read_vbios_rom, parse_atom_context, AtomCard, AtomExecutor,
  clear_asic_init_scratch, _u16, _u32, ATOM_DATA_FWI_PTR, ATOM_CMD_INIT,
  ATOM_FWI_DEFSCLK_PTR, ATOM_FWI_DEFMCLK_PTR)

# reg -> set of distinct values seen; and ordered first/last
seen = collections.defaultdict(lambda: {"n":0, "vals":collections.Counter(), "first":None, "last":None})

class LCard(AtomCard):
  def reg_read(self, reg):
    v = super().reg_read(reg)
    r = reg & 0xffff
    s = seen[r]
    s["n"] += 1
    s["vals"][v] += 1
    if s["first"] is None: s["first"] = v
    s["last"] = v
    return v

def main():
  dev = PolarisDevice()
  boot = PolarisBoot(dev)
  boot.vi_common_init(); boot.enable_vbios_rom()
  bios = read_vbios_rom(boot)
  clear_asic_init_scratch(boot)
  ctx = parse_atom_context(bios)
  card = LCard(boot, debug=False)
  exe = AtomExecutor(ctx, card)
  hwi = _u16(bios, ctx.data_table + ATOM_DATA_FWI_PTR)
  ps = [0]*16
  ps[0] = _u32(bios, hwi + ATOM_FWI_DEFSCLK_PTR)
  ps[1] = _u32(bios, hwi + ATOM_FWI_DEFMCLK_PTR)
  try:
    exe.execute_table(ATOM_CMD_INIT, ps, 16)
    print(f"DONE MEMSIZE={boot.rreg(0x150a)&0xffff} MISC0={boot.rreg(0xa80):#x}", flush=True)
  except Exception as e:
    print(f"STOP: {e}", flush=True)
  # Registers read many times = the poll registers
  print("\n=== most-read regs (poll candidates) ===", flush=True)
  for r, s in sorted(seen.items(), key=lambda kv: -kv[1]["n"])[:15]:
    nd = len(s["vals"])
    ex = s["vals"].most_common(3)
    print(f"  reg {r:#06x} reads={s['n']} distinct={nd} first={s['first']:#x} last={s['last']:#x} top={[(hex(v),c) for v,c in ex]}", flush=True)

if __name__ == "__main__":
  main()
