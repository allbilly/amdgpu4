# amdgpu

Small AMDGPU experiments for understanding direct PM4 compute submission and the
trap-handler debugger approach described in `thegeeko.md`.

## What Is Implemented

- `build/amdgpu-poc`: opens a DRM node, initializes `libdrm_amdgpu`, allocates
  GPU BOs, maps them into the GPU VM, uploads a raw shader binary, emits PM4
  compute packets, submits an indirect buffer, and waits for the fence.
- `build/amdgpu-regs2`: reads/writes 32-bit registers through the amdgpu
  debugfs `regs2` interface with SRBM/GRBM banking controls.
- `build/radv-triangle`: draws an offscreen triangle through Vulkan/RADV and
  writes `triangle.ppm`.
- `build/amdgpu-gfx-pm4`: loads text PM4 dwords and can submit them to the GFX
  ring for replay experiments.
- `build/amdgpu-raw-triangle`: experimental raw GFX PM4 triangle draw. Current
  status: the default path launches VS and writes `0x76543210`; `--ps-store-only`
  launches PS and writes `0xfeed1234`; `--color-target` renders a red triangle
  into a linear VRAM color target. Important: `PA_CL_VTE_CNTL = 0x0000003f`
  enables viewport transform; without it, the triangle collapses to the `(0,0)`
  area.
- `tools/extract-text.sh`: compiles GCN assembly and extracts raw `.text` bytes.
- `shaders/`: no-op, trap-test, and a GFX11 trap-handler assembly sketch.

See `docs/debugger-notes.md` for usage and safety notes.

## Build

```sh
sudo dnf install libdrm-devel clang llvm vulkan-tools mesa-vulkan-drivers vulkan-headers vulkan-loader-devel
make
```

This has been tested locally on:

```text
AMD Renoir [Radeon Vega Series / Radeon Vega Mobile Series]
LLVM target: gfx90c
```

Run an embedded no-op shader:

```sh
build/amdgpu-poc --card /dev/dri/renderD128
```

Compile shader examples:

```sh
AMDGPU_MCPU=gfx90c make shaders
```

Run a shader that writes a visible value into the output BO:

```sh
build/amdgpu-poc --shader build/store-output.bin --pass-output-va
```

Expected output starts with:

```text
0000: 78 56 34 12 ...
```

That is little-endian `0x12345678`, proving the GPU executed the shader and
wrote to the output buffer.

Run a simple compute add through the same PM4 `DISPATCH_DIRECT` path:

```sh
build/amdgpu-poc --shader build/add-output.bin --pass-output-va --output-bytes 16
```

Expected output starts with little-endian `2 + 3 = 5`:

```text
0000: 05 00 00 00 ...
```

The compute dispatch path now follows tinygrad's important baseline setup more
closely by programming `COMPUTE_START_X/Y/Z = 0` and dispatching with
`COMPUTE_SHADER_EN | FORCE_START_AT_000`.

You can also verify output through the SDMA engine instead of directly reading
the shader output BO:

```sh
build/amdgpu-poc --shader build/add-output.bin --pass-output-va --output-bytes 16 --sdma-readback
```

Run a simple compute multiply through the same path:

```sh
build/amdgpu-poc --shader build/mul-output.bin --pass-output-va --output-bytes 16
```

Expected output starts with little-endian `6 * 7 = 42`:

```text
0000: 2a 00 00 00 ...
```

Additional PM4 compute shader smoke tests:

| Shader | Command suffix | Expected first bytes |
| --- | --- | --- |
| `sub-output.bin` | `--shader build/sub-output.bin --pass-output-va --output-bytes 16` | `05 00 00 00` (`9 - 4`) |
| `and-or-xor-output.bin` | `--shader build/and-or-xor-output.bin --pass-output-va --output-bytes 16` | `f0 00 00 00 f0 ff 00 00 00 ff 00 00` |
| `shl-shr-output.bin` | `--shader build/shl-shr-output.bin --pass-output-va --output-bytes 16` | `00 01 00 00 80 00 00 00` |
| `load-add-store.bin` | `--shader build/load-add-store.bin --pass-output-va --init-output-u32 11 --init-output-u32 31 --output-bytes 16` | `0b 00 00 00 1f 00 00 00 2a 00 00 00` |
| `multi-thread-addtid.bin` | `--shader build/multi-thread-addtid.bin --pass-output-va --threads-x 8 --output-bytes 64` | `00 00 11 11 01 00 11 11 ... 07 00 11 11` |
| `branch-select.bin` | `--shader build/branch-select.bin --pass-output-va --output-bytes 16` | `aa aa aa aa` |
| `atomic-add.bin` | `--shader build/atomic-add.bin --pass-output-va --threads-x 8 --output-bytes 16` | `08 00 00 00` |

