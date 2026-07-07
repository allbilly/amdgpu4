#!/usr/bin/env python3
"""Find first write of a suspicious data_block, and trace SETDATABLOCK results."""
import os, sys, collections
os.environ.setdefault("AMD_BOOT_VBIOS_FILE", "/tmp/rx570.rom")
os.environ.setdefault("AMD_ATOM_QUIET", "1")
os.environ.setdefault("AMD_ATOM_MC_POLL_RETRIES", "1")
os.environ.setdefault("AMD_ATOM_JUMP_MAX", "20000")
os.environ.setdefault("AMD_ATOM_JUMP_TIMEOUT_SEC", "600")

from add import PolarisDevice
from polaris_boot import PolarisBoot
import atom_replay as ar
from atom_replay import (read_vbios_rom, parse_atom_context, AtomCard, AtomExecutor,
  clear_asic_init_scratch, _u16, _u32, _u8, ATOM_DATA_FWI_PTR, ATOM_CMD_INIT,
  ATOM_FWI_DEFSCLK_PTR, ATOM_FWI_DEFMCLK_PTR)

trace = []
last_dblk = [0]
op_put = AtomExecutor._put_dst
def put2(self, arg, attr, ptr, val, saved):
  r = op_put(self, arg, attr, ptr, val, saved)
  db = self.ctx.data_block
  if db != last_dblk[0]:
    trace.append(f"put@{ptr[0]:#x} arg={arg} -> data_block {last_dblk[0]:#x} => {db:#x}")
    last_dblk[0] = db
  return r
AtomExecutor._put_dst = put2

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
  # print distinct data_block transitions (dedup consecutive loop spam)
  seen = []
  for t in trace:
    if not seen or seen[-1] != t:
      seen.append(t)
  for t in seen[-40:]:
    print(t, flush=True)

if __name__ == "__main__":
  main()
