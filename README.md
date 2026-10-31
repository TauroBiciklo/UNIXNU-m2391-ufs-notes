# m2391（魅族 20 Pro / SM8550）UniXNU 移植进度

> 目标：在魅族 20 Pro（m2391, SM8550 / kalama）上跑起 iOS（iPadOS 17.7.11 / 21H461 用户态）。
> 技术栈：BootShim → Mu-Silicium UEFI → UniXNU → DarwinBoot → XNU。
> 本文只记录**真机验证过**的结论。

---

## 一句话现状

**存储链路已完全打通，iOS 根卷已写入真机并成功挂载，内核初始化跑到 `bsd_init` 后期；当前唯一阻塞是 AMFI 的 "No PMGR" 检查。**

```
stage: pid0  PE_lockdown:kit → kernel_task: bed_init → bed_autoconf → loopattach
             → ether_family_init → net_init_run → initthrd → setcon
"AMFI: No PMGR?\n" @ConfigurationSettings.cpp:235
```

这段画面本身就是证据：能走到 `initthrd` / `setcon`，说明**根设备已发布、根卷已挂载、内核初始化基本完成**。

---

## 已打通（真机验证）

| # | 能力 | 证据 |
|---|---|---|
| 1 | UEFI 启动 + UFS 设备唤醒 | 自建 `UfsWakeDxe` 在 DXE 阶段把 ABL 留在 PowerDown 的设备唤醒 |
| 2 | ShengUfs 接管控制器 | `hcs 0x10 → 0x11` |
| 3 | 设备从 PowerDown 唤醒到 Active | `pmc 0x33 → 0x11`，`ssu=0` |
| 4 | 6 个 LUN 全部可用 | `L0..L5 st=0 bs=0x1000` |
| 5 | 容量读取 | LUN0 = `0x3a8bfff` × 4096 = **234.9 GB** |
| 6 | GPT 读取 | `gpt sig=EFI PART` |
| 7 | iOS 根镜像写入 | 8.74 GiB（OS 5.66 GB + SystemOS 3.07 GB）写入 userdata |
| 8 | 根 manifest 校验 | `manifest status=0 windows=2` |
| 9 | 根卷挂载 | 内核跑到 `bsd_init` 后期（见上） |

---

## 关键技术障碍与解法

### 1. 观测：屏幕读数不能当证据

`boot-progress=1` 是**故意冻结**的诊断页（30 秒无活动或 120 秒上限即定格），
且内核 IRQ-off 硬锁时监视线程也会停摆。

**解法**：开 `panic_screen=true`，让每个 `panic()` 变成屏幕文字。
但它一开始装不进 cave（渲染器 2639 字节 vs 可用 2608，**差 7 字节**）：

```
panic screen is 0xa37 bytes at ..., the padding ends at ...
```

**最终解法**：删掉 `scripts/xnu-panic-screen.c` 里的 **24 层帧指针回溯**，
2639 → **2527 字节**，panic 消息本身保留。

> 这个改动是整条链路能推进的前提——后面四道墙全靠它定位。

### 2. SMMU 流映射冲突

```
NATIVE_UFS smmu: conflicting UFS stream mapping slot=2 smr=80000060 s2cr=2
```

UEFI 的 UFS 驱动早已把 SID `0x60` 映射到它自己的 context bank。
驱动要求"最后一个 bank"并判为冲突。

**解法**（`SmmuTakeover.h`，即 Linux `bypass_quirk` 的做法）：
保留固件的 SMR，把 **S2CR 改指**自己编程的 bypass bank，并区分两种状态：

```cpp
bool reuse;      // 已有人指向我们的 bank
bool takeover;   // 固件映射指向别处，我们要接管
const bool program = !p.reuse || p.takeover;
```

### 3. AppleARMCPU 抢走 /cpus

```
"CPU configuration in device tree is invalid" @AppleARMCPU.c
```

`ArmCpu::probe` 在没有 `sheng-smp=1` 时**直接返回 nullptr**，
nub 落到 Apple 自带的 `AppleARMCPU`，而它要求 `/cpus/max_cpus`。

