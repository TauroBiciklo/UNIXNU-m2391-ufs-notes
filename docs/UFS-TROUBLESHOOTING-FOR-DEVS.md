# m2391 / SM8550 UFS 驱动排障笔记（给同机型的开发者）

> 环境：Meizu 20 Pro（m2391, SM8550 / kalama），UniXNU 栈（BootShim → Mu-Silicium UEFI → UniXNU → XNU），
> 存储驱动 **ShengUfs**，内核 iPadOS 17.7.11 (21H461)。
> 下面每一条都在真机上验证过；标注「已验证」的是屏幕/复位实证，「推断」的请自行复核。

---

## 0. 先建立可观测性（不然后面全是猜）

**症状**：UFS 驱动看起来"没启动"，但屏幕上的诊断页数字永远不动、永远 0。

原因有两层：

1. `boot-progress=1` 是**故意冻结**的诊断页：30 秒无阶段/存储活动、或到 120 秒上限就定格供拍照。
2. 即使换成 `boot-progress=2`，一旦内核 IRQ-off 硬锁，监视线程也停摆，画面还是最后一帧。

**结论：这台机器上屏幕读数不能当运行状态证据。**

**解法：用 panic 屏当观测工具**。`[payload] panic_screen = true` 后，任何 `panic()` 都会把文本画到屏幕。

但我们一开始开不起来，报：

```
panic screen is 0xa37 bytes at 0xfffffff00a1645c0, the padding ends at 0xfffffff00a164ff0
```

即渲染器 2639 字节，cave 只有 2608 —— **差 7 字节**。

试过但无效：`--icf=all --gc-sections`（能省 8 字节，但会把 `panic_screen_common` 回收，
加 `--undefined=` 保住又回到原尺寸）、`-fno-inline`、`-fmerge-all-constants`、关矢量化、把 cave 起点前移（撞到 stage 代码）。

**有效解法**：删掉 `scripts/xnu-panic-screen.c` 里的 **24 层帧指针回溯**（`text(&g, "\nframes")` 那个块）。
panic 消息本身保留，尺寸 2639 → **2527 字节**，余量 81。

```diff
-    {
-      const u64 *fp = (const u64 *)__builtin_frame_address (0);
-      text (&g, "\nframes");
-      ...
-    }
+    /* 删掉帧回溯以适配 cave 尺寸；panic 文本不受影响 */
```

注意：脚本有两份（`scripts/` 仓库副本 + `tree/scripts/` 树副本），**两边都要改**。

---

## 1. 第一道墙：SMMU 流映射冲突

panic 原文（note：这行会被屏幕宽度折行，`smr=8` + 下一行 `00006060` 合起来才是 `0x80000060`）：

```
NATIVE_UFS smmu: conflicting UFS stream mapping slot=2 smr=8
00006060 s2cr=2 @NativeUfs.cpp:309
```

**含义**：UEFI 固件的 UFS 驱动（UFSDxe）**早就把 SID 0x60 映射到它自己的 context bank（bank 2）**了。
ShengUfs 的 `planUfsSmmuBank()` 要求"最后一个 bank"并且要求现有映射就指向它，否则判定冲突并 panic。

**关键认知**：这不是"要清理固件残留"，而是**要接管**。Linux 在同类固件上的做法（`arm-smmu-qcom.c` 的 bypass_quirk）
也是：保留 SMR（SID 已经是对的），把 **S2CR 改指到自己的 bypass bank**。

**改动**（`drivers/ShengUfs/SmmuTakeover.h`）：

1. `UfsSmmuPlan` 加一个状态位：

```cpp
bool reuse = false;
bool takeover = false;   // 固件已映射我们的流，但指向别的 bank
```

2. 规划器：SID 匹配且不是我们的 bank → 接管，而不是报冲突：

```cpp
if (match) {
  ++matches;
  if (matches > 1 || mask) {          // 只有"多条匹配"和"带 mask"才是真冲突
    p.error = "conflicting UFS stream mapping"; return p;
  }
  p.slot = i; p.reuse = true;
  if (!ourBank) { p.smr = smr; p.s2cr = s2cr; p.takeover = true; }
}
```

3. 配置器：接管也要**自己编程 bank**，但保留固件的 SMR：

```cpp
const bool program = !p.reuse || p.takeover;
if (program) write(cb, 0);                          // SCTLR = 0
if (program) write(0x1000 + 4 * p.bank, 0x10000);   // CBAR = BYPASS
if (!p.reuse || p.takeover) {                       // S2CR 指向我们
  write(0xc00 + 4 * p.slot, p.bank);
}
if (!p.reuse) {                                     // SMR 只在全新分配时写
  write(0x800 + 4 * p.slot, 0x80000000U | stream);
}
```

