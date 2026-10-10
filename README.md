# DanmakuRenderForColab

基于 [DanmakuRender v5](https://github.com/SmallPeaches/DanmakuRender) 深度定制的云端直播录制、实时弹幕采集与硬件渲染工具。将极度消耗本地算力与带宽的录制、NVENC 弹幕烧录、合并和备份完全交给 **Google Colab** 云端运行，配合本地 Windows 轻量开播监控，实现全自动的闭环工作流。

```text
[本地开播监控] ──(开播触发)──> [CDP 自动唤醒 Colab 申请 GPU]
                                       │
                                       ▼
                              [云端分段录制 + 弹幕采集]
                                       │
                                       ▼
                            [NVENC 硬件加速弹幕渲染]
                                       │
                                       ▼
                           [Google Drive 双向校验备份]
                                       │
                                       ▼
                          [可选: B 站多 P 自动串行投稿]
                                       │
                                       ▼
                       [下播全场归档 ──> 自动释放 Colab 运行时]
```

---

## 核心特性与技术亮点

- **云端全套流水线**：在 Google Colab 上完成源流录制、弹幕抓取、ASS 生成以及 NVENC 硬件加速渲染，保留原片、ASS 与带弹幕成品。
- **Google Drive 严密校验备份**：每个分段复制到 Drive 后均会读回并校验文件大小与 SHA-256，所有状态与耗时记录在 `manifest.json` 中。
- **B 站多 P 串行投稿（可选）**：首段自动创建新稿件，后续分段按序追加到同一个 BVID；不足 120 秒短段自动跳过投稿但完整备份；具备严格事务日志防重传。
- **算力点省流设计**：日常监控在本地运行，完全无需挂载 Colab GPU；仅在确认开播后通过专用浏览器 CDP 申请 GPU 并点击“全部运行”；任务收尾后自动请求 `runtime.unassign()` 释放运行时，不烧闲置 CU。
- **工程级可靠性**：支持优雅受控停止、断点按运行编号补传（`colab_upload.py`）以及异常任务媒体补存。通过 88 项离线自动化测试。

---

## 核心脚本速查

| 脚本 / 入口 | 运行环境 | 职责说明 |
|---|---|---|
| [`start_colab_monitor.cmd`](start_colab_monitor.cmd) | 本地 Windows | **推荐**。交互式配置向导与后台监控脚本，开播后自动拉起浏览器并触发云端任务 |
| [`colab_monitor.py`](colab_monitor.py) | 本地 Windows / CLI | 本地监控核心逻辑，管理专用浏览器 Profile 与轮询开播状态 |
| [`colab_trigger.py`](colab_trigger.py) | 本地 CLI | 底层触发脚本，负责向 Drive 写入任务控制信号并通过 CDP 操作 Notebook |
| [`notebooks/colab_record.ipynb`](notebooks/colab_record.ipynb) | Google Colab | 云端运行的 Jupyter Notebook 宿主，组织环境准备、预检与任务执行 |
| [`colab_run.py`](colab_run.py) | Colab 容器内 | 云端核心协调器，调度 DMR 下载器、渲染队列、备份流水线与投稿事务 |
| [`colab_upload.py`](colab_upload.py) | Colab 容器内 | 独立补传工具，用于为已关闭但未完成投稿的历史任务继续追传分 P |

---

## 前置准备：Google Drive 目录树速查

本工具在 Colab 运行时将所有配置、凭据与产物均保存在您的私有 Google Drive 中。请确保您的 Google Drive 根目录具有如下结构：

```text
Google Drive (我的云端硬盘)
└── DMRColab/
    ├── credentials/
    │   ├── bilibili.json           # [必填] 观看与取流 Cookie (格式见下文)
    │   └── bilibili_upload.json    # [可选] 投稿专用 Cookie (启用投稿时需要)
    ├── config/
    │   └── upload.yml              # [可选] 投稿元数据配置 (由 colab_support/upload.example.yml 复制)
    ├── control/                    # [自动维护] 本地监控与 Colab 握手通信目录 (request/claim/ack)
    └── runs/<run_id>/              # [生成成果] 单场任务输出
        ├── source/                 # 录制原片分段
        ├── danmaku/                # 弹幕 ASS 文件
        ├── rendered/               # NVENC 渲染后的带弹幕成品分段
        ├── merged/complete.mp4     # 全场归档合并视频 (仅用于归档)
        ├── manifest.json           # 录制、渲染、备份全流程状态与校验值汇总
        ├── upload.json             # 投稿事务与远端分 P 顺序记录
        └── events.log              # 白名单审计事件日志
```

### 1. 准备观看凭据（必需）
将您的 B 站观看登录 Cookie JSON 放置在私有 Drive 的：
`MyDrive/DMRColab/credentials/bilibili.json`

> **要求**：JSON 需包含 `cookie_info.cookies` 列表，且含有非空的 `SESSDATA`。程序不会调用任何登录工具或写入 refresh token。

### 2. 准备投稿配置与凭据（可选）
若需开启自动多 P 投稿：
1. 复制 [colab_support/upload.example.yml](colab_support/upload.example.yml) 到 Drive 的 `MyDrive/DMRColab/config/upload.yml`。
2. 填写账号校验 `expected_uid`、分区 `tid`、转载属性 `copyright`、标题模板、简介和标签，并设置 `enabled: true`。
3. 将包含 `SESSDATA` 和 `bili_jct` 的投稿凭据放置在 `MyDrive/DMRColab/credentials/bilibili_upload.json`。

---

## 快速上手

### 第一步：设置 Google Colab Notebook

1. 打开 [notebooks/colab_record.ipynb](notebooks/colab_record.ipynb)，在 Colab 菜单中选择 **“文件” -> “在云端硬盘中保存一份副本”**。
2. 在副本的 **“修改” -> “笔记本设置”** 中，将硬件加速器设置为支持 NVENC 的 **GPU**（如 T4）。
3. 建议先使用手动模式做一次短试跑：
   - 在 Notebook 顶部参数表单中设置 `ROOM_URL` 为目标直播间地址。
   - 设置 `SEGMENT_SECONDS = 300`（5分钟一段）、`MAX_RECORD_HOURS = 0.2`。
   - 依次执行各单元格，熟悉流程并验证 Google Drive 挂载与成品生成。

---

### 第二步：配置本地检测开播与自动触发（Windows）

1. **安装本地轻量依赖**：
   ```powershell
   python -m pip install -r colab_support/monitor_requirements.txt
   ```
   > ⚠️ **避坑提示**：请勿在本地执行 `pip install -r requirements.txt`。完整版依赖包含用于其他平台的 `quickjs`，在 Windows + Python 3.13 下缺少 prebuilt wheel 会导致 C 扩展编译失败。本地监控仅需安装上述轻量的 `monitor_requirements.txt`。

2. **初始化配置向导**：
   在仓库根目录下运行：
   ```powershell
   .\start_colab_monitor.cmd --setup
   ```
   按照向导提示输入：
   - 任务配置模板（如 `configs/DMR-example.yml`，程序将自动读取其中的直播间地址）；
   - Google Drive for desktop 本地同步目录（例如 `G:\我的云端硬盘` 或 `G:\My Drive`）；
   - 您的私有 Colab Notebook URL（形如 `https://colab.research.google.com/drive/...`）；
   - 专用浏览器程序路径（自动探测 Chrome / Edge）。

3. **开始日常自动监控**：
   配置完成后，日常使用只需双击运行 `start_colab_monitor.cmd` 即可挂机：
   - 本地程序将以低资源消耗轮询直播间状态；
   - 确认开播后，自动启动带独立 Profile 的浏览器，通过 CDP 打开您的 Notebook 并点击一次“全部运行”；
   - 云端认领任务并执行录像、渲染与归档；
   - 自动模式在持久化全部数据到 Drive 后，会自动调用 `google.colab.runtime.unassign()` 释放运行时，本地等待下播确认后重新进入警戒状态。

---

## 异常恢复与按运行编号补传

若某次运行中遇到网络抖动或 B 站 API 异常导致部分分 P 未成功追加，但视频和 ASS 已安全备份在 Drive 中：

在挂载好 Drive 的 Colab 环境中执行：
```bash
python colab_upload.py --run-id <run_id> --drive-root /content/drive/MyDrive
```

- 补传程序会自动校验原片、ASS 和成品三个备份的 SHA-256，核对已有稿件的远端分 P 序列，并自动跳过已提交的段，安全继续追传剩余分 P。
- 详细机制与状态核对见 [Colab 详细规范文档](docs/colab.md)。

---

## 离线单元测试

在提交代码或修改逻辑前，可直接在本地运行离线测试套件：

```bash
python -m unittest discover -s checks -p "test_colab*.py" -v
```

离线测试使用模拟 API、Mock 浏览器和合成媒体流，覆盖了状态机转移、Cookie 隔离、短段跳过、去重与并发防护等 88 项测试用例。

---

## 常见问题与避坑预警 (FAQ)

1. **Google Drive 首次挂载授权弹窗**：
   - 首次在 Colab 运行 Notebook 时，Google 会弹出“允许此笔记本访问您的 Google 云端硬盘文件吗？”的安全确认对话框。**该对话框必须由您本人在网页中手动点击一次“连接到 Google 云端硬盘”**。CDP 自动脚本出于安全与接口限制不会也不可能替您代点授权。

2. **本地专用浏览器环境隔离**：
   - `start_colab_monitor.cmd` 启动的 Chrome/Edge 使用位于 `.temp/colab-browser-profile/` 的独立用户目录，绝不会侵入或接管您的日常浏览器。首次通过向导唤醒时，请在该专用浏览器窗口中登录好 Google 账号并打开保存好的 Notebook。

3. **Colab 运行时配额与算力点**：
   - 监控期间您的 Notebook 应处于**未连接**状态（不消耗任何算力时长）。开播后脚本点击“全部运行”才会向 Google 申请 GPU 实例。
   - 录制和渲染结束后，程序会自动发出释放运行时的请求并持久化退出回执。

4. **Colab 临时磁盘 `/content` 被重置**：
   - Colab 运行时的本地临时磁盘文件会在实例断开后随之清空。所有生成的原片、弹幕、成品与日志均在第一时间双向校验并同步写入到您的 Google Drive 中，请直接在 Google Drive 中查验和下载录播成品。

---

## 开源溯源、许可证与致谢

- 本仓库 **DanmakuRenderForColab** 基于开源项目 [SmallPeaches/DanmakuRender](https://github.com/SmallPeaches/DanmakuRender) 的 `v5` 分支进行定制开发。
- 直播取流、弹幕通信、ASS 写入及核心渲染模块复用并适配了原有 DMR 架构；新增了 Colab 云端协调流水线、Google Drive 校验备份、B 站多 P 追加事务日志、本地 CDP 监控触发及补传机制。
- 感谢上游作者及核心依赖的杰出工作：`THMonster/danmaku`, `wbt5/real-url`, `ForgQi/biliup`, `ForgQi/stream-gears`。
- 上游代码未声明特定开源许可证（保留“本程序仅供研究学习使用！”说明）。本 fork 不替原作者做额外授权，相关代码版权归原作者所有。

**本程序仅供研究学习交流使用！**