**解法**：`sheng-smp=1` + `armcpu-spi-ipi=1`；
DTS 每核加 `unixnu,cluster-type`（E/P）；
GIC 节点必须带 **`unixnu,adt-name = "intc"`** 才会进 ADT。

### 4. 三色彩条后硬锁

绿/蓝/品红三条 entry-probe 彩条之后完全不动——8 核 SMP 在次要核心上死等。
**解法**：`cpus=1`（保留 `sheng-smp=1`，只跑启动核）。

### 5. 大镜像写入（8.74 GiB）

| 坑 | 现象 | 解法 |
|---|---|---|
| 主机 fastboot 是 **32 位** | 任何 >4 GB 操作 `std::bad_alloc` | 改走 fastbootd |
| 引导器**不支持 sparse** | 报 `OKAY` 但数据不落地 | 只在 fastbootd 里写 |
| fastbootd 单次上限 **256 MiB** | 超限被拒 | 切成 ≤250 MB 分块 |
| sparse 块头字段顺序 | `Invalid sparse file format` | `(type, reserved, chunk_sz, total_sz)`，`total_blks` 要含跳过块 |

---

## PMGR 进展（当前主线）

AMFI 的 panic 来自 `AppleMobileFileIntegrity.kext`：
它去 `waitForMatchingService("ApplePMGRNub")`，找不到就 panic。

上游的解法（sheng）是 `ShengPlatformPower.cpp`：加载 `com.apple.driver.ApplePMGR`，
用 Apple 自己的工厂函数创建 `ApplePMGRNub` 并注册。

**已验证的好消息**：用我们自己的预链接内核跑 `audit-amfi-pmgr-contract.py` **通过**：

```
PASS: unchanged AMFI read/OR/write and original PMGR provider dispatch
PASS: actual target AppleT8011PMGR slots read 0xffffffff and perform no write
Static evidence only; native platform service integration remains unresolved
```

说明我们的内核与 sheng 的 **AMFI/PMGR 布局一致**（kext 二进制哈希完全相同），
生成器的 ABI 假设对我们也成立。剩下的是把它接进构建并把地址对齐。

---

## 仓库内容

| 路径 | 说明 |
|---|---|
| `docs/UFS-TROUBLESHOOTING-FOR-DEVS.md` | UFS 排障笔记（四道墙的细节，可直接照抄） |
| `docs/STATUS-20261031.md` | 本阶段状态快照与复现步骤 |
| `patches/0001-shengufs-smmu-takeover.patch` | SMMU 接管补丁 |
| `patches/0002-panic-screen-fit-cave.patch` | panic 屏瘦身补丁 |
| `tools/split-sparse.py` | 大镜像切分成 fastboot 可吃的 sparse 分块 |
| `tools/flash-userdata-parts.ps1` | 逐块刷写 |
| `tools/dump-adt.py` | 解析 ADT（检查 /cpus、arm-io、intc） |

---

## 刷写注意（这台机器特有）

```
32 位 fastboot  →  不能整包写大镜像
ABL 不支持 sparse  →  必须进 fastbootd 才有用
fastbootd 单次 256 MiB  →  分块必须 ≤250 MB
```

进 fastbootd 的步骤（fastbootd 在原厂 recovery ramdisk 里）：

```bash
fastboot flash recovery_a <原厂 recovery.img>
fastboot --set-active=a
fastboot reboot fastboot
fastboot getvar is-userspace      # 期望 yes
```

---

## 已知的、还没做的

- **PMGR 集成**：`CONFIG_XNU_SHENG_PMGR=n`，`_sheng_pmgr_run` 尚不存在于内核
- **显示驱动**：`NATIVE_MOBILE_DISPLAY=n`，目前只有 UEFI GOP 文字帧缓冲
- **触摸**：Goodix GT9916，移植树里**没有任何驱动**，需要从零写
- **多核**：当前 `cpus=1`，8 核 SMP 会死锁

参考坐标：同 SoC 的 sheng 跑到过 UI；同为魅族的 m2381 只到 launchd。

---

## 许可

BSD-2-Clause-Patent（与 UniXNU 一致）。
