# NVIDIA GPU 性能监测

**工作台页面**：展开首页「GPU 性能」，从采集记录中选取一次观测。记录属于整张设备（当前面板为 GPU 0），不会被记作当前选中任务的指标；对照训练阶段请看采集时间。展开、切换任务或打开浏览器均不自动采样。CLI 与页面共用同一套硬件报告读数及单位换算。

| 页面操作 | 行为 |
|---|---|
| 查看记录 | 读取当前工作台数据目录的 `gpu/` 和 `gpu/panel/`。硬件报告显示样本均值、极值和样本数；CSV 显示最近 30 条及忙时曲线。历史 SQLite 按选择读取，不随任务轮询反复解析。|
| 开始趋势 / 停止趋势 | 每 5 秒读一次 `nvidia-smi`，可手动停止，最多 30 分钟。关闭折叠区或浏览器不会中断；关闭管理服务会停止趋势。正式任务自行记录已有等价字段时直接复用，不必两边同时采。|
| 采集硬件短窗 | 一次 10 秒、100 Hz，选择设备默认或 GB20x 指标集。先在有代表性的稳定阶段取一窗，出现吞吐变化或需要对比阶段时再取；不连续滚动采集。不会主动提升权限。普通账户如遇权限错误，只用管理员终端执行面板给出的独立命令，再点「刷新记录」。|

**保留范围**：页面创建的 CSV、SQLite 和 `.nsys-rep` 单独放在 `gpu/panel/`。只清理此目录内符合采集命名的文件：超过 14 天后，下次访问/启动采集时清理（最多每日检查一次）。已有面板记录合计达到 512 MiB 就暂停新硬件窗口，单次正在导出的文件可能越过该阈值。值得长期保留的窗口请把同名 `.sqlite` 和 `.nsys-rep` 复制到上一级 `gpu/` 或实验归档；上一级的 CLI 文件、旧报告、训练日志和其他命名的文件不参与清理。

**开销**：页面折叠不轮询 GPU，关闭采集不会运行 GPU 查询。硬件窗口可能干扰训练时序；需要量化对步速的影响时，应另做相同阶段有/无采集对照。Windows 取数和导出成本依设备与驱动而异。无需在已有 NVIDIA 工具之外安装常驻监控器。

**独立命令行**：独立终端在工作台根目录运行。`gpu_watch.py` 只调用本机 NVIDIA 工具，不占用任务槽，也不需要更改训练代码。一个采集会话只选一张 GPU；默认索引 0。可用 `nvidia-smi -L` 核对。文件默认写入本机 `data/gpu/`，不进入 Git；用 `--out-dir` 可放进对应尝试的产物目录。记录开始时间和训练阶段，以便与步耗时对照。日常监测无需安装 Python 包。

| 想回答的问题 | 工具与范围 | 读数及其边界 |
|---|---|---|
| 训练期间是否经常出现空档？还有多少显存余量？ | `trend`：整卡，每 2 秒一行原生 CSV | GPU 忙时占比、显存读写忙时占比、已用/总显存 MiB、功耗 W、温度 °C、SM/显存时钟 MHz。显存读写忙时占比不等于 GB/s；GPU 忙也不能推出多少 SM 在工作。`N/A` 原样保留。|
| 算力用在什么地方？显存实际传输多快？ | `counters`：Nsight Systems，默认 10 秒、100 Hz，最长 60 秒 | 按该芯片的指标集读取 SM 活跃、发射、Tensor 管线和显存读写；百分比、占用率和 GB/s 依各栏单位分别解释。仍是整卡，包含显示及其他进程。|
| 哪个算子造成这个现象？ | Nsight Compute：单独运行的代表性前检，只选命名 kernel | 可以读单个 kernel 的计数器；可能重放内核，不能把剖析耗时当正式步耗时。|

**常规趋势**

```powershell
.\.venv\Scripts\python.exe gpu_watch.py trend --interval 2
```

`Ctrl+C` 停止采集。也可加 `--seconds 30` 限时。训练已有等价记录时直接复用；若需要额外字段，可将这条命令作为单独观察使用。CSV 自带本机时间、设备标识和单位；同一轮对照用相同采样周期。2 秒间隔不能解析毫秒级停顿。

**硬件计数器短窗**

```powershell
.\.venv\Scripts\python.exe gpu_watch.py check
.\.venv\Scripts\python.exe gpu_watch.py counters --seconds 10
```