## 2. 第二道墙：`reserved context bank not bypass`

```
NATIVE_UFS smmu: reserved context bank not bypass slot=2 smr=80000060 s2cr=2
```

规划器尾部有个校验：`p.reuse` 时要求目标 bank **已经是 bypass 状态**。
但接管场景下 bank 是我们即将编程的，当然还没配好。

**解法**：该校验只在"纯复用"时生效：

```cpp
if ((sctlr & 1) || (p.reuse && !p.takeover && (sctlr || CBAR != 0x10000 || CBA2R...))) {
  p.error = "reserved context bank not bypass";
}
```

过了这两道，SMMU 就通了（真机验证：后面能读到盘）。

---

## 3. 第三道墙：`CPU configuration in device tree is invalid` @AppleARMCPU.c

这道墙和 UFS 无关，但会**挡住整个启动**，看起来像"UFS 之后又挂了"。

**根因**（`drivers/ArmCpu/ArmCpu.cpp` 的 `probe`）：

```cpp
PE_parse_boot_argn ("sheng-smp", &smp, sizeof (smp));
if (!smp) {
  return nullptr;      // 次要核心启动是 opt-in
}
```

**没传 `sheng-smp=1` 时，原生 ArmCpu 直接放弃匹配** → `/cpus` nub 落到内核自带的
`AppleARMCPU`（来自 AppleARMPlatform.kext）→ 它要求 `/cpus/max_cpus`，我们没有 → panic。

**解法**（三件一起做）：

1. 启动参数：`sheng-smp=1` + `armcpu-spi-ipi=1`（后者是 sheng 的 IPI 方式，见 `docs/zh/devices/sheng.md`）；
2. DTS 每个 CPU 加集群类型（E/P），ADT 会生成 `cluster-type` 属性：

```dts
cpu@0 { device_type = "cpu"; unixnu,cluster-type = "E"; ... };   // cpu0-2
cpu@3 { device_type = "cpu"; unixnu,cluster-type = "P"; ... };   // cpu3-7
```

3. **GIC 节点必须导出到 ADT**：

```dts
intc: interrupt-controller@17100000 {
  compatible = "arm,gic-v3";
  unixnu,adt-name = "intc";          // ← 少了这一行它就不会进 ADT
  reg = <0x0 0x17100000 0x0 0x10000>, <0x0 0x17180000 0x0 0x200000>;
  #interrupt-cells = <4>;
  #address-cells = <0>;
  interrupt-controller;
  redistributor-stride = <0x0 0x40000>;
};
```

验证方法：payload 里的 `kernel.macho.adt` 用 `mcopy` 取出，解析后应看到
`arm-io → intc + ufshc + ufsphy`（少 intc 就是没导出）。

---

## 4. 第四道墙：三色彩条之后硬锁（8 核 SMP）

现象：屏幕只有绿/蓝/品红三条彩条（entry probe 信号：绿=退出 BootServices，蓝=内核入口，
品红=MMU 打开前），之后完全不动、不重启、不 panic。

**原因**：8 核 SMP 启动在次要核心上死等（GIC/redistributor 配置或 IPI 路径）。

**解法（临时但有效）**：`cpus=1`（保留 `sheng-smp=1` 让 ArmCpu 拿到 nub，但只跑启动核）。

过了这一步，UFS 就能一路跑到读盘。

---

## 5. 成功判据（本机实测输出）

`native-ufs-probe=5` 的收尾 panic 打出了这些：

```
NATIVE_UFS adopt+read
hcs 00000010->00000011  pmc 00000033->00000011  ssu=00000000
L0 st=00000000 last=03a8bfff bs=00001000 sns=00000000
L1..L5 st=00000000 bs=00001000
gpt st=00000000 sig=20494645 54524150       ← "EFI PART"
```

含义：

| 字段 | 说明 |
|---|---|
| `pmc 0x33 → 0x11` | 设备从 UFS-PowerDown 唤醒到 Active（SSU 成功） |
| `ssu=0` | START STOP UNIT 无错误 |
| `L0 last=0x3a8bfff bs=0x1000` | LUN0 = 61,390,847 × 4096 = 234.9 GB |
| `gpt sig=EFI PART` | GPT 头真的从闪存读回 |

---

## 6. 探针档位（很容易用错）

`NativeUfs.cpp` 的 `adoptAndRead(level)`：

| `native-ufs-probe=` | 行为 |
|---|---|
| 3 | 换请求列表后 **panic 打寄存器**，**从不读盘** ← 最容易踩的坑 |
| 4 | `TEST UNIT READY lun0` 后 panic |
| **5** | **唤醒设备（SSU → WLUN 0xD0）+ 逐 LUN `TUR` + `READ CAPACITY` + GPT 头读取**，结束后 panic 打表 |
| 9 | 在 5 的基础上做 GPT 分区普查 |
| 10 | 只读 root 检查：GPT → `userdata` 分区 → 读首块 → 校验 manifest |

