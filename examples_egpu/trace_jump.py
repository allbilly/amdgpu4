#!/usr/bin/env python3
"""Instrument the backward jump at 0xd2e8: is exit a counter or MMIO poll?"""
import os, sys, time, collections
os.environ.setdefault("AMD_BOOT_VBIOS_FILE", "/tmp/rx570.rom")
os.environ.setdefault("AMD_ATOM_QUIET", "1")
os.environ.setdefault("AMD_ATOM_MC_POLL_RETRIES", "1")

from add import PolarisDevice
from polaris_boot import PolarisBoot
import atom_replay as ar
from atom_replay import (read_vbios_rom, parse_atom_context, AtomCard, AtomExecutor,
  clear_asic_init_scratch, _u16, _u32, ATOM_DATA_FWI_PTR, ATOM_CMD_INIT,
  ATOM_FWI_DEFSCLK_PTR, ATOM_FWI_DEFMCLK_PTR)

TARGET = int(os.environ.get("JT", "0xd2e8"), 0)

# Patch _execute_locked jump handling by monkey-instrumenting compare state.
orig_compare = AtomExecutor._op_compare
def traced_compare(self, arg, ptr):
  orig_compare(self, arg, ptr)
  self._last_cmp = (self.ctx.cs_equal, self.ctx.cs_above)
AtomExecutor._op_compare = traced_compare

jump_hits = collections.Counter()

def main():
  dev = PolarisDevice()
  boot = PolarisBoot(dev)
  boot.vi_common_init(); boot.enable_vbios_rom()
  bios = read_vbios_rom(boot)
  clear_asic_init_scratch(boot)
  ctx = parse_atom_context(bios)
  card = AtomCard(boot, debug=False)
  exe = AtomExecutor(ctx, card)

  # wrap jump target logging via patching card._jump_counts observation
  hwi = _u16(bios, ctx.data_table + ATOM_DATA_FWI_PTR)
  ps = [0]*16
  ps[0] = _u32(bios, hwi + ATOM_FWI_DEFSCLK_PTR)
  ps[1] = _u32(bios, hwi + ATOM_FWI_DEFMCLK_PTR)
  try:
    exe.execute_table(ATOM_CMD_INIT, ps, 16)
    print(f"DONE writes={ctx.reg_write_count} MEMSIZE={boot.rreg(0x150a)&0xffff} MISC0={boot.rreg(0xa80):#x}", flush=True)
  except Exception as e:
    print(f"STOP: {e}", flush=True)
  # dump jump counts
  jc = card._jump_counts
  tops = sorted(jc.items(), key=lambda kv: -kv[1])[:15]
  print("=== backward jump targets by iteration count ===", flush=True)
  for t, n in tops:
    print(f"  target={t:#06x} iters={n}", flush=True)

if __name__ == "__main__":
  main()
