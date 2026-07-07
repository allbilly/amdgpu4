# RX570 eGPU Bring-Up Progress (TinyGPU / M1 Mac)

**Goal:** Run vector-add on **AMD RX570 (Polaris10 / gfx803, `1002:67df rev 0xef`)** via **TinyGPU.app** bare-metal MMIO/PM4 — not macOS `AMDRadeon*` kexts.

**Last updated:** 2026-07-08

### Status at a glance

| Item | State |
|------|--------|
| **Blocker** | Layer 1 ATOM `asic_init` incomplete → VRAM not trained |
| **Proof** | `CONFIG_MEMSIZE=0`, `MISC0&0x80` clear, BAR0 dead |
| **Next** | Path A: Linux golden trace on same RX570, or Path B: fix `atom_replay.py` JMP polls |
| **Safe to run** | `--probe`, `--selftest` only |
| **Code this week** | `atom_replay.py` VBIOS parsers (NootedRed `ATOMBIOS.hpp`) |
| **Refs reviewed** | 31 AGENTS.md repos — tier-1: `linux` / `ROCm amdgpu` only |

---

## 🔴 BLOCKER — VRAM Not Trained (read this first)

**Everything in this project is blocked until GDDR5 is alive.** Vector-add, `LoadUcodes`, firmware buffers in VRAM, BAR0 writes, and reliable USB4 MMIO all depend on the memory controller finishing **hardware training**. We do **not** have that today.

### What “trained” means (hardware truth)

Linux `amdgpu` and our `vram_training_ok()` agree on the same proof registers:

| Register | Offset | Trained (RX570) | Our last probe |
|----------|--------|-----------------|----------------|
| `mmCONFIG_MEMSIZE` | `0x150a` | **4096** (MB) | **`0`** (or fake **`0x10`** after ATOM bail) |
| `mmMC_SEQ_MISC0` | `0xa80` | **bit `0x80` set** | bit `0x80` **clear** (`0x1800` after bail) |
| BAR0 VRAM aperture | PCIe BAR0 | read/write works | writes return **`0xffffffff`** |
| `mmMM_INDEX` / `mmMM_DATA` | `0x0` / `0x1` | CPU can fill VRAM | **dead** until MC + `BIF_FB_EN` path works |

**`vram_trained()`** in `polaris_boot.py` requires **both** valid `CONFIG_MEMSIZE` (not 0/0xffff) **and** `MISC0|0x80`. Do not trust `MC_IO_DEBUG_UP_13` bit 23 alone — that only means MC ucode ran once, not that VRAM is usable.

### Why M1 + TinyGPU is harder than Linux

On a normal PC, **one of these** runs before the OS driver:

1. **VBIOS option ROM** (x86 POST) — runs ATOM tables from firmware at boot, or  
2. **Linux `amdgpu` hotplug** — `amdgpu_device_asic_init()` → software interpreter runs **`ATOM_CMD_INIT`** from the ROM image.

We have **neither** on the M1 eGPU path:

- No x86 BIOS executes the VBIOS ROM as code — we only **read** the ROM via SMC/PCI and interpret it in Python (`atom_replay.py`).
- No macOS `AMDRadeon*` kext and no Linux `amdgpu` — **TinyGPU** gives raw MMIO/PM4 only.

So **we must cold-boot train VRAM ourselves**, matching what Linux does inside `atom.c` + `gmc_v8_0.c`.

### Two-layer training (Linux Polaris VI — both required)

```
Layer 1 — ATOM asic_init (VBIOS bytecode, software interpreter)
  amdgpu_atom_execute_table(ATOM_CMD_INIT)
    → CallTable 5: MemoryControllerInit
    → MMIO polls until CONFIG_MEMSIZE + MC regs valid
  Without this: MEMSIZE stays 0, BAR0 dead, SMC may stay in boot ROM.

Layer 2 — MC microcode (polaris10_mc.bin)
  gmc_v8_0_polaris_mc_load_microcode()
    → upload sequencer ucode via MC_SEQ_SUP_*
    → poll mmMC_SEQ_MISC0 until bit 0x80
  Without Layer 1: ucode upload runs but training never completes (timeout).
```

**Current gap = Layer 1 incomplete.** `atom_replay.py` runs ~5k MMIO writes then hits **stuck backward JMP** loops (memory-training polls). Linux **aborts** after ~20s (`-EINVAL`). We optionally **bail** (`AMD_ATOM_JUMP_BAIL=1`) and fall through — that produces **fake** state (`MEMSIZE=0x10`, `MISC0=0x1800`), not real GDDR5.

### What is “VRAM training”? (glossary — hard to Google)

**Not a marketing term.** AMD/Linux docs rarely say “VRAM training.” It means the **memory controller + GDDR5 PHY** learn timing and electrical margins so framebuffer reads/writes work.

| Plain English | AMD/Linux name | Where in code |
|---------------|----------------|---------------|
| VBIOS runs init tables from ROM | **`ATOM asic_init`** / `ATOM_CMD_INIT` | `atom.c` → `amdgpu_atom_asic_init` |
| Memory controller table inside asic_init | **`MemoryControllerInit`** (CallTable **5**) | VBIOS bytecode; TrustOS journal |
| Load MC sequencer firmware | **`polaris10_mc.bin`** / MC ucode | `gmc_v8_0_polaris_mc_load_microcode` |
| PHY training finished | poll **`MC_SEQ_MISC0` bit `0x80`** | comment in `gmc_v8_0.c`: `/* wait for training to complete */` |
| VRAM size visible to driver | **`CONFIG_MEMSIZE`** (`0x150a`) | set during Layer 1, not a substitute for training |

**Normal PC:** motherboard **POST** runs Layer 1 before OS; Linux amdgpu runs Layer 2 in `gmc_v8_0_hw_init`. **We** must do both on M1 + TinyGPU.

**Search keywords that work** (avoid bare `polaris vram training`):

```
gmc_v8_0_polaris_mc_load_microcode
amdgpu_atom_asic_init MemoryControllerInit
polaris10_mc.bin amdgpu
MC_SEQ_MISC0 0x80 site:github.com torvalds/linux
site:lists.freedesktop.org amdgpu polaris mc firmware
```

