#!/usr/bin/env python3
"""Log every ATOM op (addr+opcode) for the last N executed, to see loop structure."""
import os, sys, time, collections
os.environ.setdefault("AMD_BOOT_VBIOS_FILE", "/tmp/rx570.rom")
os.environ.setdefault("AMD_ATOM_QUIET", "1")
os.environ.setdefault("AMD_ATOM_MC_POLL_RETRIES", "1")
os.environ.setdefault("AMD_ATOM_JUMP_MAX", "20")
os.environ.setdefault("AMD_ATOM_JUMP_TIMEOUT_SEC", "600")
os.environ["AMD_ATOM_TRACE"] = ""  # we do our own

from add import PolarisDevice
from polaris_boot import PolarisBoot
import atom_replay as ar
from atom_replay import (read_vbios_rom, parse_atom_context, AtomCard, AtomExecutor,
  clear_asic_init_scratch, _u16, _u32, _u8, ATOM_DATA_FWI_PTR, ATOM_CMD_INIT,
  ATOM_FWI_DEFSCLK_PTR, ATOM_FWI_DEFMCLK_PTR)

ring = collections.deque(maxlen=60)

# Patch _execute_locked's op fetch by wrapping the whole method is hard; instead
# monkeypatch _u8 used for opcode? Too broad. Instead wrap key ops with addr.
# Simplest: patch AtomExecutor to log via a hook we insert into the op dispatch.
# We wrap _op_compare, _op_bin(add/sub), _op_move, and jumps by tracking ptr.

orig = {}
def wrap(name):
  f = getattr(AtomExecutor, name)
  def g(self, *a, **k):
    ptr = a[-1]
    ring.append((name, ptr[0]))
    return f(self, *a, **k)
  setattr(AtomExecutor, name, g)

for n in ["_op_move","_op_bin","_op_compare","_op_test","_op_shift","_op_mask","_op_clear","_op_shl_shr","_op_mul","_op_div"]:
  wrap(n)

def main():
  dev = PolarisDevice()
  boot = PolarisBoot(dev)
  boot.vi_common_init(); boot.enable_vbios_rom()
  bios = read_vbios_rom(boot)
  clear_asic_init_scratch(boot)
  ctx = parse_atom_context(bios)
  card = AtomCard(boot, debug=False)
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
  print("\n=== last ops (name @ operand-ptr) ===", flush=True)
  for name, p in ring:
    print(f"  {p:#06x} {name}", flush=True)
  # Also dump raw bytes of the loop
  print("\n=== raw bytes 0xd2e0..0xd320 ===", flush=True)
  print(bios[0xd2e0:0xd320].hex(), flush=True)

if __name__ == "__main__":
  main()