**很多"驱动没工作"的误判，其实只是用了 probe=3。**

---

## 7. 固件前提：UFS 必须先在 UEFI 阶段被唤醒

ABL 会把 UFS 设备留在 PowerDown。我们的固件里加了 `UfsWakeDxe`（在 `boot_b` 的 UEFI 里），
由它在 DXE 阶段把设备唤醒，内核的 `SSU` 才能真正完成。

没有这个驱动时，`adoptAndRead` 会在唤醒/TUR 阶段失败。

---

## 8. 环境与工程纪律（踩出来的）

1. **`unixnu tree` 用硬链接**：改 `tree/drivers/...` 等于改 `out/feeds/...`。
   - 后果：补丁会"叠加"，不是替换。我们因此白推了一整轮（多个复位点同时存在，手机在最靠前的那个就复位了）。
   - 恢复原始源：`git -C out/feeds/<component> checkout -- <file>`。
2. **一次构建只装一个复位传感器**，并断言数量：

```bash
grep -c 0x84000009 <driver>.cpp    # 必须恰好为 1
```

3. **`tree/scripts/*.py` 与仓库 `scripts/*.py` 是两份**，改补丁脚本要两边都改。
4. **离线构建**（避免 prelink 去连 Apple CDN）：

```bash
./unixnu build kernel -D <device> -j10 --kernelcache out/ipsw/21H461/kernelcache.decompressed
```

5. 长命令写成 `.sh` 再 `bash <file>`，不要用 PowerShell 内联多行 bash（会静默丢输出）。
6. 真实可用的符号工具是 `/usr/bin/llvm-nm`（有些环境里 `/opt/.../llvm-nm` 并不存在，
   会让"符号未定义"的结论全部失效）。

---

## 9. 如果需要刷 userdata（iOS 根镜像）——本机 fastboot 的三个硬限制

| 事实 | 影响 |
|---|---|
| 主机 `fastboot.exe` 是 **32 位** | 任何 >4 GB 的操作 `std::bad_alloc` |
| **ABL 不支持 sparse** | 报 `OKAY` 但**数据不落地**（首块一直是旧数据） |
| **fastbootd 单次下载上限 256 MiB** | 分块必须 ≤250 MB |

可复用的做法：

```bash
# 切成 ≤250 MiB 的 sparse 分块，块首用 DONT_CARE 编码本块在分区中的偏移
python3 split-sparse.py userdata-root.img parts/
# 进 fastbootd（fastbootd 在原厂 recovery ramdisk 里）
fastboot flash recovery_a <原厂 recovery.img>
fastboot --set-active=a && fastboot reboot fastboot
fastboot getvar is-userspace        # 期望 yes
# 逐块刷
fastboot flash userdata parts/userdata.partNN.sparse   # 循环
```

**sparse 块头字段顺序必须是** `(type, reserved, chunk_sz, total_sz)`
—— 写反会被判定 `Invalid sparse file format`。`total_blks` 要**包含被跳过的前导块**。

验证：`native-ufs-probe=10` 应打印

```
block0 magic 474e4853 544f4f52    ("SHNGROOT")
manifest status=0 windows=2
```

---

## 10. 目前仍卡住的地方（供参考）

`native-ufs-root=1`（真正发布根卷）在上游 payload 检查里要求：

- `sheng-pmgr=2` 启动参数；
- 内核里有 `_sheng_pmgr_run` 符号；
- `CONFIG_XNU_SHENG_PMGR=y`。

我们这边 PMGR 仍是关闭状态（`CONFIG_XNU_SHENG_PMGR=n`，符号数为 0），
且 `build-sheng-platform-power.py` 生成的头文件里，`sizeof == 0x4b8a8`、`nub+0x88`
这类断言是**钉在 sheng 的捐赠构建**上的，需要按自己机器的 prelink 布局重算。

**所以：UFS 读到数据 ≠ 能挂根。** 前者我们已经打通，后者卡在 PMGR。

---

## 附：建议的排查顺序（新移植的同学可以直接照抄）

1. 开 `panic_screen`（先解决 7 字节问题）→ 让所有失败可见
2. 用 `native-ufs-probe=5` 跑一次，看 panic 表
3. 若卡在 SMMU → 做 takeover（第 1、2 节）
4. 若卡在 AppleARMCPU → `sheng-smp=1` + cluster-type + intc 导出（第 3 节）
5. 若停在彩条 → `cpus=1`（第 4 节）
6. 看到 `pmc 0x33->0x11` 与 `gpt sig=EFI PART` → 存储链路通了
7. 再谈 userdata 与 PMGR