上例使用 Nsight 为当前设备选择的默认指标集。以下是 GB205 的示例；设备支持时，可加 `--metric-set gb20x-top` 读取显存 GB/s。采集只启动一个等待结束的 CPU 计时进程，不附加训练或采集系统 API/CPU 调用栈。报告为 `.nsys-rep`（用 Nsight Systems 打开）及 `.sqlite`。终端显示关键指标的样本均值、最小值、最大值和独立的 samples 列。已有文件用 `python gpu_watch.py summary <报告.sqlite>` 只读复看，无需管理员权限或重新采集。SQLite 中 GB20x-top 的 VRAM GB/s 原值为 sector/s，确认报告内嵌显示倍率后才换算（本版为 32×10⁻⁹）；原始全量指标保留在文件中；权限错误或无有效记录时不能报为取得计数。首次获权后，先在稳定训练段取一窗核实字段与单位；如需衡量观察本身的影响，另作同阶段有/无采集步耗时对照，不假定零开销。

| GB205 示例指标集 | 优先查看 |
|---|---|
| `gb20x-top` | `SM Issue Stage Throughput`、`SM Tensor Pipe Active`；`VRAM Read Bandwidth`、`VRAM Write Bandwidth`、`VRAM Total Bandwidth` 均有 **GB/s** 子项（十进制）。注意选择正确的子项，而非同名的百分比。|
| `gb20x` | `SMs Active`（SM 至少有一个 warp 在途的周期占比，跨 SM 平均）、`SM Issue`、`Tensor Active`、`Compute Warps in Flight`。本集合的 `DRAM Read/Write Bandwidth` 仅列 **%**。|

这两套集合要分窗采集，对照相同训练阶段，不能当作同一时刻。Tensor 管线活跃不等于已达到峰值 FLOP/s；整体看不出某一个 CUDA 核心的负载，也不能按进程分离桌面显示。需要辨认 CPU 同步空洞时，另用前检的 PyTorch Profiler / Nsight Systems CUDA 时间线。

若 `check` 返回 `Insufficient privilege` / `ERR_NVGPUCTRPERM`，先只在有管理员权限的采集终端重试 `check`；训练和工作台服务继续按原权限运行。不必为此永久开放普通用户权限。若确需长期允许普通账户访问，NVIDIA 驱动的“管理 GPU 性能计数器”是**系统级设置**，由机器管理员决定。工具不会修改驱动设置。

**逐 kernel 核查**（可选）：在有权限的独立前检中，用 Nsight Compute 的 `--kernel-name`、`--launch-count 1` 限定范围。已在 GB205 离线目录核对的 `--metrics` 包括 `sm__cycles_active.avg.pct_of_peak_sustained_elapsed`、`sm__warps_active.avg.pct_of_peak_sustained_active`、`sm__pipe_tensor_cycles_active.avg.pct_of_peak_sustained_elapsed`、`dram__bytes_op_read.sum.per_second` 和 `dram__bytes_op_write.sum.per_second`。后两项为 byte/s，除以 10⁹ 得 GB/s。不要同时运行两种 Nsight 采集器，也不要将全轮正式训练包在 `ncu` 中。

**选型依据**：nvitop 适合交互查看 NVML 整卡/进程概况；htop 关注主机进程。监视界面不能授予缺失的硬件计数器权限。旧版 `nvprof` 不支持计算能力 8.0 及以上设备。这里使用驱动自带的 `nvidia-smi`，以及可选的 NVIDIA Nsight 工具；不引入常驻服务。

原始资料：[NVIDIA SMI 字段定义](https://docs.nvidia.com/deploy/nvidia-smi/index.html)、[Nsight Systems GPU Metrics](https://docs.nvidia.com/nsight-systems/2025.6/UserGuide/index.html#gpu-metrics)、[Nsight Compute 内核重放与计数器](https://docs.nvidia.com/nsight-compute/ProfilingGuide/index.html)、[Windows 计数器权限说明](https://developer.nvidia.com/ERR_NVGPUCTRPERM)、[nvprof 支持范围](https://docs.nvidia.com/cuda/archive/12.9.1/profiler-users-guide/index.html)、[nvitop 项目](https://github.com/XuehaiPan/nvitop)。具体 GB20x 单位以安装目录 `target-windows-x64/GpuMetrics/{gb20x,gb20x-top}.config` 为准。
