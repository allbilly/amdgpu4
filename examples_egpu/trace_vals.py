#!/usr/bin/env python3
"""Track data_block / divmul / shift at the 0xd2e8 loop compare + what sets them."""
import os, sys, collections
os.environ.setdefault("AMD_BOOT_VBIOS_FILE", "/tmp/rx570.rom")
os.environ.setdefault("AMD_ATOM_QUIET", "1")
os.environ.setdefault("AMD_ATOM_MC_POLL_RETRIES", "1")
os.environ.setdefault("AMD_ATOM_JUMP_MAX", "6")
os.environ.setdefault("AMD_ATOM_JUMP_TIMEOUT_SEC", "600")

from add import PolarisDevice
from polaris_boot import PolarisBoot
from atom_replay import (read_vbios_rom, parse_atom_context, AtomCard, AtomExecutor,
  clear_asic_init_scratch, _u16, _u32, _u8, ATOM_DATA_FWI_PTR, ATOM_CMD_INIT,
  ATOM_FWI_DEFSCLK_PTR, ATOM_FWI_DEFMCLK_PTR)

events = []
oc = AtomExecutor._op_compare
def c2(self, arg, ptr):
  p = ptr[0]; oc(self, arg, ptr); g = self.ctx
  if p == 0xd2e8:
    events.append(f"CMP@d2e8 data_block={g.data_block:#x} remainder={g.divmul[1]:#x} "
                  f"quot={g.divmul[0]:#x} shift={g.shift:#x} eq={g.cs_equal}")
AtomExecutor._op_compare = c2

od = AtomExecutor._op_div
def d2(self, arg, ptr):
  od(self, arg, ptr)
  events.append(f"  DIV -> quot={self.ctx.divmul[0]:#x} rem={self.ctx.divmul[1]:#x}")
AtomExecutor._op_div = d2

def main():
  dev = PolarisDevice(); boot = PolarisBoot(dev)
  boot.vi_common_init(); boot.enable_vbios_rom()
  bios = read_vbios_rom(boot); clear_asic_init_scratch(boot)
  ctx = parse_atom_context(bios); card = AtomCard(boot, debug=False)
  exe = AtomExecutor(ctx, card)
  hwi = _u16(bios, ctx.data_table + ATOM_DATA_FWI_PTR)
  ps=[0]*16; ps[0]=_u32(bios,hwi+ATOM_FWI_DEFSCLK_PTR); ps[1]=_u32(bios,hwi+ATOM_FWI_DEFMCLK_PTR)
  try:
    exe.execute_table(ATOM_CMD_INIT, ps, 16); print("DONE", flush=True)
  except Exception as e:
    print(f"STOP: {e}", flush=True)
  for e in events[-40:]:
    print(e, flush=True)

if __name__ == "__main__":
  main()
