# m2391 UFS bring-up notes

Notes and patches from bringing the **ShengUfs** (native UFS) driver up on a
**Meizu 20 Pro (m2391, SM8550 / kalama)** under the UniXNU stack.

Everything here is something that actually blocked us on hardware and how it was
resolved. Verified results on the device:

```
NATIVE_UFS adopt+read
hcs 00000010->00000011  pmc 00000033->00000011  ssu=00000000
L0 st=00000000 last=03a8bfff bs=00001000 sns=00000000     <- 234.9 GB, 4 KiB blocks
L1..L5 st=00000000 bs=00001000
gpt st=00000000 sig=20494645 54524150                     <- "EFI PART"
```

i.e. the controller is adopted, the device is woken from UFS-PowerDown to Active,
every LUN answers TEST UNIT READY and READ CAPACITY, and the GPT header is read
back from flash. Mounting a root volume is a separate problem (PMGR, see below).

## Contents

| Path | What |
|---|---|
| `docs/UFS-TROUBLESHOOTING-FOR-DEVS.md` | The write-up: four walls, root causes, exact code changes, checklist |
| `patches/0001-shengufs-smmu-takeover.patch` | `SmmuTakeover.h`: take over the firmware's stream mapping instead of panicking |
| `patches/0002-panic-screen-fit-cave.patch` | `xnu-panic-screen.c`: drop the frame-pointer walk so the panic screen fits its cave |
| `tools/split-sparse.py` | Split a large userdata image into `fastboot`-able sparse parts |
| `tools/flash-userdata-parts.ps1` | Flash those parts in order |
| `tools/dump-adt.py` | Dump a `kernel.macho.adt` tree (checks `/cpus`, `arm-io`, `intc`) |

## Quick summary of the four walls

1. **`smmu: conflicting UFS stream mapping`** — the UEFI UFS driver already mapped
   stream id `0x60` to its own context bank. Keep its SMR and repoint the S2CR at a
   bypass bank you program yourself (the Linux `bypass_quirk` approach).
2. **`smmu: reserved context bank not bypass`** — distinguish *reuse* (somebody
   already points at our bank) from *takeover* (we are about to program it).
3. **`CPU configuration in device tree is invalid` @AppleARMCPU.c** — without
   `sheng-smp=1` the native `ArmCpu::probe` returns `nullptr`, the `/cpus` nubs go
   to Apple's `AppleARMCPU`, which requires `/cpus/max_cpus`. Pass
   `sheng-smp=1` + `armcpu-spi-ipi=1`, tag the CPUs with `unixnu,cluster-type`,
   and make sure the GIC node carries `unixnu,adt-name = "intc"` so it reaches the ADT.
4. **Three coloured bars then a hard lock** — 8-core SMP hangs during secondary
   bring-up; `cpus=1` gets past it.

And the one that made all of this observable: `boot-progress=1` is a *deliberately
frozen* diagnostic page, so on-screen counters are not evidence. Turn on
`panic_screen` instead — the renderer is 7 bytes too large for its cave until the
frame-pointer walk is removed (`patches/0002`).

## Applying the patches

```bash
# ShengUfs component (a checkout of component:ShengUfs)
git apply patches/0001-shengufs-smmu-takeover.patch

# UniXNU checkout (both copies of the script exist: scripts/ and the tree copy)
git apply patches/0002-panic-screen-fit-cave.patch
```

## Still blocked: PMGR

`native-ufs-root=1` (publishing the root volume) is refused unless the kernel is
built with `CONFIG_XNU_SHENG_PMGR=y` and carries `_sheng_pmgr_run`, plus
`sheng-pmgr=2` on the command line. The platform-power generator's ABI assertions
(`sizeof == 0x4b8a8`, `nub + 0x88`) are pinned to the sheng donor build and need to
be re-derived for a different prelink layout.

**Reading storage is not the same as mounting root** — this repository only covers
the former.

## Hardware notes

- The ABL leaves the UFS device in PowerDown; a `UfsWakeDxe` in the UEFI firmware
  wakes it before the kernel's `START STOP UNIT` can complete.
- Probe levels matter: `native-ufs-probe=3` replaces the request list and then
  **panics on purpose without ever issuing a read**. Use `5` (wake + TUR + READ
  CAPACITY + GPT), `9` (GPT survey) or `10` (read-only root check).

## License

BSD-2-Clause-Patent, matching UniXNU.
