#!/usr/bin/env python3
"""Minimal ATOM disassembler around a code range to inspect a poll loop."""
import os, struct, sys
os.environ.setdefault("AMD_BOOT_VBIOS_FILE", "/tmp/rx570.rom")
bios = open("/tmp/rx570.rom","rb").read()

def u8(o): return bios[o]
def u16(o): return struct.unpack_from("<H", bios, o)[0]

# opcode names (linux atom-names.h order, index=opcode)
NAMES = {
 0:"NOP",55:"SETPORT_MM/IIO",56:"SETPORT_PCI",57:"SETPORT_SYSIO",58:"SETREGBLOCK",
 59:"SETFBBASE",66:"SWITCH",
 67:"JMP_ALWAYS",68:"JMP_EQUAL",69:"JMP_BELOW",70:"JMP_ABOVE",71:"JMP_BELOWEQ",72:"JMP_ABOVEEQ",73:"JMP_NOTEQ",
 80:"DELAY_MS",81:"DELAY_US",82:"CALLTABLE",83:"REPEAT",90:"NOP2",91:"EOT",
 98:"POSTCARD",99:"BEEP",102:"SETDATABLOCK",121:"DEBUG",122:"PROCESSDS",
}
def cls(op):
  if 1<=op<=6: return f"MOVE_{op}"
  if 7<=op<=12: return f"AND_{op-7}"
  if 13<=op<=18: return f"OR_{op-13}"
  if 19<=op<=24: return f"SHL_{op-19}"
  if 25<=op<=30: return f"SHR_{op-25}"
  if 31<=op<=36: return f"MUL_{op-31}"
  if 37<=op<=42: return f"DIV_{op-37}"
  if 43<=op<=48: return f"ADD_{op-43}"
  if 49<=op<=54: return f"SUB_{op-49}"
  if 60<=op<=65: return f"CMP_{op-60}"
  if 74<=op<=79: return f"TEST_{op-74}"
  if 84<=op<=89: return f"CLEAR_{op-84}"
  if 92<=op<=97: return f"MASK_{op-92}"
  if 103<=op<=108: return f"XOR_{op-103}"
  if 109<=op<=114: return f"SHL2_{op-109}"
  if 115<=op<=120: return f"SHR2_{op-115}"
  if 123<=op<=126: return f"MULDIV32_{op-123}"
  return NAMES.get(op, f"OP{op}")

start = int(os.environ.get("START","0xd2b0"),0)
end = int(os.environ.get("END","0xd320"),0)
o = start
while o < end:
  op = u8(o)
  extra = ""
  if 67 <= op <= 73:
    extra = f" -> target(rel base)+{u16(o+1):#x}"
  elif op in (55,58):
    extra = f" val={u16(o+1):#x}"
  print(f"{o:#06x}: op={op:3d} {cls(op):14s}{extra}  bytes={bios[o:o+6].hex()}")
  # naive advance: we can't fully decode; step 1 to show raw stream
  o += 1
