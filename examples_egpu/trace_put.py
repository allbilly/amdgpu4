#!/usr/bin/env python3
import os, sys, collections
os.environ.setdefault("AMD_BOOT_VBIOS_FILE", "/tmp/rx570.rom")
os.environ.setdefault("AMD_ATOM_QUIET", "1")
os.environ.setdefault("AMD_ATOM_MC_POLL_RETRIES", "1")
os.environ.setdefault("AMD_ATOM_JUMP_MAX", "20000")
os.environ.setdefault("AMD_ATOM_JUMP_TIMEOUT_SEC", "600")

from add import PolarisDevice
from polaris_boot import PolarisBoot
from atom_replay import (read_vbios_rom, parse_atom_context, AtomCard, AtomExecutor,
  clear_asic_init_scratch, _u16, _u32, _u8, ATOM_DATA_FWI_PTR, ATOM_CMD_INIT,
  ATOM_FWI_DEFSCLK_PTR, ATOM_FWI_DEFMCLK_PTR)

ring = collections.deque(maxlen=30)

ob = AtomExecutor._op_bin
def b2(self, fn, arg, ptr):
  p = ptr[0]-1
  if 0xd2e8 <= p <= 0xd2fa:
    attr = _u8(self.ctx.bios, ptr[0])
    dv = self._get_dst(arg, attr, [ptr[0]], [0])
    sv = None
    ring.append(f"BIN@{p:#x} arg={arg} attr={attr:#x} dst_before={dv:#x} fn(dst,?)")
  return ob(self, fn, arg, ptr)
AtomExecutor._op_bin = b2

op = AtomExecutor._put_dst
def p2(self, arg, attr, ptr, val, saved):
  pp = ptr[0]
  r = op(self, arg, attr, ptr, val, saved)
  if arg == 2 and 0xd2f0 <= pp <= 0xd2fa:  # WS writes in loop
    idx = _u8(self.ctx.bios, pp)
    ring.append(f"PUT@{pp:#x} WS[{idx:#x}] <- val={val:#x} (data_block now {self.ctx.data_block:#x} rem {self.ctx.divmul[1]:#x})")
  return r
AtomExecutor._put_dst = p2

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
