#!/usr/bin/env python3
"""Full op-by-op trace of the d2e8 loop with special-reg state, 3 iterations."""
import os, sys, collections
os.environ.setdefault("AMD_BOOT_VBIOS_FILE", "/tmp/rx570.rom")
os.environ.setdefault("AMD_ATOM_QUIET", "1")
os.environ.setdefault("AMD_ATOM_MC_POLL_RETRIES", "1")
os.environ.setdefault("AMD_ATOM_JUMP_MAX", "4")
os.environ.setdefault("AMD_ATOM_JUMP_TIMEOUT_SEC", "600")

from add import PolarisDevice
from polaris_boot import PolarisBoot
from atom_replay import (read_vbios_rom, parse_atom_context, AtomCard, AtomExecutor,
  clear_asic_init_scratch, _u16, _u32, _u8, ATOM_DATA_FWI_PTR, ATOM_CMD_INIT,
  ATOM_FWI_DEFSCLK_PTR, ATOM_FWI_DEFMCLK_PTR)

ring = collections.deque(maxlen=200)
LO, HI = 0xd2e0, 0xd370

def hook(name):
  f = getattr(AtomExecutor, name)
  def g(self, *a, **k):
    ptr = a[-1]; p = ptr[0] - 1  # opcode addr
    r = f(self, *a, **k)
    if LO <= p <= HI:
      gg = self.ctx
      ring.append(f"{p:#06x} {name:12s} dblk={gg.data_block:#x} q={gg.divmul[0]:#x} "
                  f"rem={gg.divmul[1]:#x} sh={gg.shift:#x} eq={gg.cs_equal} ab={gg.cs_above}")
    return r
  setattr(AtomExecutor, name, g)

for n in ["_op_move","_op_bin","_op_compare","_op_test","_op_shift","_op_mask","_op_clear","_op_mul","_op_div","_op_switch"]:
  hook(n)

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
  for e in ring:
    print(e, flush=True)

if __name__ == "__main__":
  main()
