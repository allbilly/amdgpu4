#!/usr/bin/env python3
"""Log the op stream of one iteration of the 0xd2e8 loop with compare outcomes."""
import os, sys, time
os.environ.setdefault("AMD_BOOT_VBIOS_FILE", "/tmp/rx570.rom")
os.environ.setdefault("AMD_ATOM_QUIET", "1")
os.environ.setdefault("AMD_ATOM_MC_POLL_RETRIES", "1")
os.environ.setdefault("AMD_ATOM_JUMP_MAX", "40")
os.environ.setdefault("AMD_ATOM_JUMP_TIMEOUT_SEC", "600")

from add import PolarisDevice
from polaris_boot import PolarisBoot
import atom_replay as ar
from atom_replay import (read_vbios_rom, parse_atom_context, AtomCard, AtomExecutor,
  clear_asic_init_scratch, _u16, _u32, _u8, ATOM_DATA_FWI_PTR, ATOM_CMD_INIT,
  ATOM_FWI_DEFSCLK_PTR, ATOM_FWI_DEFMCLK_PTR)

LO = int(os.environ.get("LO","0xd2e0"),0)
HI = int(os.environ.get("HI","0xd340"),0)

log = []
class BCard(AtomCard):
  def reg_read(self, reg):
    v = super().reg_read(reg)
    log.append(("R", reg & 0xffff, v))
    return v
  def reg_write(self, reg, val):
    log.append(("W", reg & 0xffff, val))
    return super().reg_write(reg, val)

# wrap compare + jump to record decisions with ptr
orig_cmp = AtomExecutor._op_compare
def cmp2(self, arg, ptr):
  p = ptr[0]
  orig_cmp(self, arg, ptr)
  if LO <= p <= HI:
    log.append(("CMP@%#x"%p, self.ctx.cs_equal, self.ctx.cs_above))
AtomExecutor._op_compare = cmp2

orig_test = AtomExecutor._op_test
def test2(self, arg, ptr):
  p = ptr[0]
  orig_test(self, arg, ptr)
  if LO <= p <= HI:
    log.append(("TEST@%#x"%p, self.ctx.cs_equal, None))
AtomExecutor._op_test = test2

def main():
  dev = PolarisDevice()
  boot = PolarisBoot(dev)
  boot.vi_common_init(); boot.enable_vbios_rom()
  bios = read_vbios_rom(boot)
  clear_asic_init_scratch(boot)
  ctx = parse_atom_context(bios)
  card = BCard(boot, debug=False)
  exe = AtomExecutor(ctx, card)
  hwi = _u16(bios, ctx.data_table + ATOM_DATA_FWI_PTR)
  ps = [0]*16
  ps[0] = _u32(bios, hwi + ATOM_FWI_DEFSCLK_PTR)
  ps[1] = _u32(bios, hwi + ATOM_FWI_DEFMCLK_PTR)
  try:
    exe.execute_table(ATOM_CMD_INIT, ps, 16)
    print("DONE", flush=True)
  except Exception as e:
    print(f"STOP: {e}", flush=True)
  print(f"\n=== last {min(len(log),80)} events ===", flush=True)
  for ev in log[-80:]:
    print("  ", ev, flush=True)

if __name__ == "__main__":
  main()
