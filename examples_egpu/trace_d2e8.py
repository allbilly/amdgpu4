#!/usr/bin/env python3
"""Trace the 0xd2e8 unconditional-jump loop: ops, MMIO, and WS[0x41]/WS[0x42]."""
import os, sys, collections
os.environ.setdefault("AMD_BOOT_VBIOS_FILE", "/tmp/rx570.rom")
os.environ.setdefault("AMD_ATOM_QUIET", "1")
os.environ.setdefault("AMD_ATOM_MC_POLL_RETRIES", "1")
os.environ.setdefault("AMD_ATOM_JUMP_MAX", "8")
os.environ.setdefault("AMD_ATOM_JUMP_TIMEOUT_SEC", "600")

from add import PolarisDevice
from polaris_boot import PolarisBoot
from atom_replay import (read_vbios_rom, parse_atom_context, AtomCard, AtomExecutor,
  clear_asic_init_scratch, _u16, _u32, _u8, ATOM_DATA_FWI_PTR, ATOM_CMD_INIT,
  ATOM_FWI_DEFSCLK_PTR, ATOM_FWI_DEFMCLK_PTR)

ring = collections.deque(maxlen=120)
class C(AtomCard):
  def reg_read(self, reg):
    v = super().reg_read(reg); ring.append(("R", reg & 0xffff, v)); return v
  def reg_write(self, reg, val):
    ring.append(("W", reg & 0xffff, val)); return super().reg_write(reg, val)

def main():
  dev = PolarisDevice(); boot = PolarisBoot(dev)
  boot.vi_common_init(); boot.enable_vbios_rom()
  bios = read_vbios_rom(boot); clear_asic_init_scratch(boot)
  ctx = parse_atom_context(bios); card = C(boot, debug=False)
  exe = AtomExecutor(ctx, card)
  # instrument compare to record WS special regs each time near loop
  oc = AtomExecutor._op_compare
  def c2(self, arg, ptr):
    p = ptr[0]
    oc(self, arg, ptr)
    if 0xd2e0 <= p <= 0xd310:
      g = self.ctx
      ring.append((f"CMP@{p:#x}", f"dblk={g.data_block:#x}", f"rem={g.divmul[1]:#x}",
                   f"eq={g.cs_equal}", f"ab={g.cs_above}"))
  AtomExecutor._op_compare = c2
  hwi = _u16(bios, ctx.data_table + ATOM_DATA_FWI_PTR)
  ps = [0]*16; ps[0]=_u32(bios,hwi+ATOM_FWI_DEFSCLK_PTR); ps[1]=_u32(bios,hwi+ATOM_FWI_DEFMCLK_PTR)
  try:
    exe.execute_table(ATOM_CMD_INIT, ps, 16); print("DONE", flush=True)
  except Exception as e:
    print(f"STOP: {e}", flush=True)
  for ev in ring:
    print("  ", ev, flush=True)

if __name__ == "__main__":
  main()
