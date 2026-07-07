#!/usr/bin/env python3
"""Faithful ATOM bytecode disassembler (operand lengths per amdgpu atom.c)."""
import os, struct, sys
bios = open(os.environ.get("ROM","/tmp/rx570.rom"),"rb").read()

def u8(o): return bios[o]
def u16(o): return struct.unpack_from("<H", bios, o)[0]
def u32(o): return struct.unpack_from("<I", bios, o)[0]

ATOM_SRC_DWORD=0
ARG_REG,ARG_PS,ARG_WS,ARG_FB,ARG_ID,ARG_IMM,ARG_PLL,ARG_MC=0,1,2,3,4,5,6,7
DST_TO_SRC=((0,0,0,0),(1,2,3,0),(1,2,3,0),(1,2,3,0),(4,5,6,7),(4,5,6,7),(4,5,6,7),(4,5,6,7))
DEF_DST=(0,0,1,2,0,1,2,3)
ARGN=["REG","PS","WS","FB","ID","IMM","PLL","MC"]
ALIGN=["DW","W0","W8","W16","B0","B8","B16","B24"]

def src_len(attr):
  align, arg = (attr>>3)&7, attr&7
  if arg in (ARG_REG,ARG_ID): return 2
  if arg in (ARG_PLL,ARG_MC,ARG_PS,ARG_WS,ARG_FB): return 1
  if arg==ARG_IMM:
    return 4 if align==0 else 2 if align<=3 else 1
  return 1

def src_str(attr, o):
  align, arg = (attr>>3)&7, attr&7
  if arg==ARG_IMM:
    if align==0: v=u32(o); n=4
    elif align<=3: v=u16(o); n=2
    else: v=u8(o); n=1
    return f"IMM:{v:#x}", n
  if arg in (ARG_REG,ARG_ID):
    return f"{ARGN[arg]}[{u16(o):#x}]", 2
  return f"{ARGN[arg]}[{u8(o):#x}]", 1

def dst_attr_len(attr):
  full = (attr&7)|(DST_TO_SRC[(attr>>3)&7][(attr>>6)&3]<<3)
  return src_len(full)
def dst_str(attr,o):
  arg=attr&7
  align=DST_TO_SRC[(attr>>3)&7][(attr>>6)&3]
  if arg in (ARG_REG,ARG_ID):
    return f"{ARGN[arg]}[{u16(o):#x}].{ALIGN[align]}",2
  return f"{ARGN[arg]}[{u8(o):#x}].{ALIGN[align]}",1

JMP={67:"ALWAYS",68:"EQUAL",69:"BELOW",70:"ABOVE",71:"BELOWEQ",72:"ABOVEEQ",73:"NOTEQ"}

def dst_src_op(name,o):
  attr=u8(o+1); p=o+2
  d,dn=dst_str(attr,p); p+=dst_attr_len(attr)
  s,sn=src_str(attr,p); p+=src_len(attr)
  return f"{name} {d}, {s}", p

def dst_only_op(name,o,defdst=False):
  attr=u8(o+1)
  if defdst:
    attr=(attr&0x38)|(DEF_DST[(attr>>3)&7]<<6)
  p=o+2
  d,dn=dst_str(attr,p); p+=dst_attr_len(attr)
  return f"{name} {d}", p

def dis(o):
  op=u8(o)
  if 1<=op<=6: return dst_src_op(f"MOVE",o)
  if 7<=op<=12: return dst_src_op("AND",o)
  if 13<=op<=18: return dst_src_op("OR",o)
  if 19<=op<=24:  # SHIFT_LEFT (shift is 1 byte)
    attr=u8(o+1); attr=(attr&0x38)|(DEF_DST[(attr>>3)&7]<<6); p=o+2
    d,_=dst_str(attr,p); p+=dst_attr_len(attr); sh=u8(p); p+=1
    return f"SHL {d}, {sh}", p
  if 25<=op<=30:
    attr=u8(o+1); attr=(attr&0x38)|(DEF_DST[(attr>>3)&7]<<6); p=o+2
    d,_=dst_str(attr,p); p+=dst_attr_len(attr); sh=u8(p); p+=1
    return f"SHR {d}, {sh}", p
  if 31<=op<=36: return dst_src_op("MUL",o)
  if 37<=op<=42: return dst_src_op("DIV",o)
  if 43<=op<=48: return dst_src_op("ADD",o)
  if 49<=op<=54: return dst_src_op("SUB",o)
  if 60<=op<=65: return dst_src_op("CMP",o)
  if 74<=op<=79: return dst_src_op("TEST",o)
  if 84<=op<=89: return dst_only_op("CLEAR",o,defdst=True)
  if 92<=op<=97:  # MASK: dst, mask, src
    attr=u8(o+1); p=o+2
    d,_=dst_str(attr,p); p+=dst_attr_len(attr)
    m,_=src_str(attr,p); p+=src_len(attr)
    s,_=src_str(attr,p); p+=src_len(attr)
    return f"MASK {d}, {m}, {s}", p
  if op==55:
    port=u16(o+1); return f"SETPORT {'MM' if port==0 else f'IIO:{port:#x}'}", o+3
  if op==58:
    return f"SETREGBLOCK {u16(o+1):#x}", o+3
  if op==59:
    attr=u8(o+1); p=o+2; s,_=src_str(attr,p); p+=src_len(attr)
    return f"SETFBBASE {s}", p
  if 67<=op<=73:
    return f"JMP_{JMP[op]} -> base+{u16(o+1):#x}", o+3
  if op==66:  # SWITCH - variable
    attr=u8(o+1); p=o+2; s,_=src_str(attr,p); p+=src_len(attr)
    out=f"SWITCH {s} {{"; 
    while u16(p)!=0x5A5A:
      if u8(p)==0x63:
        p+=1
        # case value src with align of attr + IMM
        a=(attr&0x38)|ARG_IMM
        cv,cn=src_str(a,p); p+=src_len(a)
        tgt=u16(p); p+=2
        out+=f" case {cv}->+{tgt:#x};"
      else:
        break
    p+=2
    return out+" }", p
  if op==80: return f"DELAY_MS {u8(o+1)}", o+2
  if op==81: return f"DELAY_US {u8(o+1)}", o+2
  if op==82: return f"CALLTABLE {u8(o+1)}", o+2
  if op==91: return "EOT", o+1
  if op==90: return "NOP", o+1
  if op==102: return f"SETDATABLOCK {u8(o+1)}", o+2
  return f"OP{op}?", o+1

def main():
  start=int(os.environ.get("START","0xdf70"),0)
  end=int(os.environ.get("END","0xe010"),0)
  o=start
  while o<end:
    try:
      s,nxt=dis(o)
    except Exception as e:
      s,nxt=f"<err {e}>",o+1
    print(f"{o:#06x}: {s}")
    o=nxt

if __name__=="__main__":
  main()
