# HD 5570 / Redwood eGPU bring-up progress

Last updated: 2026-07-22

## Goal and honesty boundary

Run `add.py` on the attached `1002:68d9` Radeon HD 5570 and return a
four-lane FP32 result produced by Redwood hardware. CPU arithmetic and a CP
`MEM_WRITE` payload are diagnostics only and must never be reported as GPU
addition.

## Verified working

| Layer | Evidence |
|---|---|
| PCI/MMIO | `1002:68d9`, Redwood, BAR0 256 MiB, BAR2 128 KiB |
| Board ROM | `fw/hd5570.rom`, 59,904 bytes, valid ATOM BIOS |
| ATOM init | 1,164 MMIO writes; `CONFIG_MEMSIZE=0x400`, meaning 1024 MiB on discrete Evergreen |
| VRAM training | BAR0 retained `0xa5a55a5a` exactly after the unmodified board ATOM table |
| MC layout | AGP `0x00000000-0xbfffffff`; 1 GiB FB relocated to `0xc0000000-0xffffffff` |
| CP firmware | `REDWOOD_pfp.bin` 4,480 B; `REDWOOD_me.bin` 5,504 B |
| CP ring | scratch changed to `0xdeadbeef` |
| CP to AGP | three payload cases passed; diagnostic only, no ALU |
| LS launch | compiler-produced `CF_END` no-op dispatch retires and writes its separate fence |
| RAT-only launch | compiler-produced constant-store shader retires and fences, but target remains unchanged |
| Compiler add | 144-byte Redwood kernel contains two `VTX_READ_128`, four FP32 `ADD`s, and `MEM_RAT_CACHELESS STORE_RAW` |

## Current blockers

There are two separate defects, in this order:

1. A retiring RAT-only shader does not modify its target in AGP or VRAM.
2. The full VTX+ADD+RAT kernel stalls with `SH` and `SPI` busy, so the VTX
   descriptor/path must be debugged after RAT writes become observable.

The default pool is AGP. VRAM is optional only via `AMD_REDWOOD_VRAM_POOL=1`.

## Experiments already completed

| Experiment | Result | Interpretation |
|---|---|---|
| Original copied Evergreen stub | Zero-filled, not a real shader | Replaced; unsafe as proof |
| Generic Redwood ATOM | 1 GiB configured; BAR0 sticky | VRAM is trained on this board |
| CP-only ring and payload writes | Pass | Command transport and AGP CP writes work |
| Real no-op LS kernel | Fence pass | CP compute packets, shader fetch, SQ/SPI launch, and completion work |
| RAT constant store to AGP | Fence pass, 4 KiB target unchanged | RAT state/address/commit is wrong; not a shader-launch failure |
| RAT constant store to VRAM | Fence pass, target unchanged | AGP-only write limitation is not yet proven |
| Absolute VRAM RAT base | No write | Did not fix addressing |
| FB-relative VRAM RAT base | No write | Did not fix addressing |
| `CB_TARGET_MASK=0xf`, `CB_SHADER_MASK=0` | No write | Matches Mesa but is insufficient |
| `CB_COLOR_CONTROL=0x00cc0000` | No write | Correct shared CB mode but insufficient |
| Linux Evergreen clear-state table | No write | Common clear state alone is insufficient |
| Redwood golden/SQ resource subset | No-op works; RAT still absent | Necessary initialization, not sufficient for RAT |
| Backend remap from efuse (`0x1100`) | No write | Correct topology, not sufficient |
| Full add before RAT isolation | Fence timeout, `SH/SPI` busy | VTX binding is a second defect |

## Hypotheses and falsification roadmap

### H1 — live RAT context differs from intended PM4

- Why plausible: context packets target registers above the directly mapped
  MMIO window; a wrong packet count, compute flag, or register offset could be
  silently programming different state.
- Prove: read back `CB_COLOR0_BASE..INFO/ATTRIB/DIM`, `CB_TARGET_MASK`,
  `CB_SHADER_MASK`, and `CB_COLOR_CONTROL` through MM_INDEX or a CP register-copy
  packet after binding.
- Disprove: every live value exactly matches the generated PM4 and Mesa.
- Next action: implement context-register capture and compare field by field.

### H2 — a required Mesa common-start register is missing

- Why plausible: direct bring-up does not execute the complete Mesa
  `evergreen_init_atom_start_cs` and framebuffer atom graph.
- Prove: diff the emitted PM4/state list against Mesa and find a shared CB/SX
  register still at reset value; adding it makes constant RAT store visible.
- Disprove: complete relevant state equivalence with no output.
- Candidates: CB/SX export controls, render condition state, coherency base
  enables, and framebuffer ancillary state.

### H3 — RAT completion/cache visibility is wrong

- Why plausible: the fence is written by CP after a compute flush, while CPU
  visibility may require a different Evergreen event or coherency sequence.
- Prove: have the GPU read/copy the RAT target after `CS_PARTIAL_FLUSH`, or read
  VRAM via MM_INDEX, and observe the constant even when CPU mapping is stale.