## RADV Triangle

RADV is Mesa's Vulkan driver for AMD GPUs. On this machine `vulkaninfo --summary`
reports:

```text
AMD Radeon Graphics (RADV RENOIR)
driverName = radv
```

Run the offscreen Vulkan triangle:

```sh
build/radv-triangle
```

Force RADV to use a linear-tiled render target for PM4 comparison:

```sh
RADV_TRIANGLE_LINEAR=1 RADV_DEBUG=dumpibs build/radv-triangle > /tmp/radv-triangle-linear-dumpibs.txt 2>&1
```

Expected result:

```text
device: AMD Radeon Graphics (RADV RENOIR)
wrote triangle.ppm (256x256)
```

This uses RADV through the normal Vulkan loader path. It is useful as the known
good reference before reverse-engineering the GFX PM4 packets for a direct
`libdrm_amdgpu` triangle.

See `docs/raw-gfx-triangle.md` for the raw GFX PM4 capture/replay workflow.

## RX570 eGPU (`examples_egpu/`)

Bare-metal Polaris10 / gfx803 (`1002:67df`) over TinyGPU.app on M1 Mac USB4 —
standalone scripts (`add.py`, `mul.py`, `neural.py`, …), not macOS `AMDRadeon*`
kexts. See `examples_egpu/progress.md` for bring-up history.

## Terascale eGPU (`examples_egpu_terrascale/`)

Scaffold for **HD 5570** (Redwood / Evergreen, TeraScale 2) and **HD 4850**
(RV770 / R700, TeraScale 1) over the same TinyGPU transport. Uses linux
**`drm/radeon`** (`ref/linux/drivers/gpu/drm/radeon/`) and Mesa evergreen
compute PM4 — not GCN/`amdgpu`. Hardware bring-up is pending; offline:

```sh
python3 examples_egpu_terrascale/add.py --selftest --chip=hd5570
python3 examples_egpu_terrascale/add.py --dry-run --chip=hd4850
```

See `examples_egpu_terrascale/README.md`.

### VRAM limitation (important)

On this path **GDDR is not a usable CPU/GPU data store**:

| Symptom | Meaning |
| --- | --- |
| `MC_SEQ_STATUS_M` lacks `CMD_RDY` | Memory controller not live for GDDR traffic |
| BAR0 / `MM_INDEX` writes vanish after HDP flush | Posted latch only; value does not stick in VRAM |
| `probe_bar0_writes()` / `probe_vram_mm_writes()` | Strict checks that require survival past HDP |

**Consequence:** KCQ ring, MQD, EOP, shaders, and a/b/out buffers all go through
**AGP (physically contiguous host sysmem)** via `AMD_BOOT_COMPUTE_AGP=1`, not
BAR0 VRAM. Do not put rings or payloads at FB MC addresses — that caused PCIe
panics when GDDR was dead.

**Capacity:** Smoke tests use tiny vectors (4×f32, 2×2 GEMM) on purpose.
AGP can hold more than 16 bytes, but large contiguous DMA allocations are hard
on macOS. Bigger working sets need either:

1. **GART/GTT** — map non-contiguous sysmem pages (walker must be solid), or
2. **Fix VRAM** — train GDDR until `CMD_RDY` and HDP-surviving BAR0/MM writes.

GPU `memcopy` only copies between two already-reachable addresses; it does not
create a VRAM data path.

```sh
python3 examples_egpu/add.py
python3 examples_egpu/neural.py --op=gemm --test
python3 examples_egpu/add.py --probe   # prints BAR0 ok/FAIL after HDP
```

## Notes

The blog post uses an RDNA3/Navi31-style GPU (`gfx1100`). This machine uses
Renoir/Vega (`gfx90c`). The direct PM4 submission path works, but the trap
handler/debugger path needs GFX9/Renoir-specific register offsets and assembly.

## References

- https://thegeeko.me/blog/amd-gpu-debugging/
- https://martty.github.io/posts/radbg_part_1/
- https://gitlab.freedesktop.org/tomstdenis/umr
- https://github.com/tinygrad/tinygrad/blob/0ae957bb/tinygrad/runtime/ops_amd.py
- https://github.com/tinygrad/tinygrad/blob/0ae957bb/tinygrad/runtime/support/am/amdev.py