**Canonical patch mail:** [amd-gfx 2017-03 — load MC firmware for Polaris](https://lists.freedesktop.org/archives/amd-gfx/2017-March/006616.html) (training poll loop).

**Inspect your ROM offline:**

```bash
python3 -c "from atom_replay import atom_info; print(atom_info(open('rx570.rom','rb').read()))"
# → vram_mb, vram_type, mc_phyinit_off, bios_scratch_reg_start, mdt_count, …
```

### Symptom chain (why LoadUcodes hangs)

```
VRAM untrained
  → cannot place header_buffer / smu_buffer in real VRAM
  → SMC DMA read during PPSMC_MSG_LoadUcodes fails or hangs
  → MMIO poll storm (if uncapped) → USB4 link drop → pci=0xffff → replug
```

Linux puts TOC + scratch in **VRAM** and IP images in **GTT**; SMC must **read VRAM MC addresses** during `LoadUcodes`. Programming `CONFIG_MEMSIZE=4096` by hand does **not** train the PHY — it only fools our software checks if we’re not careful.

### What is NOT the fix

| Approach | Verdict |
|----------|---------|
| Skip ATOM, only `load_mc_firmware()` | Times out — MC ucode needs MC regs from asic_init |
| `AMD_BOOT_LOADUCODES_UNTRAINED=1` | Hangs / crashes USB4 — **never** on untrained VRAM |
| `AMD_ATOM_JUMP_BAIL=1` without real training | Fake MEMSIZE — safe-ish for staged probe, **not** success |
| macOS kext “black box” (NootedRed, NootRX, WhateverGreen) | **Wrong layer** — closed `AMDRadeon*` does training; open kexts are hooks, not recipes. NootedRed **VBIOS parser only** (ported to `atom_replay.py`). |
| TrustOS SDMA milestone | Assumes **VBIOS already POST’d** on x86; no cold train |
| VFIO passthrough (Aitbytes, Zile995) | Windows guest driver trains VRAM; Proxmox PCI rescan ≠ M1 cold boot |
| PolarisBiosEditor / AtomBiosEditor / upp / ReBAR / RGB tools | Wrong layer |

### Real fix paths (ordered)

1. **Path A (best):** Capture Linux `asic_init` MMIO trace on the **same RX570** → `AMD_BOOT_ATOM_REPLAY=trace.json` (`tools/linux_trace_asic_init.sh`).
2. **Path B:** Fix `atom_replay.py` until `ASIC_Init` → `MemoryControllerInit` completes **without bail** — use TrustOS [`journal.md`](https://github.com/nathan237/TrustOS/blob/main/memory/journal.md) checklist (IIO, JMP base, parser offsets, `ps[]` sizing).
3. **Path C:** After real training, confirm **BAR0 or MM_INDEX** writes, then `pre-fw` must show `trained=True` and `load_ok=True` before full `add.py`.

### Success criteria (do not proceed past until met)

```bash
# After --boot-stage=atom or --boot-stage=pre-fw:
CONFIG_MEMSIZE=0x1000   # 4096 MB — not 0, not 0x10
MC_SEQ_MISC0 & 0x80     # training complete bit set
BAR0 or MM_INDEX        # vram write/read round-trip at low offset
pre-fw: trained=True load_ok=True
```

**Until the table above is true, the RX570 is not ready for `LoadUcodes`, compute dispatch, or vector-add.**

---

## ⚠️ STOP — Safety (VRAM must be trained first)

> Full problem explanation: **[🔴 BLOCKER — VRAM Not Trained](#-blocker--vram-not-trained-read-this-first)** above.

**Repeated USB4 crashes** are caused by running **`LoadUcodes` / heavy boot** when VRAM is not usable. Each crash may require a physical replug.

### Is `python3 add.py` safe? (2026-07-08)

**Default full `add.py` is safer than before but still not recommended** until `--boot-stage=pre-fw` shows `load_ok=True` or `trained=True`.

| Command | Safe? | What happens |
|---------|-------|--------------|
| `python3 add.py --probe` | ✅ **Yes** (once) | Few PCI/MMIO reads; stop if `pci=0xffff` |
| `python3 add.py --selftest` | ✅ | Transport self-test only |
| `python3 add.py --boot-stage=atom` | ⚠️ Low–medium | ATOM only (~5k MMIO); bail may fake training |
| `python3 add.py --boot-stage=smc` | ⚠️ Medium | SMC upload + mailbox |
| `python3 add.py --boot-stage=pre-fw` | ⚠️ Medium–high | Full boot **except** LoadUcodes; reports `load_ok` |
| **`python3 add.py` (full, default env)** | ⚠️ **Not recommended** | Skips LoadUcodes if VRAM dead (**good**), but still runs SMC + `mc_program` + GART + compute init — **lots of MMIO**, USB4 drop risk; exits with error, won't vector-add |
| `AMD_BOOT_LOADUCODES_UNTRAINED=1 python3 add.py` | ❌ **Unsafe** | Forces LoadUcodes hang / PCIe drop |

**Why full `add.py` is not “safe” even with gates:** `polaris_boot.boot()` still executes `start_smc`, `mc_program`, `gart_enable`, `enable_compute` when prereqs fail — only **`load_ip_firmware()` / LoadUcodes is skipped**. That MMIO volume can still stress the TB link (same class of crashes as before, minus the 120s LoadUcodes poll).

**Run full `add.py` only when** `pre-fw` prints `trained=True` OR (`bar0=True` or `mm=True`) AND `load_ok=True`.

**Root rule (Linux):** `header_buffer` + `smu_buffer` live in **VRAM**. SMC DMA-reads them during `PPSMC_MSG_LoadUcodes`. Untrained VRAM → LoadUcodes hang → USB4 drop.

---

## Latest Session (2026-07-08) — Docs + VBIOS parser, no hardware runs

### Research conclusions

| Topic | Verdict |
|-------|---------|
| **ChefKiss NootedRed / NootRX** | Open source but **no VRAM training** — Lilu patches on Apple kexts. NootedRed: Vega **iGPU** only. NootRX: RDNA2 `0x73xx` only. **Useful:** `ATOMBIOS.hpp` VBIOS validation → ported to `atom_replay.py`. |
| **WhateverGreen** | macOS dGPU patches; Polaris often native X5000. Training inside closed kext. **0** for TinyGPU. |
| **TrustOS `atom/` module** | **Never in git** (all branches/tags) — journal only. |
| **Aitbytes/proxmox-amd-gpu-passthrough** | Same **`1002:67DF`**; documents broken PCIe SBR + Code 43 failed POST; PCI remove/rescan workaround. VFIO/Windows — symptom parallel only. |
| **Zile995/PinnacleRidge-Polaris-GPU-Passthrough** | Generic Arch libvirt VFIO + `amdvbflash` ROM dump; **no** Polaris reset fix — superseded by Aitbytes for `67df` issues. |

### Code — `atom_replay.py` (from NootedRed + linux headers)

| Addition | Purpose |
|----------|---------|
| `check_atom_bios()` | `0xAA55` + `ATOM`/`MOTA` magic |
| `mdt_offset()` / `MDT_IDX_*` | Master data table lookup (`VRAM_INFO=0x1C`, etc.) |
| `parse_firmware_info()` | `main_call_parser`, `bios_scratch_reg_start` |
| `parse_vram_info()` | GDDR5 size, channels, `mc_phyinit_off` from ROM |
| `atom_info()` | Extended dump when `DEBUG=1` during boot |

### AGENTS.md

**31 repos** ranked in [External References](#agentsmd-repos--ranked-for-vram-training-2026-07-08) below. Tier-1 unchanged: **linux / ROCm amdgpu** only.

---

## Latest Session (2026-07-07 late PM) — Research + Safety, No Runs

### DeepWiki / Linux amdgpu — Correct Polaris VI Boot Order

Prior notes in this file **contradicted each other** on LoadUcodes vs `gart_enable`. Cross-checking `amdgpu_device_ip_init` in linux amdgpu:

```
amdgpu_device_init()
  amdgpu_read_bios_from_rom + amdgpu_atombios_init
  amdgpu_device_need_post()?  → amdgpu_device_asic_init()
       amdgpu_atom_execute_table(ATOM_CMD_INIT)   # memory training polls
  amdgpu_device_ip_init()
    loop: all IP sw_init
      vi_common_hw_init          (COMMON — early)
      gmc_v8_0_hw_init           (GMC — early, before fw_loading)
        gmc_v8_0_mc_program
        gmc_v8_0_polaris_mc_load_microcode   # poll MISC0 bit 0x80
        gmc_v8_0_gart_enable                 # BEFORE LoadUcodes
    smu7_init (pp sw_init)       # VRAM alloc header_buffer + smu_buffer
    amdgpu_ucode_create_bo       # GTT fw_buf
    amdgpu_device_ip_hw_init_phase1   (IH)
    amdgpu_device_fw_loading
      polaris10_start_smu        # upload/start SMC bin
      smu7_request_smu_load_fw   # SMU_DRAM, DRV_DRAM, LoadUcodes
    amdgpu_device_ip_hw_init_phase2   (gfx_v8_0_hw_init, sdma, …)
```

**Confirmed:** `gart_enable` is **before** `LoadUcodes`. `fw_buf` is **GTT**; TOC + scratch are **VRAM**.

### DeepWiki / Linux — VRAM Training Proof

| Register | Trained means |
|----------|----------------|
| `mmCONFIG_MEMSIZE` (`0x150a`) | ≥ 128 MB (4096 for RX570) |
| `mmMC_SEQ_MISC0` (`0xa80`) | **bit `0x80` set** |
| `mmMC_VM_FB_LOCATION` | Valid FB base/top (not `0` / garbage) |

**ATOM jump loops:** Linux **aborts** `asic_init` after ~20s stuck backward jump (`ctx->abort`, `-EINVAL`). It does **not** fall through. Our `AMD_ATOM_JUMP_BAIL=1` is an eGPU-only escape hatch — it completes with **fake** training (`MEMSIZE=0x10`, `MISC0=0x1800`).

**MC ucode path:** `gmc_v8_0_polaris_mc_load_microcode` also polls `MISC0|0x80` with `usec_timeout` then **falls through** even if incomplete (driver continues; hardware may still be broken).

### DeepWiki / tinygrad — Not Applicable to RX570

| Path | gfx803? | Boot model |
|------|---------|------------|
| `KFDIface` + `/dev/kfd` | Yes (Linux kernel) | amdgpu driver does everything |
| `PCIIface` → `AMDev` | **No** | PSP → TMR → MP1 → MMHUB (RDNA/GFX9+) |
| **`polaris_boot.py` (ours)** | **Yes** | VI path: ATOM → SMC7 → GART → LoadUcodes |

tinygrad `AMDDevice` asserts `gfx942` / `gfx11+` only. **Do not port `AMDev` boot to Polaris** — use linux amdgpu VI sequence.

### smu7_request_smu_load_fw Message Sequence (Polaris10)

| Step | Message | Arg |
|------|---------|-----|
| 1 | `PPSMC_MSG_SMU_DRAM_ADDR_HI` `0x252` | `upper32(smu_buffer.mc_addr)` |
| 2 | `PPSMC_MSG_SMU_DRAM_ADDR_LO` `0x253` | `lower32(smu_buffer.mc_addr)` |
| 3 | (build TOC in header_buffer VRAM) | |
| 4 | `PPSMC_MSG_DRV_DRAM_ADDR_HI` `0x250` | `upper32(header_buffer.mc_addr)` |
| 5 | `PPSMC_MSG_DRV_DRAM_ADDR_LO` `0x251` | `lower32(header_buffer.mc_addr)` |
| 6 | `PPSMC_MSG_LoadUcodes` `0x254` | `fw_mask` (`0x47e` Polaris10) |
| 7 | Poll `UcodeLoadStatus` @ soft_regs+`0x6c` | `(status & mask) == mask` |

Linux waits only for **SMC RESP ack** on the message; completion is **`UcodeLoadStatus` poll**, not 120s inside `smc_send_msg`.

### MM_INDEX VRAM Access (when BAR0 dead)

Linux `amdgpu_device_mm_access`: `mmMM_INDEX = (pos & 0x7fffffff) | 0x80000000`, `mmMM_INDEX_HI = pos >> 31`. **`pos` is offset from `vram_start` (0)**, not visible-BAR window. After MM writes: `vi_flush_hdp` (`mmHDP_MEM_COHERENCY_FLUSH_CNTL`). Requires `mmBIF_FB_EN=0x3`, `mmBIF_MM_INDACCESS_CNTL=0`.

**Code fix applied:** `vram_mc_offset()` default `AMD_BOOT_MM_OFFSET=full` (was wrongly using visible window for in-range MC addrs).

### What Killed the GPU (crash post-mortem)

| # | Cause | Mitigation (in code) |
|---|--------|----------------------|
| 1 | **`LoadUcodes` on untrained VRAM** — SMC hangs, MMIO poll storm | `AMD_BOOT_LOADUCODES_UNTRAINED` default **`0`**; `load_ip_firmware_prereqs()` |
| 2 | **`smc_send_msg` polled UcodeLoadStatus 120s** inside LoadUcodes | Removed — only wait SMC RESP; `wait_ucode_load` separate (20s, 100ms poll) |
| 3 | **ATOM `JUMP_BAIL=1` + unlimited writes** → 119k MMIO | `ATOM_MAX_WRITES=65536`, `ATOM_JUMP_MAX=512` |
| 4 | **ATOM MEMSIZE poll** treated `16` as success | Poll requires `MEMSIZE >= 128` |
| 5 | **Full `mc_program` before training** | `mc_program_light()` when `!vram_trained()` |
| 6 | **GTT-only layout** with dead VRAM | Refused unless `AMD_BOOT_LOADUCODES_UNTRAINED=1` |

### Code Changes This Session (safety)

| File | Change |
|------|--------|
| `polaris_boot.py` | `load_ip_firmware_prereqs()` — gate LoadUcodes |
| `polaris_boot.py` | Default **skip** LoadUcodes when BAR0+MM dead + untrained |
| `polaris_boot.py` | `smc_send_msg` — LoadUcodes no longer blocks 120s |
| `polaris_boot.py` | `wait_ucode_load` — 100ms poll, PCI check every 5th iter |
| `polaris_boot.py` | Boot order: `mc_program` → `load_mc_firmware` → `gart` → LoadUcodes |
| `polaris_boot.py` | `vram_mc_offset` — Linux full offset default |
| `atom_replay.py` | MEMSIZE poll `>= 128`; write cap 65536; bail max disabled |
| `add.py` | `--boot-stage=atom`, `--boot-stage=pre-fw` (stops before LoadUcodes) |

### Env Vars (updated — safety)

| Variable | Default | Purpose |
|----------|---------|---------|
| `AMD_BOOT_LOADUCODES_UNTRAINED` | **`0`** | `1` = force LoadUcodes (crash risk) |
| `AMD_BOOT_UCODE_LOAD_TIMEOUT_S` | `20` | UcodeLoadStatus poll cap |
| `AMD_BOOT_UCODE_POLL_MS` | `100` | Slow poll (was 10ms — MMIO storm) |
| `AMD_BOOT_MM_OFFSET` | `full` | MM_INDEX pos from vram_start |
| `AMD_ATOM_JUMP_BAIL` | `0` | `1` = fake-complete ATOM (unsafe) |
| `AMD_ATOM_JUMP_MAX` | `512` | Backward jump iter cap before bail |
| `AMD_ATOM_MAX_WRITES` | `65536` | Hard MMIO cap during ATOM |

### Current State (last known good probe)

- `pci=1002:67df`, BAR0 writes **FAIL**, `CONFIG_MEMSIZE=0`
- SMC not running at cold probe
- ATOM `asic_init` completes with bail: **4719 writes**, `MEMSIZE=0x10`, `MISC0=0x1800` — **not trained**
- **LoadUcodes must not run** until `MISC0|0x80` + working VRAM write path

### Real Fix Path (no GPU until ready)

**Path A — Fix ATOM training on M1 USB4**

- Why hardware polls never complete: TB MMIO latency, no real VRAM until MC trained
- Linux aborts; we bail — both leave card untrained
- Need: Linux golden trace replay (`tools/linux_trace_asic_init.sh` → `AMD_BOOT_ATOM_REPLAY=trace.json`) **on a Linux box with same RX570**, or fix poll/drain so training completes without bail

**Path B — MC ucode after partial ATOM**

- `load_mc_firmware()` after `mc_program_light` — may set `MISC0|0x80` without full ATOM
- Never run full `mc_program()` before `MISC0|0x80`

**Path C — MM_INDEX bring-up**

- After `BIF_FB_EN=0x3` + `CONFIG_MEMSIZE=4096` programmed: probe `vram_mm_write`/`read` at low offset
- If MM works → hybrid layout (VRAM TOC + GART fw_buf) becomes viable

### Safe Test Plan (when replugged — user runs manually)

```bash
cd examples_egpu
python3 add.py --probe                    # once; stop if pci=0xffff

# Staged only — do NOT run full add.py until pre-fw shows load_ok=True
AMD_BOOT_VBIOS_FILE=/tmp/rx570.rom \
  AMD_ATOM_JUMP_BAIL=1 AMD_ATOM_JUMP_MAX=512 AMD_ATOM_QUIET=1 \
  python3 add.py --boot-stage=atom

AMD_BOOT_VBIOS_FILE=/tmp/rx570.rom \
  AMD_ATOM_JUMP_BAIL=1 python3 add.py --boot-stage=pre-fw
# Want: trained=True OR bar0/mm_index=True before any LoadUcodes attempt
```

---

## External References (curated)

Repos and docs reviewed for this bring-up. **Our path = linux amdgpu VI sequence via `polaris_boot.py`**, not VFIO, VBIOS editors, or tinygrad `AMDev`.

### AGENTS.md repos — ranked for VRAM training (2026-07-08)

Score **0–10** for helping **Polaris10 RX570 cold-boot VRAM train** on **M1 + TinyGPU** (`ATOM asic_init` → MC ucode → `MISC0|0x80`). All **31** repos in `AGENTS.md` + linked docs. DeepWiki + source review (2026-07-08).

| Rank | Score | Repo | DeepWiki / practical verdict |
|------|-------|------|------------------------------|
| 1 | **10** | **torvalds/linux** | **Canonical reference.** `atom.c`, `gmc_v8_0.c`, `amdgpu_atom_asic_init`, `gmc_v8_0_polaris_mc_load_microcode`, SMC7. Source of truth for `atom_replay.py` / `polaris_boot.py`. |
| 2 | **9** | **ROCm/amdgpu** | Same amdgpu driver tree (kernel module fork). Same `asic_init` / MC training paths as linux; use interchangeably with torvalds/linux. |
| 3 | **7** | **nathan237/TrustOS** | **Same cold-boot diagnosis** (`journal.md`: atom POST missing → SMU stuck). Atom walker + GMC lessons. **`atom/` / `smu.rs` never in git**; SDMA path assumes VBIOS POST. Narrative > code. |
| 4 | **5** | **komen205/polaris30-smu-bist** | Polaris **`1002:67DF`** SMU7 mailbox BIST on UEFI x86. Cross-check SMC message IDs / scratch — **after** VRAM alive. Not M1/TinyGPU. |
| 5 | **4** | **geerlingguy/raspberry-pi-pcie-devices** | No VRAM training. **[Discussion #756](https://github.com/geerlingguy/raspberry-pi-pcie-devices/discussions/756)** ARM PCIe **DMA coherency** (cache flush before GPU DMA) — relevant **post-train** for `sysmem_dma_flush`, not Layer 1. |
| 6 | **3** | **Aitbytes/proxmox-amd-gpu-passthrough** | **Same Polaris `1002:67DF`** — documents broken **secondary bus reset** ([kernel #198397](https://bugzilla.kernel.org/show_bug.cgi?id=198397)), Windows **Code 43 / failed POST** after stale state, PCI **remove+rescan** hookscript. Proxmox **VFIO → Windows VM** — wrong layer (guest driver trains VRAM, not bare ATOM). Useful **symptom parallel** to cold eGPU; replug ≈ rescan. Not M1/TinyGPU. |
| 7 | **3** | **boopdotpng/tenstorrent-docs** | [tinygrad-amd-dma vs Blackhole](https://github.com/boopdotpng/tenstorrent-docs/blob/master/hardware/tinygrad-amd-dma-vs-blackhole-host-memory.md) — host memory / DMA model contrast. Tangential transport insight only. |
| 8 | **3** | **tinygrad/7900xtx** | RDNA3 boot docs. **`docs/MEC.md`** notes `polaris10_mec.bin` is F32 MEC + PM4 dispatch hints — **useful after LoadUcodes**, not VRAM train. |
| 9 | **2** | **ChefKissInc/NootedRed** | **VBIOS parser only** — open `ATOMBIOS.hpp` + `checkAtomBios` / `getVBIOSDataTable` ported into `atom_replay.py` (MDT indices, `MOTA` magic). **No VRAM training** in source: `populateVramInfo` = iGPU metadata; `getVBIOSFromVRAM` needs live BAR0. Vega **iGPU** Lilu kext on Intel Hackintosh — wrong GPU & platform. |
| 10 | **2** | **vosen/amdgpu_debug** | GDB GPU kernel trace + LLVM IR split. Compute debug only — **no** ATOM/MMIO boot. |
| 11 | **2** | **allbilly/amdgpu** | `libdrm_amdgpu` PM4 compute POC on **working** Linux driver. Post-init dispatch layer. |
| 12 | **1** | **acidanthera/WhateverGreen** | macOS AMD **dGPU** Lilu patches (board-id, UnfairGVA, etc.) on Intel Hackintosh. Polaris often **native X5000 + device-ID spoof** — training inside **closed** `AMDRadeon*` kext, not in this repo. Conflicts with NootRX/NootedRed. **0** for M1/TinyGPU cold boot. |
| 13 | **1** | **gem5/gem5** | GPU sim (VIPER, SDMA, PM4). Research only; Fiji/gfx803 mentioned historically — **no** real MC training recipe. |
| 14 | **1** | **sarchlab/mgpusim** | Timing sim (GCN3/CDNA). No Polaris bare-metal init. |
| 15 | **1** | **gpgpu-sim/gpgpu-sim_distribution** | NVIDIA-oriented GPGPU sim. Wrong vendor. |
| 16 | **1** | **VerticalResearchGroup/miaow** | RTL GCN-like CU sim; simplified memory — **no** Polaris MC/VRAM train. |
| 17 | **1** | **boopdotpng/rdna-sim** | RDNA sim — wrong gen, no cold boot. |
| 18 | **0** | **ChefKissInc/NootRX** | RDNA2 (`0x73xx`) macOS kext glue — PSP blob `memcpy`, ASIC caps spoof. **Not Polaris** (`0x67DF`). No ATOM/MC training code in open source (hooks Apple X6000 binary). |
| 19 | **0** | **tinygrad/tinygrad** | `AMDev` = **RDNA3/4** PSP→MP1 path. **No gfx803** register tables or Polaris SMC7. |
| 20 | **0** | **allbilly/applegpu** | Apple AGX MMIO — wrong GPU vendor. |
| 21 | **0** | **allbilly/amd_scheduler** | ROCm CU scheduling research on x86 Linux. |
| 22 | **0** | **allbilly/ml_workload** | ML workloads — assumes working GPU stack. |
| 23 | **0** | **Tim453/ClusterSim** | Cluster simulation — unrelated. |
| 24 | **0** | **vosen/ZLUDA** | CUDA→ROCm via **installed amdgpu driver**. No bare-metal init. |
| 25 | **0** | **coreboot/coreboot** | AMD **APU** VBIOS/FSP GOP — integrated gfx only; **no** discrete Polaris RX570 VRAM train. |
| 26 | **0** | **heavyarms2112/atitool** | Linux `-asicinit` on **working** amdgpu — needs trained GPU. |
| 27 | **0** | **Andybf/AtomBiosEditor** | Offline VBIOS editor (clocks/fan) — does not **execute** `asic_init`. |
| 28 | **0** | **xCuri0/ReBarUEFI** | PC UEFI ReBAR — post-POST PCIe BAR resize. |
| 29 | **0** | **acidanthera/VirtualSMC** | macOS SMC emulator — Intel/AMD **CPU** power, not GPU VRAM. |
| 30 | **0** | **Nihal2202/macOS-Tahoe-Ryzentosh** | x86 Hackintosh OpenCore + RX570 **kext** path — opposite of TinyGPU bare-metal. |
| 31 | **0** | **Zile995/PinnacleRidge-Polaris-GPU-Passthrough** | Generic Linux **VFIO passthrough** to Windows/QEMU. Same wrong layer as Aitbytes but less Polaris-specific depth — superseded by Aitbytes guide for `1002:67DF` reset bug. |

**Takeaway:** Only **linux/ROCm amdgpu** are tier-1 for VRAM training. **TrustOS journal** is the best secondary narrative. **NootedRed** = VBIOS parser tier-2. **Aitbytes** = useful **Polaris `67DF` POST/reset** context only (VFIO, not TinyGPU). ChefKiss kexts / VFIO passthrough guides are wrong layer.

### Primary (use these)

| Source | URL / path | Use for |
|--------|------------|---------|
| **Linux amdgpu VI** | `torvalds/linux` → `drivers/gpu/drm/amd/` | Boot order, ATOM, SMC7, GMC8, GFX8 |
| `polaris10_smumgr.c` | `pm/powerplay/smumgr/` | `LoadUcodes`, message IDs `0x250–0x254` |
| `gmc_v8_0.c` | `amdgpu/` | `mc_program`, Polaris MC ucode, `gart_enable` |
| `atom.c` | `amdgpu/` | ATOM interpreter, jump timeout, `asic_init` |
| `gmc_v8_0_polaris_mc_load_microcode` | [2017 amd-gfx patch](https://lists.freedesktop.org/archives/amd-gfx/2017-March/006616.html) | MC ucode upload + `MISC0\|0x80` training poll |
| `vi.c` | `amdgpu/` | `vi_common_hw_init`, `vi_flush_hdp` |
| `gfx_v8_0.c` | `amdgpu/` | KCQ, `CP_HQD_*`, compute enable |
| **Local trace tool** | `examples_egpu/tools/linux_trace_asic_init.sh` | Golden ATOM MMIO → `AMD_BOOT_ATOM_REPLAY` |
| **ARM I/O coherency** | [rpi-pcie #756](https://github.com/geerlingguy/raspberry-pi-pcie-devices/discussions/756) | `sysmem_dma_flush` before SMC DMA read |
| **Register headers** | `asic_reg/bif_5_0_d.h`, `smu_7_1_3_d.h`, `gfx_8_0_d.h` | MMIO offsets (`mmSMC_MSG_ARG_0=0xa4`, etc.) |
| **[TrustOS `memory/journal.md`](https://github.com/nathan237/TrustOS/blob/main/memory/journal.md)** | Polaris10 cold-boot lab notebook | **Best narrative for our blocker** — same chain: no atom POST → SMU boot ROM stuck → SMC not RUNNING → LoadUcodes dead. Atom walker phases, SMC/GMC pitfall fixes. See checklist below. |

### VRAM training debug — TrustOS journal checklist

From [`memory/journal.md`](https://github.com/nathan237/TrustOS/blob/main/memory/journal.md) (Apr–May 2026, BTC-250PRO, `1002:67DF`, headless mining). **Confirms:** fix path is `ATOM_CMD_INIT` / `MemoryControllerInit` (table 5), not a separate VRAM trainer. **`atom/` module + `smu.rs` never committed** on public `main` — journal only, not copy-paste code.

| Journal finding | Our `atom_replay.py` / `polaris_boot.py` |
|-----------------|------------------------------------------|
| Root chain: *atom POST jamais exécuté* → SMU PC `0x2A40–0x2C8C` → GFX unclocked | Same on M1 eGPU; `CONFIG_MEMSIZE=0`, bail → fake `0x10` |
| ATOM parser: `MasterCommandTable` @ ROM **+0x1E** (U16), not +0x20 | Verify `parse_atom_context` offsets |
| **`Frame.start = table base`** for JMP (not `code_start+6`) | ✅ `abs_t = base + target` |
| **IIO executor** required — dies at SETPORT `ATOM_IO_IIO` after table **71** | ✅ `_iio_execute`; verify all IIO ports indexed |
| `ASIC_Init` → CallTable **5** = `MemoryControllerInit` | Must reach in trace/dasm |
| `ps[]` sized from table header, not `ps_size` alone | Check param block sizing |
| SMC: **destructive** `smu7_start_smc` wipes VBIOS-pre-init | ✅ non-destructive detect path |
| `SRAM[0]=0xAAAA5555` = boot-ROM mirror, not always failure | Don't over-interpret |
| SMC bank **0** for upload (bank 11 JUMP → secure mode) | Match Linux `smu7_copy_bytes_to_smc` |
| GMC: SYS_APR **`0x82A/0x82B/0x82C`**, shift **`>>12`** not `>>18` | Verify `mc_program` / `gart_enable` |
| Linux golden trace | `linux_trace_asic_init.sh` + `amdgpu.atom_debug=1` (journal Phase 2 plan) |

### Post-VRAM Polaris reference (use after `MISC0|0x80` + BAR0/MM_INDEX)

| Source | URL / path | Use for |
|--------|------------|---------|
| **[nathan237/TrustOS](https://github.com/nathan237/TrustOS)** | `kernel/src/drivers/amdgpu/firmware.rs` | **Validated SDMA on real Polaris 10 (`1002:67DF`, RX 580X)** on x86 mining board. `polaris_gmc_init()` mirrors Linux `gmc_v8_0_mc_program` + `gart_enable`; `polaris_sdma_full_init()` does GART ring + direct MMIO ucode upload (SDMA/RLC/MEC). Devlog: [`docs/devlog/gpu_amd_sdma_milestone.md`](https://github.com/nathan237/TrustOS/blob/main/docs/devlog/gpu_amd_sdma_milestone.md). |
| TrustOS GMC golden L2 | same file, `polaris_gmc_init` | Linux mmiotrace values on BTC-250PRO — **differs from our `gart_enable()`** (try after VRAM works): |

| Register | TrustOS (golden) | Our `polaris_boot.gart_enable()` |
|----------|------------------|----------------------------------|
| `mmVM_L2_CNTL` | `0x0C0B8E03` | `0x30103` |
| `mmVM_L2_CNTL2` | `0x00000003` (invalidate L1+L2) | `0x30003` |
| `mmVM_L2_CNTL3` | `0x80148009` | `0x24100003` |
| `mmVM_L2_CNTL4` | `0x00000000` @ offset **`0x578`** | computed @ **`0x503`** |

| TrustOS other takeaways | Detail |
|-------------------------|--------|
| DCE blanking | `polaris_dce_disable_all()` before SDMA — stops DMIF PF `0x01078001` (CID `0x78`) starving SDMA |
| Direct fw load | Embedded `polaris10_{sdma,rlc,mec,...}.bin` via MMIO — **no SMC LoadUcodes for SDMA**; possible shortcut once GART works |
| System aperture | `0x80D/0x80E/0x80F`, shift `>>12` — we already match |
| `BIF_FB_EN` | `0x1524`, value `0x3` — we already match |

**TrustOS does NOT fix our blocker:** boots on x86 where **VBIOS already POST'd** the GPU (`CONFIG_MEMSIZE` populated, SMU often active). Firmware loader says *"SMU firmware is typically loaded by VBIOS"*. No cold-boot ATOM training on M1/USB4. `smu.rs` / `atom/` declared in `mod.rs` but **never in public `main` tree**. Treat as reference code, not gospel.

**Deep dive (cloned to `~/Desktop/TrustOS`, 2026-07-07):**

| Question | Answer |
|----------|--------|
| VRAM training code? | **No.** `polaris_mc_setup()` is just `polaris_gmc_init()`. No `gmc_v8_0_polaris_mc_load_microcode`, no `MISC0\|0x80` poll, no MC ucode upload in shipped `firmware.rs`. |
| How did SDMA succeed? | x86 BTC-250PRO: **platform VBIOS POST** trains VRAM before TrustOS boots. Then `gpu smu start` + `gpu sdma init` (GART + direct MMIO fw). |
| Same cold-boot problem? | **Yes.** `memory/journal.md` (Apr 2026): mining board, no display → *"atom POST jamais exécuté"* → SMU boot ROM stuck → SMC not RUNNING. Their fix path was **atom walker** (`gpu atom asic-init`), not a separate VRAM trainer. |
| Is atom code usable? | **Not from public repo.** Journal documents `kernel/src/drivers/amdgpu/atom/{exec,mod,bios,parser}.rs` — ported Linux `atom.c`, ASIC_Init calls `MemoryControllerInit` (table 5). **Sources never shipped on `main`**; kernel won't build without `smu.rs` / `atom/`. |
| SMC lessons for us | (1) **Detect VBIOS-pre-init** (`boot_seq_done && RESP==1 && INPUT!=0`) — destructive `smu7_start_smc` reset/upload **wipes** partial VBIOS state. (2) MSG_Test to boot-ROM-idle SMU can flip SRAM to secure mode. (3) Legacy BIOS CSM helped their board get `SMU_INPUT_DATA=0x20000`. |
| TrustOS bugs to ignore | `POL_CONFIG_MEMSIZE = 0x5428` is **Navi offset** — Polaris is **`0x150a`** (we fixed this; their diag MEMSIZE reads are wrong). |
| Best artifact for us | **[`memory/journal.md`](https://github.com/nathan237/TrustOS/blob/main/memory/journal.md)** — see **VRAM training debug** checklist above |

**Verdict for VRAM train debug:** TrustOS **confirms our diagnosis** (ATOM/POST is the path; GMC alone doesn't train VRAM) but **cannot be run or copied** for training — atom/SMU sources missing on `main`. SDMA milestone (`firmware.rs`) assumes **VBIOS POST already ran** on x86.

### Useful later (post–LoadUcodes / dispatch / validation)

| Source | URL | Use for |
|--------|-----|---------|
| **[tinygrad/7900xtx `docs/MEC.md`](https://github.com/tinygrad/7900xtx/blob/master/docs/MEC.md)** | Polaris note | `polaris10_mec.bin` is **f32** MEC (not RS64); `DISPATCH_DIRECT` → `COMPUTE_DISPATCH_INITIATOR` `0xb800`, `COMPUTE_DIM_X` `0xb804` — validates our PM4 path in `add.py` |
| **tinygrad/7900xtx** | `f32dis.py` + `polaris10_mec.bin` | Reverse MEC firmware **after** `LoadUcodes` loads it |
| **UMR** | `umr -cpc`, `-RS gfx_0.0.0` | Queue dump on Linux (gfx11 examples in MEC.md; gfx8 similar concept) |
| **[Umio-Yasuno/libdrm-amdgpu-sys-rs](https://github.com/Umio-Yasuno/libdrm-amdgpu-sys-rs)** | [`examples/polaris11-result.txt`](https://github.com/Umio-Yasuno/libdrm-amdgpu-sys-rs/blob/master/examples/polaris11-result.txt) | **Post-boot golden** via `libdrm_amdgpu` ioctls (`mc_arb_ramcfg`, `gb_addr_cfg`, `gb_tile_mode`, fw versions). Needs Linux `amdgpu` + `/dev/dri/renderD*`. **Capture same dump on your RX570** (`cargo run --example amdgpu_info`) — Polaris11 ≠ Polaris10 (`0x67FF` vs `0x67DF`). |
| **[gboddin/atitool](https://github.com/gboddin/atitool)** | `atitool show rom.bin` | Offline Polaris VBIOS parser (PowerPlay, VRAM part numbers, 4096 MB GDDR5). Sanity-check `/tmp/rx570.rom` before `atom_replay.py`. Does **not** parse `MCInitParameter` / `MemoryTrainingInfo` or run `asic_init`. |
| **ChefKissInc/NootedRed** | `GPUDriversAMD/ATOMBIOS.hpp`, `NRed.cpp` | **VBIOS validation + MDT lookup** — `checkAtomBios`, `MOTA` magic, `getVBIOSDataTable(index)` → ported to `atom_replay.py` (`check_atom_bios`, `mdt_offset`, `parse_vram_info`). **Not** VRAM training; Vega iGPU kext only. |

### Register / protocol cross-check only

| Source | URL | Verdict |
|--------|-----|---------|
| **[komen205/polaris30-smu-bist](https://github.com/komen205/polaris30-smu-bist)** | UEFI x86_64 SMU mailbox BIST for `1002:67DF` | **Reference only** — [`REFERENCE.md`](https://github.com/komen205/polaris30-smu-bist/blob/main/REFERENCE.md) documents SMU7 mailbox steps, `SMC_MSG_ARG_0=0xA4`, scratch offsets. Does **not** run on M1/TinyGPU. Optional hardware sanity check on **x86 PC + UEFI Shell**. We already pass what it tests (`PPSMC_MSG_Test`). |
| **[Aitbytes/proxmox-amd-gpu-passthrough](https://github.com/Aitbytes/proxmox-amd-gpu-passthrough)** | Proxmox VFIO + `TECHNICAL_DEEP_DIVE.md` | **Polaris `1002:67DF` only** — broken PCIe secondary bus reset ([kernel #198397](https://bugzilla.kernel.org/show_bug.cgi?id=198397)), Code 43 = GPU failed POST. PCI remove/rescan on VM lifecycle; our analog = **USB4 replug**. **No** ATOM/MMIO training code. |
| **Zile995/PinnacleRidge-Polaris-GPU-Passthrough** | [Arch libvirt VFIO](https://github.com/Zile995/PinnacleRidge-Polaris-GPU-Passthrough) | RX 580 + Ryzen 2600; `amdvbflash` ROM → QEMU `<rom file>`. CPU pinning hooks. **No** Polaris SBR reset fix — use Aitbytes instead. |

### Not applicable (do not pursue for this path)

| Source | URL | Why skip |
|--------|-----|----------|
| **vanities/PolarisBiosEditor** | Mining VBIOS strap editor | Memory timing / hashrate mods; requires ATIFlash on Windows. Does not fix ATOM training, BAR0, or `LoadUcodes`. Brick risk. |
| **[Andybf/AtomBiosEditor](https://github.com/Andybf/AtomBiosEditor)** | macOS/ cross-platform VBIOS GUI editor | Offline ROM editor: PowerPlay clocks/voltages/fan, extract/replace ATOM cmd/data tables, checksum. **Partial** RX400/500 support. Does **not** run `asic_init`, train VRAM, or flash on M1/TinyGPU. Same layer as PBE/gboddin/atitool — inspect `/tmp/rx570.rom` only; flashing modded ROM is brick risk and does not cold-boot train on eGPU. |
| **[sibradzic/upp](https://github.com/sibradzic/upp)** | Uplift Power Play — CLI PP table tool | Parse/edit **PowerPlay** tables (clocks, voltages, fan, TDP) from Linux `pp_table` sysfs **or** `upp extract -r rom.bin`. Polaris supported. Needs **working amdgpu** for live `--write`. No ATOM/`asic_init`, no VRAM training, no TinyGPU. Offline `extract` on `/tmp/rx570.rom` is optional PP inspection only — does not touch `MemoryControllerInit` or MC ucode. |
| **Zile995/PinnacleRidge-Polaris-GPU-Passthrough** | VFIO single-GPU passthrough | Superseded for Polaris reset bug by **[Aitbytes/proxmox-amd-gpu-passthrough](https://github.com/Aitbytes/proxmox-amd-gpu-passthrough)**. Linux `amdgpu` unbind → QEMU/Windows VM — unrelated to M1 TinyGPU bare-metal. |
| **tinygrad/tinygrad `AMDev`** | `PCIIface` bare-metal | **gfx9+/RDNA only** (PSP→MP1). No gfx803 register tables or SMC7 boot. |
| **allbilly/amdgpu** | User-space over **kernel** amdgpu | DRM/libdrm; assumes driver already booted GPU. |
| **nathan237/TrustOS** (cold boot) | Bare-metal Rust OS | Assumes **VBIOS-trained VRAM** on x86 PCIe; does not implement M1 TinyGPU transport or ATOM training from cold. See post-VRAM section above. |
| **[Leo-Atienza/Ghost-GPU PR #1](https://github.com/Leo-Atienza/Ghost-GPU/pull/1)** | Pi 5 + RX 580 + ROCm + llama.cpp | **Kernel amdgpu** stack over Wi-Fi — not bare-metal. Copilot repo bootstrap (docs/scripts only). Same wrong layer as `allbilly/amdgpu`. Pi PCIe coherency already covered by rpi-pcie #756. |
| **[matryer/xbar-plugins PR #1220](https://github.com/matryer/xbar-plugins/pull/1220)** | xbar `eGPU_monitor.3s.sh` (closed, unmerged) | macOS **kext** menu-bar monitor (`system_profiler` + `ioreg PerformanceStatistics`). Needs AMDRadeon* driver — opposite of TinyGPU bare-metal path. No VRAM/SMC/MMIO help. |
| **[ChefKissInc/NootedRed](https://github.com/ChefKissInc/NootedRed)** | Vega iGPU Lilu kext | macOS **iGPU** patches (`0x15D8`/`0x164C` APUs). Open ATOM header structs only — see Useful later. **Not** Polaris RX570. |
| **[ChefKissInc/NootRX](https://github.com/ChefKissInc/NootRX)** | RDNA2 dGPU Lilu kext | macOS **Navi 21–23** (`0x73xx`). PSP firmware injection into closed X6000 kext. **Not** Polaris; no training source. |
| **[acidanthera/WhateverGreen](https://github.com/acidanthera/WhateverGreen)** | macOS AMD dGPU patches | Intel Hackintosh; Polaris often native. Training in Apple kext, not this repo. Conflicts with NootRX/NootedRed. |
| **[twifty/aura-gpu](https://github.com/twifty/aura-gpu)** | macOS RGB for ASUS Aura | User-space USB/RGB; no GPU init or VRAM. |
| **[xCuri0/ReBarUEFI](https://github.com/xCuri0/ReBarUEFI)** | UEFI DXE ReBAR enabler | Enlarges CPU VRAM BAR **after** GPU already POST'd on PC firmware. No cold boot, no ATOM, no M1/TinyGPU. |
| **coreboot/coreboot** | Open PC firmware | No Polaris VRAM training path for eGPU; targets board ROM/ACPI on x86. |
| **[heavyarms2112/atitool](https://github.com/heavyarms2112/atitool)** | Linux CLI `-asicinit` | Live GPU on **Linux amdgpu** only; needs trained GPU. Name collision with gboddin/atitool. |
| **PolarisBiosEditor / VFIO / Hackintosh kexts** | — | Wrong layer — we need VI boot from cold, not driver tuning or VM passthrough |

### Boot stage vs reference map

```
[ATOM asic_init]     ← linux atom.c, TrustOS journal.md, atom_replay.py, AMD_BOOT_ATOM_REPLAY trace
                     ← VBIOS parse: NootedRed ATOMBIOS.hpp (check_atom_bios, parse_vram_info)
[MC train + GART]    ← gmc_v8_0.c, polaris10_mc.bin
[SMC + LoadUcodes]   ← polaris10_smumgr.c, smu7_smumgr.c
                     ← alt post-VRAM: TrustOS direct MMIO fw load (SDMA/RLC/MEC)
[GMC golden L2]      ← TrustOS polaris_gmc_init (after VRAM alive)
[KCQ + PM4 dispatch] ← gfx_v8_0.c, tinygrad/7900xtx MEC.md (polaris10_mec.bin)
[vector-add kernel]  ← shaders/egpu-add4.s, add.py PM4Builder
[success fingerprint]← libdrm-amdgpu-sys-rs polaris11-result.txt (post-boot; capture polaris10 on Linux)
```

**Current gap:** first two stages (VRAM trained, BAR0 or MM_INDEX alive). Everything below `LoadUcodes` in the map is blocked until then. TrustOS journal + linux `atom.c` for **training**; TrustOS `firmware.rs` + libdrm golden dump for **post-VRAM validation**.

---

## Previous Session (2026-07-07 PM) — Hybrid VRAM+AGP layout, MM_INDEX path

### Research (linux amdgpu + tinygrad + DeepWiki)

**Linux `smu7_init` / `smu7_request_smu_load_fw` buffer domains (confirmed):**
| Buffer | Domain | SMC address |
|--------|--------|-------------|
| `header_buffer` (TOC) | **VRAM** (`AMDGPU_GEM_DOMAIN_VRAM`) | `header_buffer.mc_addr` |
| `smu_buffer` (scratch) | **VRAM** | `smu_buffer.mc_addr` |
| `fw_buf` (IP images) | **GTT** | `fw_buf_mc + offset` via `amdgpu_gmc_agp_addr` = `agp_start + dma_address` |

**VRAM CPU write when BAR0 dead:** Linux `amdgpu_device_vram_access` falls back to `amdgpu_device_mm_access` using `mmMM_INDEX` (0x0) / `mmMM_DATA` (0x1) with `pos | 0x80000000`. VI HDP flush/invalidate via `mmHDP_MEM_COHERENCY_FLUSH_CNTL` / `mmHDP_DEBUG0`.

**4GB VRAM addressing:** `vram_start=0`, `vram_end=0xffffffff`, `vram_visible_mc=0xf0000000` (last 256MB BAR window) — correct for full 4GB.

### Code changes this session (continued)

| Change | Detail |
|--------|--------|
| **Hybrid uses GART not AGP** | Linux VI has AGP disabled (`agp_size=0`); fw_buf uses GART VA + PTE bind |
| **GART before LoadUcodes** | `gart_enable()` moved before `load_ip_firmware()` (eGPU needs HW VM live) |
| **Contiguous sysmem** | `alloc_sysmem(contiguous=True)` for firmware buffers |
| **CONFIG_MEMSIZE** | Program `mmCONFIG_MEMSIZE` from `AMD_VRAM_MB` when hardware reads 0 |
| **Enhanced `--probe`** | Tests BAR0, MM_INDEX, sysmem paddr/AGP after `mc_program` |

### Env vars (new/updated)

| Variable | Default | Purpose |
|----------|---------|---------|
| `AMD_BOOT_FW_LAYOUT` | `auto` | `vram`/`hybrid`/`gtt`/`agp` |
| `AMD_BOOT_FORCE_HYBRID` | `0` | Try hybrid even if MM_INDEX probe fails |
| `AMD_BOOT_HDP_NONSURFACE` | `1` | Program HDP_NONSURFACE in mc_program |
| `AMD_BOOT_AGP_RAW_PHYS` | `0` | Legacy all-sysmem layout only |

### Test commands (historical — see top section for safe commands)

Do **not** run `python3 add.py` full boot until VRAM trained. Staged probes only.

### ARM I/O coherency ([rpi-pcie #756](https://github.com/geerlingguy/raspberry-pi-pcie-devices/discussions/756))

M1 Mac + Thunderbolt eGPU shares the Pi 5 problem: **CPU cache writes to sysmem may not be visible to GPU DMA**.

- SMC accepts `DRV_DRAM`/`SMU_DRAM` addresses (`resp=0x1`) but **`LoadUcodes` hangs** — likely reading empty/stale GART-backed fw_buf
- Pi fix (yanghaku): `pgprot_dmacoherent()` in TTM for ARM64
- Our fix: `sysmem_dma_flush()` via `msync(MS_SYNC)` before LoadUcodes (`AMD_BOOT_SYSMEM_FLUSH=1`, default on)

| Variable | Default | Purpose |
|----------|---------|---------|
| `AMD_BOOT_SYSMEM_FLUSH` | `1` | Flush CPU cache on fw_buf before SMC DMA read |

### Missing ATOM `asic_init` (root architectural gap)

Linux hot-plug works because **amdgpu replays VBIOS in software**, not because BIOS ran first:

```
amdgpu_device_init()
  → amdgpu_read_bios_from_rom()
  → amdgpu_atombios_init()
  → amdgpu_device_need_post()?   # scratch[7] missing ATOM_S7_ASIC_INIT_COMPLETE
  → amdgpu_device_asic_init()
       → amdgpu_atom_asic_init()  # Polaris VI
            → amdgpu_atom_execute_table(ATOM_CMD_INIT)   # asic_init bytecode
  → amdgpu_device_ip_init()      # SMC, mc_program, LoadUcodes, gart, ...
```

**We have:** `enable_vbios_rom()` + ROM magic `0xe974aa55`  
**We lack:** ATOM interpreter — nobody runs `ATOM_CMD_INIT`

**False skip (fixed):** `MC_IO_DEBUG_UP_13` bit 23 can be set while `CONFIG_MEMSIZE=0` — partial state, not trained VRAM. `load_mc_firmware()` now requires `MISC0|0x80` **and** valid `CONFIG_MEMSIZE`.

**Paths:** (B) trace Linux `asic_init` reg writes → `AMD_BOOT_ATOM_REPLAY=trace.json`; (A) Python `atom_replay.py` runs `ATOM_CMD_INIT` from ROM (default `AMD_BOOT_ATOM_INIT=1`).

| Variable | Default | Purpose |
|----------|---------|---------|
| `AMD_BOOT_ATOM_INIT` | `1` | Run ATOM asic_init from VBIOS ROM before SMC boot |
| `AMD_BOOT_ATOM_FORCE` | `0` | Force asic_init even if scratch says done |
| `AMD_BOOT_ATOM_REPLAY` | — | JSON register trace from Linux (path B) |
| `AMD_BOOT_DUMP_VBIOS` | — | Write full VBIOS ROM to file |

---

## Previous Session (2026-07-07 PM) — Linux-aligned boot, blocked at LoadUcodes

### Research (linux amdgpu + tinygrad + DeepWiki)

**Linux `amdgpu_device_init` order (Polaris VI) — CORRECTED:**

1. `amdgpu_device_asic_init` → ATOM `asic_init` (if need_post)
2. `gmc_v8_0_sw_init` + `smu7_init` (VRAM buffer alloc)
3. **`gmc_v8_0_hw_init`** → `mc_program` → MC ucode → **`gart_enable`** (early, before fw_loading)
4. `amdgpu_ucode_create_bo` (GTT fw_buf)
5. phase1 hw_init (IH)
6. **`amdgpu_device_fw_loading`** → `polaris10_start_smu` → **`smu7_request_smu_load_fw`**
7. phase2 hw_init (GFX, SDMA, …)

~~Earlier note "LoadUcodes before gart_enable" was wrong.~~ GART must be live before SMC reads GTT `fw_buf`.

**Linux buffer layout (`smu7_init` / `smu7_request_smu_load_fw`):**
| Buffer | Domain | CPU write | SMC address |
|--------|--------|-----------|-------------|
| `header_buffer` (TOC) | **VRAM** | `memcpy_toio(kaddr)` via BAR0 | `mc_addr` in VRAM |
| `smu_buffer` (scratch) | **VRAM** | BAR0 | `mc_addr` in VRAM |
| `fw_buf` (IP images) | **GTT** | sysmem | `fw_buf_mc + offset` (GART VA or AGP) |

**tinygrad:** `APLRemotePCIDevice.alloc_sysmem` → TinyGPU `PrepareDMA` writes phys addrs to shm. **gfx803/Polaris not supported** by `AMDev` (gfx9+ only). Our bare-metal path is correct approach.

### Bugs fixed this session

| Fix | Detail |
|-----|--------|
| `mmBIF_FB_EN` | `0x1024` → **`0x1524`** (`bif_5_0_d.h`) |
| VRAM visible MC base | `vram_visible_mc = 0xF0000000` (256MB BAR window at end of 4GB) |
| `MC_VM_AGP_BOT/TOP` | Was zero; now programmed from `agp_start`/`agp_end` |
| numpy int32 overflow | `rreg()`/`reg()` cast to Python `int` before `<< 24` |
| Boot order | `load_ip_firmware` **before** `gart_enable` (matches Linux) |
| Auto layout | BAR0 fail → `agp` sysmem layout (`AMD_BOOT_FW_LAYOUT=auto`) |

### BAR0 framebuffer is dead on this eGPU

After `mc_program` + `BIF_FB_EN=0x3`: writes to BAR0 read back **`0xffffffff`**. Host cannot populate VRAM. Linux relies on BAR0 for `header_buffer`/`smu_buffer`.

`MC_IO_DEBUG_UP_13` bit 23 **set** (VBIOS MC ucode loaded). `CONFIG_MEMSIZE=0`, `MISC0` bit `0x80` clear.

### Layouts tried for LoadUcodes (all fail)

| Layout | SMC addresses | Result |
|--------|---------------|--------|
| VRAM | `0xF00xxxxx` via BAR0 upload | BAR0 writes don't stick |
| GART | `0xff001xxxxx` | PTE table in dead VRAM |
| GART self-map sysmem | PTE at `0xff00000000` | Still timeout |
| AGP (`agp_start + paddr`) | `0x1000xxxxx` | Timeout |
| Raw phys | `0x4000`, `0x19c000` | Timeout |
| RLC-only `mask=0x400` | Same | Timeout |

SMC accepts `SMU_DRAM`/`DRV_DRAM` (`resp=0x1`) but **`LoadUcodes` hangs** (`RESP=0`, `UcodeLoadStatus=0`, PC ≈ `0x3a6c0`).

### Root cause (refined)

SMC cannot **read** the firmware buffers we point it at — not a message-ID bug anymore. Linux needs **working VRAM BAR0** for TOC header + scratch; we don't have that on M1+eGPU without VBIOS `asic_init` memory training.

### Next steps

1. **Verify TinyGPU `PrepareDMA` phys addrs are GPU-reachable** (not just host-local)
2. **VBIOS replay** — run ATOM `asic_init` so BAR0 + `CONFIG_MEMSIZE` work
3. **HDP_NONSURFACE** path to write VRAM without BAR0
4. **Hybrid:** VRAM MC addrs for header/smu (per Linux) + working write path

---

### Critical bug fixed: wrong PPSMC message IDs for DRV_DRAM

`polaris_boot.py` had `PPSMC_MSG_DRV_DRAM_ADDR_HI/LO = 0x255/0x256` (wrong). Correct per `smu7_ppsmc.h`:

| Message | ID |
|---------|-----|
| `PPSMC_MSG_DRV_DRAM_ADDR_HI` | `0x250` |
| `PPSMC_MSG_DRV_DRAM_ADDR_LO` | `0x251` |
| `PPSMC_MSG_SMU_DRAM_ADDR_HI` | `0x252` |
| `PPSMC_MSG_SMU_DRAM_ADDR_LO` | `0x253` |
| `PPSMC_MSG_LoadUcodes` | `0x254` |

**Proof:** Wrong IDs gave `DRV_DRAM resp=0xfe` (UnknownCmd). After fix, all setup messages return `resp=0x1`.

### Current failure: `PPSMC_MSG_LoadUcodes` (0x254)

```text
SMC SMU_DRAM_HI/LO resp=0x1
SMC DRV_DRAM_HI/LO resp=0x1
SMC LoadUcodes resp=0x0 (async)  → UcodeLoadStatus=garbage after 120s
SMC PC ≈ 0x3a6c0 (appears hung)
```

### Linux amdgpu init order (VI / Polaris10) — from `ref/linux`

1. `gmc_v8_0_mc_init` + `vram_gtt_location` (sw_init)
2. `amdgpu_ucode_create_bo` — **fw_buf in GTT**, header/smu_buffer in VRAM
3. phase1 hw_init (common, IH)
4. `amdgpu_device_fw_loading` → `polaris10_start_smu` + `smu7_request_smu_load_fw`
5. phase2 → `gmc_v8_0_hw_init` → `mc_program` → MC ucode → `gart_enable`

### Changes applied this session

- Fixed `smc_send_msg` IndentationError
- Firmware images + TOC + smu_dram staging → **GTT** (`gart_start+` MC addrs)
- GART PTE flags `0x73` (was `0x17`) per `amdgpu_ttm_tt_pte_flags` + `gmc_v8_0`
- **GART page table in VRAM** (`amdgpu_gart_table_vram_alloc`) not sysmem
- Boot order: `gart_enable` → `load_ip_firmware` → `mc_program` (fw before GMC hw_init)
- `LoadUcodes` treated async; poll `UcodeLoadStatus` at soft_regs+0x6c

### Bugs fixed this continuation

- **`mmCONFIG_MEMSIZE` wrong register**: was `0x5428` (DCE), correct is **`0x150a`** (BIF). Both read 0 on this eGPU (no VBIOS training).
- **`smc_soft_reg` treated 0 as invalid**: `smc_read()` filters `0` → `UcodeLoadStatus=0` showed as `None`. Now uses raw `smc_rreg`.
- **`AMD_BOOT_FW_MINIMAL` env check**: `int(x)== "1"` was always false.
- **GART `VM_CONTEXT0_CNTL`**: `0x11` (was `0x9`).

### Root cause hypothesis

`PPSMC_MSG_LoadUcodes` hangs (SMC PC ~`0x3a6c0`, `RESP=0`) because **GPU-side VRAM is not trained** (`CONFIG_MEMSIZE=0`, `MC_SEQ_MISC0` bit `0x80` never sets). SMC cannot fetch firmware from VRAM MC addresses. Linux runs VBIOS `asic_init` / MC training before driver load.

### Next investigation

- [ ] Fix MC ucode training on eGPU (or VBIOS replay via `enable_vbios_rom`)
- [ ] Verify TinyGPU sysmem DMA for GTT path once MC works
- [ ] `AMD_BOOT_FW_MINIMAL=1 AMD_BOOT_FW_MASK=0x400` for single-ucode debug

---

## Earlier Session (2026-07-07 AM) — SMC BOOT WORKING

### Root cause fix: wrong `mmSMC_MSG_ARG_0`

Polaris (smu_7_1_1) uses `mmSMC_MSG_ARG_0 = 0xa4`, **not** `0x96` (`0x96` is `mmSMC_MESSAGE_1`). Writing `PPSMC_MSG_Test` arg `0x20000` to `0x96` corrupted the message interface → perpetual `RESP=0` timeout.

### Working SMC boot

```text
stage=smc smc_running=True PC=0x20558 FLAGS=0x1 STATUS=0x3 RESP=0x1
```

Combined with **segmented upload** (`AMD_BOOT_SMC_UPLOAD=segmented`, sync=4096 dwords): firmware readback verified, GPU stays on PCI.

### Next blocker: IP firmware load + compute

Full `add.py` reaches `load_ip_firmware` (`PPSMC_MSG_LoadUcodes`) then times out. Also fixed: `alloc_sysmem_buffer` unpack, GART PTE fill via byte slices.

---

With `AMD_BOOT_SMC_UPLOAD=chunked` + `AMD_MMIO_DRAIN_EVERY=128`, the full ~130 KiB SMC upload completes and **PCI stays `1002:67df`** through protection-mode handshake. Previous crashes were misattributed to timeouts; many were **PCIe device loss** (`0xffff`).

### Current failure mode (GPU online)

| Step | Result |
|------|--------|
| Upload (chunked + drain) | Completes, PCI OK |
| `RCU_INTERRUPTS_ENABLED` | Already set at boot (`EVENTS=0xf0080`) |
| `PPSMC_MSG_Test` @ `0x20000` | **30 s timeout**, `RESP=0x0`, `pci_online=True` |
| Non-protection fallback | Also times out; `FLAGS=0xaaaa5555` (garbage read) |

**Conclusion:** Firmware image is likely **not landing in SMC RAM** without `pc_sync` barriers during upload. Message interface never responds because protected firmware never starts.

### Upload verify matrix

| Mode | `AMD_BOOT_SMC_SYNC` | GPU after upload | Readback @ `0x20000` |
|------|---------------------|------------------|----------------------|
| `pc_sync` | 32768 | Online | **Mismatch** (no mid-upload barrier for 32490 dwords) |
| `pc_sync` | 4096 | **Offline** at upload finish | — |
| `chunked` + drain | 64 | Online | Not verified; msg timeout |

The `sync=4096` crash at "upload finish" was likely **`smc_flush_upload()` SMC RAM read** (`mmio_sync_smc_data`), not the `pc_sync` barriers. **Fix:** `AMD_BOOT_SMC_FLUSH_READ=0` (new default).

### New defaults (this session)

| Variable | New default | Notes |
|----------|-------------|-------|
| `AMD_BOOT_SMC_UPLOAD` | `segmented` | Burst per segment + 1 PC barrier each |
| `AMD_BOOT_SMC_SYNC` | `4096` | Dwords per segment (~32 segments for 130 KiB FW) |
| `AMD_BOOT_SMC_PC_PAUSE_MS` | `15` | Pause after each SMC PC read |
| `AMD_BOOT_SMC_FLUSH_READ` | `0` | Skip post-upload SMC RAM read |
| `AMD_BOOT_SMC_SETTLE_MS` | `250` | Pause between upload segments |

`pc_sync` @ 8192 still knocks GPU off USB4 in ~1.5 s — too aggressive even with flush read disabled.

---

## Hardware & Transport

| Item | Value |
|------|-------|
| GPU | RX570 Polaris10, PCI `1002:67df` |
| Host | M1 Mac, USB4 eGPU enclosure |
| Transport | TinyGPU.app → `APLRemotePCIDevice` unix socket |
| BAR layout | BAR0 VRAM, BAR2 doorbells, BAR5 MMIO (`fmt='I'`) |
| Linux reference | `ref/linux/` (local torvalds/linux tree) |

---

## Key Files

| File | Role |
|------|------|
| `examples_egpu/add.py` | TinyGPU PCI transport, `PolarisDevice`, CLI, PM4 builder |
| `examples_egpu/polaris_boot.py` | VI boot: SMC, MC ucode, golden regs, GART, compute queue |
| `examples_egpu/atom_replay.py` | ATOM `asic_init` interpreter + `AMD_BOOT_ATOM_REPLAY` |
| `examples_egpu/tools/linux_trace_asic_init.sh` | Capture Linux golden ATOM MMIO trace |
| `shaders/egpu-add4.s` | gfx803 add kernel |
| `ref/linux/drivers/gpu/drm/amd/` | amdgpu init order, `polaris10_smumgr.c`, `vi.c`, `gmc_v8_0.c` |

---

## What Works

- [x] GPU enumeration after USB4 replug / `--reset`
- [x] BAR0/MMIO probe (`--probe`, `--selftest`)
- [x] `alloc_sysmem` segfault fixed (`FileIOInterface.mmap` + `MAP_FAILED` check)
- [x] Chunked SMC upload completes without immediate GPU drop (when avoiding SMC reads)
- [x] VBIOS ROM read via SMC ind-port (`ROM[0]=0xe974aa55`, valid `55AA` signature)
- [x] Golden register init + doorbell aperture (`vi_common_init`)
- [x] PCI health checks during boot (`pci_online()` / `_check_pci()`)
- [x] MMIO write draining (`AMD_MMIO_DRAIN_EVERY`) — GRBM read RPC after N writes

---

## Current Blocker: SMC Firmware Won't Start

Upload likely completes, but **SMC never runs driver firmware**:

| Symptom | Typical value |
|---------|---------------|
| SMC PC | Stuck `0x80`–`0x88` (ROM idle), not `≥ 0x20100` |
| `FIRMWARE_FLAGS` | `0x0` |
| `RCU_INTERRUPTS_ENABLED` (`0x10000`) | Rarely sets without risky SMC reads |
| `PPSMC_MSG_Test` | Timeout — `SMC_RESP` stays `0` |
| `boot_seq_done` (`EVENTS` bit `0x80`) | Sometimes set after replug; not sufficient alone |
| SMC RAM readback | Often `0xaaaa5555` — unreliable on TinyGPU |
| PCI | Stays `0x1002` until SMC read storm or `PPSMC_MSG_Test` knocks GPU off (`0xffff`) |

**End-to-end add kernel test** (`[11,22,33,44] + [10,20,30,40]`) is blocked on SMC.

---

## Critical Discoveries

1. **TinyGPU MMIO writes are fire-and-forget** — client `_bulk_write` does not wait for server ack. Mitigation: periodic `pci.drain_mmio()` (GRBM read RPC) via `AMD_MMIO_DRAIN_EVERY`.

2. **SMC indirect reads are dangerous on M1 eGPU** — `smc_rreg()` during upload or aggressive post-upload polling can knock the GPU off USB4. Safe: `mmio_sync_ind_port()` (read `mmSMC_IND_ACCESS_CNTL` / `mmSMC_IND_INDEX_11` only).

3. **`pc_sync` upload** (SMC PC read barriers) can get past `RCU_INTERRUPTS_ENABLED` but tends to crash the GPU during or after `PPSMC_MSG_Test`. Use only sparingly (`AMD_BOOT_SMC_FINAL_PC_SYNC=1`).

4. **Card is in SMC protection mode** — `SMU_FIRMWARE` has `SMU_MODE` (`0x10000`). Use `polaris10_start_smu_in_protection_mode`, not non-protection (unless forced).

5. **Firmware selection** — `SMU_SEL` bit 17: `1` → `polaris10_smc.bin`, `0` → `polaris10_smc_sk.bin`. Override: `AMD_SMC_FW=...`.

6. **No VBIOS/ACPI handoff on M1** — `boot_seq_done` may be `0` at cold boot; linux gets pre-SMU init from VBIOS/ATOM on PC.

7. **Hackintosh / WhateverGreen / macOS kexts** — not applicable to TinyGPU bare-metal path.

---

## Linux Init Order (reference)

From `amdgpu_device_init` in `ref/linux/`:

1. **sw_init:** read VBIOS ROM (`vi_read_bios_from_rom`), `amdgpu_atombios_init`
2. **hw_init phase1:** `vi_common_hw_init` (golden regs, ASPM, doorbell)
3. **`amdgpu_device_fw_loading`:** `amdgpu_pm_load_smu_firmware` → `polaris10_start_smu`
4. **hw_init phase2:** GMC (`gmc_v8_0_hw_init`), GFX, etc.

Our port order in `polaris_boot.boot()` (aligned to Linux):

```
vi_common_init → enable_vbios_rom → ATOM asic_init
→ mc_program_light + load_mc_firmware (if untrained)
→ gmc_sw_init → start_smc
→ mc_program → load_mc_firmware → gart_enable
→ load_ip_firmware (ONLY if load_ip_firmware_prereqs ok)
→ enable_compute → init_compute_queue
```

**Do not run `load_ip_firmware` until `vram_trained()` or BAR0/MM_INDEX probe passes.**

---

## Test Commands

```bash
cd examples_egpu

# After USB4 replug (required if pci=0xffff):
python3 add.py --reset
python3 add.py --probe

# SAFE staged boot (no LoadUcodes unless pre-fw says load_ok=True)
AMD_BOOT_VBIOS_FILE=/tmp/rx570.rom AMD_ATOM_JUMP_BAIL=1 AMD_ATOM_JUMP_MAX=512 \
  python3 add.py --boot-stage=atom
AMD_BOOT_VBIOS_FILE=/tmp/rx570.rom AMD_ATOM_JUMP_BAIL=1 \
  python3 add.py --boot-stage=pre-fw

# SMC only (still MMIO-heavy)
python3 add.py --boot-stage=smc

# ❌ DO NOT until VRAM trained:
# python3 add.py
# AMD_BOOT_LOADUCODES_UNTRAINED=1 python3 add.py
```

---

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `AMD_MMIO_DRAIN_EVERY` | `128` | GRBM read RPC every N MMIO writes (drain TinyGPU queue) |
| `AMD_BOOT_SMC_UPLOAD` | `pc_sync` | `pc_sync`, `chunked`, `linux`, `per_addr`, `hybrid` |
| `AMD_BOOT_SMC_SYNC` | `8192` | PC barrier interval (dwords); must be ≤ ~32000 for 130 KiB FW |
| `AMD_BOOT_SMC_PC_PAUSE_MS` | `15` | Sleep after each SMC PC read during upload |
| `AMD_BOOT_SMC_FLUSH_READ` | `0` | Post-upload SMC RAM read in `smc_flush_upload` (risky) |
| `AMD_BOOT_SMC_POLL_MS` | `25` | Post-upload poll interval (ms), backoff to 250 ms |
| `AMD_BOOT_SMC_SETTLE_MS` | `250` | Pause after reset deassert before handshake |
| `AMD_BOOT_SMC_TIMEOUT_S` | `60` | Generic firmware wait timeout (seconds) |
| `AMD_BOOT_RCU_TIMEOUT_S` | `30` | `RCU_INTERRUPTS_ENABLED` wait |
| `AMD_BOOT_SMC_MSG_TIMEOUT_S` | `30` | `PPSMC_MSG_Test` / SMC_RESP wait |
| `AMD_BOOT_SMC_PROT` | `auto` | `auto`, `1` (protection), `0` (non-protection) |
| `AMD_BOOT_PROT_SKIP_RCU` | `0` | Skip RCU wait, try message anyway |
| `AMD_BOOT_SMC_VERIFY` | `0` | SMC RAM readback verify (unreliable on TinyGPU) |
| `AMD_BOOT_SMC_FINAL_PC_SYNC` | `0` | Single SMC PC barrier after hybrid upload |
| `AMD_SMC_FW` | (auto) | Override firmware blob name |
| `AMD_BOOT_GOLDEN` | `1` | Apply polaris10 golden regs before SMC |
| `AMD_BOOT_ROM_ENABLE` | `1` | Enable VBIOS ROM (`vi_read_disabled_bios` path) |
| `DEBUG` | `0` | Verbose boot logging |

---

## Session History (summary)

| Session | Result |
|---------|--------|
| alloc_sysmem fix | Segfault resolved |
| MMIO sync discovery | Fire-and-forget writes; read barriers required |
| Protection mode port | Aligned with `vegam_start_smu_in_protection_mode` |
| `pc_sync` upload | Passes RCU sometimes; crashes GPU on msg or mid-upload SMC read |
| Chunked + drain | Upload completes; firmware still doesn't execute |
| Timeout / PCI health | Early abort on `pci=0xffff`; slower polling reduces read storms |

---

## Next Steps

1. **Replug USB4** after any crash — `pci=0xffff` needs physical replug.

2. **Do not run full `add.py`** until `--boot-stage=pre-fw` reports `load_ok=True` or `trained=True`.

3. **Path A (best):** Capture Linux `asic_init` MMIO trace on real hardware → `AMD_BOOT_ATOM_REPLAY=trace.json`.

4. **Path B:** After replug, user runs `--boot-stage=pre-fw` only — check `bar0` / `mm_index` / `MISC0|0x80`.

5. **Path C:** If MM_INDEX works after `mc_program_light`, hybrid VRAM TOC + GART fw_buf per Linux.

6. **After VRAM works:** `load_ip_firmware` with `AMD_BOOT_FW_MINIMAL=1 AMD_BOOT_FW_MASK=0x400` (RLC only) first.

7. **Do not pursue** tinygrad `AMDev`, WhateverGreen, or macOS kexts for this path.

---

## Todo

- [x] Fix `alloc_sysmem` segfault
- [x] SMC boot working (segmented upload + `mmSMC_MSG_ARG_0=0xa4`)
- [x] ATOM `asic_init` interpreter (`atom_replay.py`)
- [x] LoadUcodes safety gates (skip when VRAM dead)
- [ ] **VRAM training** (`MISC0|0x80`, `MEMSIZE>=128`, BAR0 or MM_INDEX)
- [ ] `load_ip_firmware` / `PPSMC_MSG_LoadUcodes` (blocked on VRAM)
- [ ] Run full `add.py` kernel test