- Disprove: both GPU-side and BAR/MM_INDEX reads show the original canary.

### H4 — RAT base uses a different address model

- Why plausible: VRAM was relocated to the top of the 32-bit MC aperture and
  RAT/CB base fields may not use the same address convention as VTX.
- Evidence against: both absolute `0xc0100000` and FB-relative `0x00100000`
  trials failed.
- Remaining proof: read live base register and test a zero-offset target at the
  exact framebuffer base while scanning both candidate physical locations.
- Disprove: correct live base plus no write at any candidate location.

### H5 — AGP is readable by CP but not writable by RAT

- Why plausible: CP uses the memory bridge; RAT traverses SH/SX/CB and a
  different MC client/TLB.
- Prove: identical verified state writes VRAM but not AGP.
- Disprove: RAT fails in verified VRAM state too, or succeeds in AGP.
- Current status: not proven; both targets currently fail, so do not require
  VRAM for the default path yet.

### H6 — backend/SX topology is incomplete

- Why plausible: RAT stores use a render backend. Redwood has four pipes and
  two active backends.
- Evidence: RCU efuse disable mask is zero; Linux remap is `0x1100`.
- Disprove/finalize: capture `GB_ADDR_CONFIG`, `GB_BACKEND_MAP`, active SIMD/RB
  state, and relevant SX registers after reset and compare to Linux.

### H7 — constant-buffer ABI is wrong

- Why plausible: LLVM reads `%out/%a/%b` from `KC0[2].y/.z/.w`.
- Evidence against: assembly explicitly shows those slots; RAT-only shader
  retires when `%out=0`.
- Prove: vary `%out` among aligned offsets and scan the whole target.
- Disprove: live constant-cache base/size and offset movement are correct but
  no store appears.

### H8 — VTX resource descriptor or resource index is wrong

- Why plausible: full kernel stalls only when `VTX_READ_128` executes.
- Prove: after RAT works, run a one-input copy kernel using VTX1 and RAT0.
- Disprove: copy passes for multiple offsets and vectors.
- Checks: Evergreen descriptors are eight dwords; compute resource base is
  `(EG_FETCH_CONSTANTS_OFFSET_CS + 1) * 8`; stride is one byte; size is bytes-1;
  type is valid buffer; cache invalidation precedes dispatch.

### H9 — compiler kernel itself is incompatible

- Why plausible: modern LLVM still emits legacy R600 code, but its ABI could
  differ from the vendored Mesa path.
- Evidence against: no-op retires; assembly is valid Redwood CF/ALU/VTX/RAT.
- Prove: constant RAT store, then VTX copy, then add, each built by the same
  compiler and verified independently.
- Disprove: a hand/Mesa-generated equivalent works while LLVM output does not.

### H10 — VRAM training is incomplete despite BAR0 retention

- Why plausible: some GPU clients could require tiling/channel state beyond
  simple host BAR retention.
- Evidence against: ATOM reports 1 GiB and BAR0 retains arbitrary data.
- Prove: CP/engine copy within VRAM and compare BAR0; inspect channel/address
  config from Linux.
- Disprove: multiple GPU clients read/write VRAM correctly.

## Ordered execution plan

1. Add live context-register readback after RAT binding.
2. Compare captured values with the PM4 builder and Mesa source.
3. Complete the missing common CB/SX state until the RAT constant store writes
   `[11, 22, 33, 44]` into AGP.
4. If CPU visibility is ambiguous, add a GPU-side target read/copy and MM_INDEX
   readback.
5. Only if verified AGP RAT remains impossible, repeat the identical test in
   verified VRAM and document the architectural boundary.
6. Compile/run a VTX1-to-RAT0 copy kernel and correct the resource descriptor.
7. Run the real four-ADD kernel on default, mixed-sign, decimal, and repeated
   cases.
8. Remove temporary no-op/store switches or retain them as clearly labeled
   diagnostics; update README and selftests.

## Commands

```bash
# Offline
python3 -m py_compile examples_egpu_terrascale2/add.py
python3 examples_egpu_terrascale2/add.py --selftest --chip=hd5570

# Proven CP/AGP diagnostic (not arithmetic)
python3 examples_egpu_terrascale2/add.py --cp-mem-write-test --chip=auto --test

# Current isolated RAT test, AGP default
AMD_REDWOOD_STORE=1 python3 examples_egpu_terrascale2/add.py --chip=auto

# Optional comparison only
AMD_REDWOOD_STORE=1 AMD_REDWOOD_VRAM_POOL=1 \
  python3 examples_egpu_terrascale2/add.py --chip=auto

# Required final behavior
python3 examples_egpu_terrascale2/add.py --chip=auto
```

## Current conclusion

The new card is not using CPU arithmetic. The CP and LS engines are genuinely
running, and VRAM training is genuinely successful. The remaining failure is
an unobserved Evergreen RAT write followed by a separate VTX-resource stall.
No successful add should be claimed until the RAT target contains the expected
FP32 vector and passes multiple cases.
