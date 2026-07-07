#!/usr/bin/env python3
"""Trace SETDATABLOCK / DIV32 / MUL32 / DIV leading into the d2e8 loop."""
import os, sys, collections
os.environ.setdefault("AMD_BOOT_VBIOS_FILE", "/tmp/rx570.rom")
os.environ.setdefault("AMD_ATOM_QUIET", "1")
os.environ.setdefault("AMD_ATOM_MC_POLL_RETRIES", "1")
os.environ.setdefault("AMD_ATOM_JUMP_MAX", "3")
os.environ.setdefault("AMD_ATOM_JUMP_TIMEOUT_SEC", "600")

from add import PolarisDevice
from polaris_boot import PolarisBoot
import atom_replay as ar
from atom_replay import (read_vbios_rom, parse_atom_context, AtomCard, AtomExecutor,
  clear_asic_init_scratch, _u16, _u32, _u8, ATOM_DATA_FWI_PTR, ATOM_CMD_INIT,
  ATOM_FWI_DEFSCLK_PTR, ATOM_FWI_DEFMCLK_PTR)

ring = collections.deque(maxlen=60)

# instrument SETDATABLOCK via op 102 — patch _execute_locked is hard; hook divmul ops
for opn, tag in [("_op_div","DIV"),("_op_mul","MUL"),("_op_div32","DIV32"),("_op_mul32","MUL32")]:
  f = getattr(AtomExecutor, opn)
  def mk(f, tag):
    def g(self, *a, **k):
      p = a[-1][0]-1
      r = f(self, *a, **k)
      ring.append(f"{p:#06x} {tag} -> q={self.ctx.divmul[0]:#x} rem={self.ctx.divmul[1]:#x}")
      return r
    return g
  setattr(AtomExecutor, opn, mk(f, tag))

oc = AtomExecutor._op_compare
def c2(self, arg, ptr):
  p = ptr[0]-1; oc(self, arg, ptr)
  if p == 0xd2e8:
    ring.append(f"{p:#06x} CMP data_block={self.ctx.data_block:#x} rem={self.ctx.divmul[1]:#x} eq={self.ctx.cs_equal}")
AtomExecutor._op_compare = c2

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
  for e in ring: print(e, flush=True)

if __name__ == "__main__":
  main()
