# Colab 单场录制、自动触发与多 P 投稿

流程：本地检测开播 → 打开指定 Notebook 并点击一次全部运行 → 每小时录制一段 → 渲染、分段备份 → 向同一 B 站稿件追加分 P → 下播后完成媒体归档、投稿收尾和结果汇总。

首版限定一个直播间、一个投稿账号、一个活动运行时。自动模式保存结果后请求释放运行时，手动模式保留运行时；不自动清理 Drive 媒体；原来的 main.py 和本地配置继续使用原路径。正式 Notebook 设置为 GPU 类型即可，监控期间无需提前连接运行时。

## 来源与许可证状态

本改造基于 [SmallPeaches/DanmakuRender](https://github.com/SmallPeaches/DanmakuRender) 的 v5 分支，直接复用或适配：

- DMR/LiveAPI：直播状态、取流和弹幕协议。
- DMR/Downloader：StreamDownloadTask、FFmpeg 录制、ASS 写入；SingleSessionDownload 和 ManagedFFmpeg 补充单场生命周期和停止屏障。
- DMR/Render：现有 Render 和弹幕烧录。
- DMR/Uploader/biliwebapi.py：网页投稿、稿件查询及 UPOS 上传。新增显式 readonly_cookies 路径、媒体单独上传及全部分块结果检查；桌面默认登录路径保留。
- DMR/Config/default.yml、DMR/utils：默认参数、PipeMessage 和共享类型。

colab_support、colab_run.py、colab_trigger.py、colab_upload.py 和 Notebook 负责云端协调、环境检查、备份、事务日志、自动触发和恢复。

截至 2026-10-10，此前检查的上游 v5 根目录及本地版本未发现 LICENSE、COPYING 或 NOTICE 文件；上游 README 包含“本程序仅供研究学习使用！”说明。本改造保留来源和贡献者致谢，不把原代码声明为 MIT、Apache、GPL，也不替原作者重新授权。第三方依赖许可证需分别核对。

## 实现和验收边界

| 能力 | 状态 |
|---|---|
| 单场录制、弹幕、中文渲染、Drive 分段备份和合并 | 已实现；用户此前报告云端录制、备份和合并可执行 |
| 本地 CLI 自动触发、状态锁、请求认领、回执和 Notebook 顺序防护 | 已实现，离线验证；真实浏览器与 Drive 同步链待验收 |
| B 站首 P 创建、后续追加、投稿事务日志和补传 | 已实现，模拟 API 验证；真实投稿待验收 |
| 自然下播、真实账号两个 P、审核和公开状态 | 本轮未执行；submitted 不表示审核通过或已公开 |
| 开播后 Run all 连接、Notebook 内部释放运行时 | 新增实现，需真实 Colab 验收 |
| 自动删除 Drive 媒体、跨运行时续录、多直播间 | 不在本版范围 |

新增行为不能凭离线测试认定已完成云端验收。首次授权、登录凭据和私有配置由用户准备。本地正式任务在验收前继续按原配置运行。

## 1. 源码与观看凭据

分支为 codex/colab-poc，远程 colab 指向 zaixiakongyiji/DanmakuRenderForColab，origin 保留上游。提交时只纳入相关源码、无输出的 Notebook、测试和文档；不纳入个人配置、Cookie、录播、日志或会话文档。Git 提交和推送默认由用户执行；本次发布已由用户明确授权代理执行。

Notebook 默认从 fork 的 codex/colab-poc 拉取，清单记录实际 commit SHA。已有检出不会自动覆盖运行中的代码；更新前结束活动任务，再手动拉取或换新运行时。Drive 中另存的 Notebook 不会随 Git 更新，请重新打开仓库版本并保存私有副本。

手动将已有观看登录 JSON 放到私有 Drive：

```text
MyDrive/DMRColab/credentials/bilibili.json
```

必须包含 cookie_info.cookies 列表和非空 SESSDATA。只复制 Cookie 到临时盘 private 目录，不复制 refresh token，不运行 biliup 登录或续期。文件缺失或错误时停止预检。每次预检及正式运行启动时，使用观看凭据调用 B 站官方 nav 接口，只读核验登录状态；登录失效时停止并提示更新观看凭据，网络或接口异常导致无法确认时也停止并提示重试，不静默降级为未登录录制。此检查不自动刷新或替换凭据，也不保证直播源提供原画；清单保留实际 quality 和 stream_type。

## 2. Notebook 与环境

依次执行顶部表单、Drive 授权、源码准备、创建会话并认领、安装依赖、预检、运行和结果检查。Drive 挂载不再等待 apt-get/pip；源码 clone 后立即认领，依赖安装和预检期间每 30 秒刷新 accepted 回执及准备阶段。创建会话失败、安装失败或预检失败时不能沿用旧 COMMAND 运行。预检及正式运行都检查环境，成功后才能录制；已运行会话不能再次直接运行。

请求仍须在生成后 15 分钟内认领。若运行时分配、Drive 挂载或源码下载本身超过此窗口，Notebook 显示恢复提示；不会延长旧请求、二次认领或自动重发。源码准备只使用标准库，认领不依赖 pip 安装。安装失败或中断会记录 failed 回执并按现有最终保存与释放流程处理。

依赖位于 colab_support/requirements.txt，无需安装投稿登录 CLI。安装 FFmpeg、Noto Sans CJK SC 和 fontconfig，实测两秒视频、中文 ASS、字幕滤镜与 H.264 NVENC。失败停止，不回退 CPU 编码；字体形状和音画同步还需播放检查。

| 参数 | 默认值 | 含义 |
|---|---|---|
| --url | 必填 | 单个标准 B 站直播间 URL |
| --segment | 3600 秒 | 每个渲染分段对应一个 P，实际边界依源流关键帧 |
| --initial-wait | 900 秒 | 初始未开播等待上限 |
| --offline-grace | 180 秒 | 连续确认下播，查询失败会清空连续计时 |
| --max-record | 43200 秒 | 本场录制上限 |
| --drain-timeout | 7200 秒 | 尾段、渲染、合并及媒体备份总预算 |
| --min-free-gib | 10 GiB | 临时盘保护阈值 |
| --upload-config | 私有 Drive/config/upload.yml | 省略完整前缀 DMRColab |
| --upload-cookie | 私有 Drive/credentials/bilibili_upload.json | 投稿凭据，独立于观看 Cookie |

短试跑设置 SEGMENT_SECONDS = 300、MAX_RECORD_HOURS = 0.2；只录制一小时设置 MAX_RECORD_HOURS = 1。总运行时间包括后续渲染、合并和投稿，可能超过录制上限。时间统一为 Asia/Shanghai。

## 3. 私有投稿配置

公开示例 colab_support/upload.example.yml 默认 enabled: false，不含个人账号或直播间。复制到私有 Drive 并填写：

```text
MyDrive/DMRColab/config/upload.yml
MyDrive/DMRColab/credentials/bilibili_upload.json
```

投稿 JSON 同样使用 cookie_info.cookies，要求非空 SESSDATA 和 bili_jct，不要求 token_info。观看账号与投稿账号可以不同。

启用投稿必须明确填写 account（账号备注）、expected_uid（登录身份校验）、tid、copyright、source、title、desc 和 tag。copyright 1 为自制、2 为转载；转载须填写非空来源。分区和转载属性没有推断默认值。配置 enabled: true 后，每场自动执行，不逐场确认。

min_length 默认 120，不能低于 120；更短段标记 skipped_short，仍录制、渲染和备份。limit 为上传分块并发数，默认 3。part_timeout 为单 P 任务超时，drain_timeout 为独立投稿收尾预算，默认都为 7200 秒。录制期间投稿并行；媒体归档完成后进入独立投稿收尾阶段，单 P 超时始终从该 P 开始处理时计算。

标题、简介、标签和来源复用原项目模板，如 {TITLE}、{STREAMER.NAME}、{CTIME.YEAR}。第一个有效分段确定稿件元数据，后续只追加 P1、P2 等分 P；不会用全场合并 MP4 投稿。可选 cover 支持上游的本地文件或图片 URL，配置封面处理失败不会悄悄跳过继续提交。

启用时在录制前查询登录身份；配置、凭据或身份错误则拒绝启动。运行中失效会停止投稿队列，录制、渲染、备份继续。每场使用临时盘私有配置快照，运行时修改 Drive 配置不会改变已开始的稿件。

## 4. 备份、投稿事务与完成条件

```text
MyDrive/DMRColab/runs/<run_id>/
  source/             原片分段
  danmaku/            ASS
  rendered/           弹幕版分段，每个有效分段作为一个 P
  merged/complete.mp4 全场归档，仅用于 Drive
  manifest.json       三项结果、分段状态、校验值、计数和耗时
  upload.json         投稿事务、远端文件标识、BVID/AID 和状态
  upload_resume.json  独立补传结果（补传后生成）
  events.log          白名单事件日志
```

视频和 ASS 关闭写入后才处理，按 group_id 和 segment_id 去重。原片、ASS 和成品三个备份都成功后才投稿。渲染、备份各一条队列；投稿按数字段号串行执行，编号缺口或失败段阻挡后续追加。

复制先写临时名称，关闭后校验大小和 SHA-256，读回一致才改为正式名称，最多三次。此验证只证明挂载目录可读回一致，不宣称已验证 Drive 服务端持久性。Cookie、上传授权 URL、原始 API 响应不进清单或备份日志；事务中的元数据和身份编号只保存在私有 Drive。

状态区别：

- uploading：媒体传输中。
- uploaded：媒体上传完成，尚未提交稿件。
- submitting：本地和 Drive 均已校验保存操作意图，提交正在进行或尚未核对。
- submitted：有效 BVID 与远端文件顺序核对一致；不表示审核通过或已经公开。
- failed：明确失败，后续停止；备份文件保留。
- unknown：可能已提交但无法确认，禁止盲目重发和继续追加。
- skipped_short：小于本场设定的最短投稿时长。

每个上传块最多三次。AUTO 探测会跳过超时或拒绝访问的节点，继续选择可用 UPOS 线路。媒体传输遇到超时、连接失败、HTTP 429/5xx 或块传输失败时，整文件最多尝试三次，AUTO 会重新探测线路；所有尝试受单 P 任务超时限制。明确的鉴权或预上传拒绝不自动重传。全部块成功才合并上传；创建和追加稿件 POST 无自动重试。提交前保存操作编号、内容哈希、远端文件名和预期分 P 序列到本地及 Drive；持久化失败绝不发送 POST。提交响应丢失先核对 BVID；首 P 的 BVID 丢失时有限查询自己的稿件列表，找不到或不能唯一确定则 unknown。稿件锁定、删除、查询失败或外部修改分 P 时不另建稿件。

投稿错误在分段 upload 和 upload.json 中保存 diagnostic：白名单阶段、错误类别、异常类型，以及可取得的 HTTP 状态、API 错误码、尝试次数。Notebook 最终汇总也显示这些诊断，不记录 Cookie、授权 URL、原始响应或异常正文。历史 upload_operation_failed 无法从旧日志补回已丢弃的错误细节。

停止录制后等待下载器完成屏障和所有尾段事件，不靠队列暂空判断结束。同一场按第一段实际帧率固定渲染输出，统一 CFR、H.264 Main/yuv420p 和 MP4 视频时间基准，避免重连后的微小帧率变化使成品无法直接拼接。音频保留原流。

媒体阶段完成后，参数一致的分段使用 stream copy 合并；同规格 H.264/AAC 仅帧率、时间基准或编码头不一致时，先统一到第一段帧率再合并，记录 mode=normalized_stream_copy 和 stream_differences。Colab 使用 NVENC，额外编码和临时空间仍受原收尾截止时间及磁盘余量保护；原分段和 Drive 备份不覆盖。分辨率、像素格式、流数量、采样率或声道变化仍报告 merge_incompatible_streams，并列出不同字段，不自动缩放或改变声道。

合并结果检查时长和输出流签名；CUDA 可用时全片硬解，否则短片全片软解，长片抽查头尾与接缝。投稿失败不使媒体归档跳过。若媒体本身失败，合并按原规则报告失败或跳过。

结果分别查看 recording_backup、merge、upload。启用投稿时，至少有一个有效 P 且所有应投 P 都确认提交或按短段规则跳过，投稿才算成功。全短场、失败、unknown 或超时都非零退出。已提交内容不自动撤回。

第一次中断通过 STOP 请求受控停止，仍处理已完成分段；第二次通过 FORCE_STOP 明确强制结束。监督器可终止 worker 及其子进程组；强制结束不宣称成功。不继续等待下一场。手动模式保留运行时和媒体；自动模式由 Notebook 完成汇总和最终清单同步，再调用 runtime.unassign()。

最终元数据备份或回执写入失败时，Drive 可能留在旧状态，须结合退出码、本地清单和文件核对；不要只看某份旧 success。

## 5. 本地自动触发和回执

Windows 可双击仓库根目录的 `start_colab_monitor.cmd`。首次运行：

```powershell
.\start_colab_monitor.cmd --setup
```

向导选择任务配置（例如 `configs/DMR-example.yml`），填写本机 Google Drive for desktop 的同步根目录，例如 `G:\你的云端硬盘`，以及 Colab Notebook 地址。Notebook 支持私有 Drive 副本 `https://colab.research.google.com/drive/...`，也支持 GitHub 地址 `https://colab.research.google.com/github/<owner>/<repo>/blob/<branch>/...ipynb`；GitHub 模式下 Notebook 仍挂载同一个 Drive，并从 `DMRColab/control` 读取本地触发请求。可选填写 Google 账号邮箱，用于多账号选择；留空仅自动选择唯一可识别账号。旧启动设置仍可使用，需要指定账号时重新运行 `--setup`。向导会自动创建 `DMRColab/control`，设置保存在 `.temp/colab-monitor-settings.json`，不会保存 Cookie。后续直接双击 `.cmd` 即可复用设置。

`--drive-sync-root` 必须是本机可读写的同步目录。触发器把 `request.json`、`claim.json` 和 `ack.json` 写入该目录，Google Drive 将它们同步给 Notebook；录像、渲染和合并仍在 Colab 运行时处理，并备份到 Drive 的 `DMRColab/runs/<run_id>/`。

手动打开模式：

```powershell
python colab_trigger.py --url https://live.bilibili.com/<房间号> --drive-sync-root "G:\My Drive"
```

自动模式需 Google Drive for desktop 同步目录、专用浏览器 profile 及 loopback CDP。首次登录、授权和保存表单可使用 CPU。录制 Notebook 默认选择“读取本地触发请求”，正式副本保存为 GPU 类型，准备完成后断开运行时；需要手动录制时再切换为“手动填写直播间”。监控期间无需 GPU 连接，Run all 在开播后申请运行时。实际 GPU 型号由预检记录：

```powershell
python colab_trigger.py --url https://live.bilibili.com/<房间号> --drive-sync-root "G:\My Drive" --notebook-url "<自己的 Notebook URL>" --auto-run --cdp-url http://127.0.0.1:9222
```

自动浏览器适配依赖本地 Playwright，Windows B站监控可直接安装 `colab_support/monitor_requirements.txt`。连接已有专用浏览器，不启动或控制日常浏览器。CDP 只接受 http 的 127.0.0.1、localhost、::1 和显式端口，不接受外部地址。完整 `requirements.txt` 还包含 Douyu/Douyin 等平台使用的 `quickjs`；Windows Python 3.13 可能没有对应 wheel，B站监控无需安装它。

只对配置的 Notebook 至多点击一次“全部运行/全部執行/Run all”。通过可见按钮、状态区和弹窗判断，运行中、连接中或启动状态无法辨别时交给人工。点击后专用监听线程保持 CDP 连接，每 2 秒检查一次 Drive 授权；直播仍每 60 秒轮询。监听全部 browser.contexts 的 page 事件及目标页面的 popup 事件，并扫描已有页面；只有目标 Notebook 及其 opener 链中的 HTTPS accounts.google.com 页面会自动处理，其他账号窗口不点击。支持简体、繁体和英文的 Drive 连接、继续及允许按钮。

命令行可附加 `--google-account "自己的账号邮箱"`；账号列表有多个且未指定、账号不匹配或授权页无法确认配置身份时报告明确原因。密码、验证码、二次验证与未知授权页面保留人工处理。监听在有效同编号回执到达、15 分钟请求窗口结束或本地监控停止时关闭；不会因点击按钮就宣称录制成功，也不自动重启运行时或保活。页面 DOM 可变化，真实账号与浏览器适配仍须另行验收。

`--once --auto-run` 会等待认领后退出，期间授权监听保持活动；认领失败或超时返回非零退出码。仅打开浏览器的 `--once` 保持触发后退出。

监控重启时，尚未过期且未收到回执的已发送请求会保留原运行编号，并补开原 Notebook 页面（优先复用专用浏览器的空白标签）。已认领、运行中和收尾中的任务同样会恢复原页面，不受认领期限限制；页面渲染完成后切换到该标签。此时不会重写请求、延长有效期或再次点击全部运行；未认领时日志显示“已有 Colab 请求，等待认领”。补开页面失败会独立告警，活动任务状态仍按 Drive 回执读取。页面恢复不代表录制已启动，只有认领回执才能确认 Colab 已接收任务；过期且未认领的请求仍走下述显式恢复流程。

`--setup` 只修改设置，保留活动或未确认的请求状态。旧请求认领超时后，先停止监控并核实旧 Notebook 没有活动任务，再执行 `start_colab_monitor.cmd --recover-expired`，无需手动改名状态文件。恢复入口在进程锁内校验原请求已过期、Drive 没有 claim/ack 文件且原 Notebook 明确未连接；通过后归档本地状态到 `.temp/colab-trigger-state.expired-<run_id>.bak.json`，重新累计开播确认次数。原 Notebook 标签已关闭时，恢复入口会在专用浏览器重新打开原地址，等待最多 15 秒识别连接状态，不点击全部运行或连接按钮；同地址有多个标签时，任一仍连接或状态未知都会阻止恢复。请求文件、过期时间和历史结果保持原值。有认领、回执、未知运行时状态或损坏请求时拒绝恢复；错误信息分别提示仍连接和无法确认，后者需检查登录或弹窗。新请求记录原 Notebook 地址，修改启动设置后仍检查原地址；旧版状态不含该地址，执行恢复前须由本人核实旧副本，再检查当前配置的 Notebook。

自动模式在⑦完成结果汇总和媒体校验后保存最终清单与回执，随后调用 `google.colab.runtime.unassign()`；⑧仅供手动模式检查。失败任务先补存尚未备份的已关闭媒体到 `recovery/`，不复制 Cookie、私有配置和原始 console 日志。保存无法完成或监督器仍活动时不释放，保留唯一副本并报告 `needs_attention`。认领前的授权、源码下载等错误无法保证自动释放，15 分钟认领超时后人工检查。

清单和回执增加 `runtime_release.status`：`requested` 表示释放意图已保存并将请求释放，`unknown` 表示持久化或调用异常；事件名称为 `runtime_release_requested`，不是任务成功状态。任务的 `success/failed` 保持原语义。请求发出后无法从已结束内核确认释放；本地在最终回执至少 60 秒后只读检查指定 Notebook，明确未连接才允许重新武装，仍连接或无法判断进入 `needs_attention`，不重复执行。

CPU 准备阶段的 Drive 授权不保证新 GPU 运行时免授权。本地在上述限定范围内推进已登录账号的 Drive 授权；密码和二次验证仍由本人完成。临时磁盘内容会随运行时释放丢失，结果检查使用 Drive。上述机制只验证挂载目录读回一致性，不承诺服务端持久性。

默认 60 秒轮询，连续两次开播触发；相同状态日志自动去重避免刷屏（可通过 `--verbose` 保持每轮输出），上场已经回执终结且连续三次下播后才能重新武装。查询失败清空连续计数，不把活动任务判为结束。OS 进程锁防止本地重复启动，启动意图先写盘再操作浏览器。状态损坏、旧协议状态或启动结果不明不会自动重置和重新点击。

```text
<同步根目录>/DMRColab/control/
  active.json                  当前请求编号
  runs/<run_id>/request.json    schema_version=2，15 分钟有效
  runs/<run_id>/claim.json      单运行时认领
  runs/<run_id>/ack.json        accepted/running/draining/success/failed
```

同一编号贯穿请求、认领、运行目录、清单和回执，校验版本、有效期及路径。认领仅用于约定的单运行时，不声称提供 Drive 跨机器分布式互斥。手动参数设置每次生成新编号。

认领时由 Notebook 写 accepted；安装依赖和预检期间准备线程每 30 秒更新，结束时停止并等待线程退出，正式启动后由单一监督器负责回执。心跳附准备或媒体阶段及投稿状态；超过 5 分钟未更新或 worker 状态长时间停滞则需人工检查，不推断任务结束。15 分钟未收到认领不重复发起。损坏状态需要先人工核实活动任务，再归档本地状态重新开始；不能删除状态作为自动重试策略。

若 Drive 写入阻塞导致准备心跳线程无法退出，则停止后续录制和自动释放，保留运行时供人工检查，避免尚在写入的 accepted 覆盖最终回执。

## 6. 按运行编号补传

只补传已关闭任务的校验备份。先确认没有活动运行或其他补传实例：

```bash
python colab_upload.py --run-id <run_id> --drive-root /content/drive/MyDrive
```

补传校验原片、ASS、成品三个备份的大小及 SHA-256，把成品恢复到临时盘。绑定原 run_id 和 expected_uid，沿用事务中的首 P 元数据，核对远端序列并跳过已提交内容。媒体传输中断时重新上传该文件，不恢复分块。

unknown 必须先核对远端，无法确认就停止。首 P 编号遗失且自动查询无法找回时，可人工提供候选值，仍核对账号和远端文件序列：

```bash
python colab_upload.py --run-id <run_id> --reconcile-bvid <BVID>
```

本地与 Drive 中的投稿事务记录不一致时会停止，不能覆盖本地较新的提交意图。请保留两份记录供人工核对，不通过删除 upload.json 来重试。服务器明确拒绝的稿件提交不自动再试；本版不提供自动修改或撤回已提交 P 的入口。

## 7. 按运行编号恢复处理与投稿

使用 `notebooks/colab_resume.ipynb`，第一步先挂载 Drive，第二步填写原运行编号（虚构示例：`20261001-220500-bbbbbbbb`），再从上到下执行。先将本次恢复代码提交、推送到 `colab` 远程的 `codex/colab-poc`，再打开仓库中的 Notebook 或保存私有副本。每次准备源码使用独立目录，不覆盖录制 Notebook 的检出；本入口不使用直播触发请求，不会遇到旧请求 `trigger_expired`，也不会重新录制。

开始前确认该编号的录制、补传和其他恢复实例已结束；建议另开独立 GPU 运行时。原清单必须为 failed/success，现有同编号控制回执也必须已结束。本机恢复与补传在相同 work-root 下共用进程锁；不提供 Drive 跨运行时分布式互斥，不要同时运行多个副本。

表单默认补做投稿，使用现有私有 `upload.yml`（须 enabled: true）和投稿 Cookie；取消 UPLOAD 则只恢复媒体。默认 NVENC，需要 GPU；可显式选择 libx264 使用 CPU，长片处理可能较慢。媒体处理总时限默认 4 小时，投稿仍使用配置中单 P 和总收尾时限。

流程校验原片、ASS、已有成品的大小和 SHA-256；成功成品复用，未完成渲染的分段从已校验原片和 ASS 重渲染。已有成功合并备份复用，否则使用当前合并逻辑处理，包括旧录像帧率与时间基准差异的规范化。合并失败仍允许补传已校验分 P，最终分别记录合并和投稿结果。已有成功备份损坏或缺失、段号不连续时停止，不用新内容覆盖证据。已存在该段投稿事务时禁止重渲染改变文件身份。

新成品与合并归档放在原运行目录下的 `recovery/rendered/`、`recovery/merged/complete.mp4`，每步校验保存后更新独立 `resume_manifest.json`。重跑表单和后续步骤，会复用这些已完成结果；中断时尚未完成的媒体步骤重新执行。清单绑定原 manifest 的 SHA-256；本地与 Drive 恢复清单不同则停止核对，不自动丢弃较新证据。只校验挂载目录读回一致，不声明已验证 Drive 服务端持久性。

投稿沿用原 `upload.json`、原首 P 元数据和同一稿件，核对远端后跳过 submitted，unknown/submitting 仅核对，无法确认就停止。候选 RECONCILE_BVID 通常留空，首 P 响应丢失时才填写待核对编号。恢复结果保存到 `upload_resume.json` 和 `resume_manifest.json`，不覆盖原 `manifest.json`、原录制错误或触发回执；恢复 success 仅表示现有已备份分段完成选定步骤，不能补回漏录内容。本 Notebook 保留运行时供检查，检查后手动释放。

CLI 也可先检查再执行：

```bash
python colab_resume.py --run-id <run_id> --inspect-only
python colab_resume.py --run-id <run_id>
python colab_resume.py --run-id <run_id> --no-upload
```

若同一场直播被多个失败任务拆开，使用已创建稿件的任务作为主任务。以下编号均为虚构示例：`20261001-210000-aaaaaaaa` 已有一段投稿，后续任务为 `20261001-220500-bbbbbbbb`，先从主任务的 `upload.json` 确认目标 BV 号，再执行（替换为自己的编号与 BV 号）：

```bash
python colab_resume.py --run-id 20261001-210000-aaaaaaaa \
  --append-run-id 20261001-220500-bbbbbbbb \
  --target-bvid <主任务 upload.json 中的 BV 号> --inspect-only
python colab_resume.py --run-id 20261001-210000-aaaaaaaa \
  --append-run-id 20261001-220500-bbbbbbbb \
  --target-bvid <同一个 BV 号>
```

Notebook 对应填写（虚构示例）：`RUN_ID=20261001-210000-aaaaaaaa`，`APPEND_RUN_IDS=20261001-220500-bbbbbbbb`，`TARGET_BVID` 为主任务原稿件 BV 号，`UPLOAD=True`，`RECONCILE_BVID` 留空。多个追加编号用英文逗号分隔；普通单任务恢复时两个追加字段都留空。`TARGET_BVID` 用于跨任务追加，`RECONCILE_BVID` 仅用于找回单任务创建响应丢失的稿件，二者不可混用。

若第④步出现退出码 2，先查看参数解析错误及第③步打印的源码 commit。新 Notebook 配合未包含 `--append-run-id` / `--target-bvid` 的旧源码会在解析参数时退出，尚未校验备份或投稿。第③步会在安装依赖前检查追加入口；本地改动需要先由用户提交、推送至 `colab` 的 `codex/colab-poc`，再重新运行第①②③步下载源码，第④步显示完整校验输出。仅重跑第④步会继续使用已经下载的旧目录。

跨任务追加会锁住所有涉及的本机恢复目录，校验房间、录制时间、源片和 ASS 指纹、已有投稿前缀及远端文件顺序；重复片段会记录并跳过，时间重叠、缺少时间证据、其他任务已有未确认投稿或目标 BV 不一致会停止。时间区间来自录制清单，可能包含探测等待；发现重叠时保守停止，不自动裁剪，时间间隙会列入计划且无法补录。开始处理前在主任务 `upload.json` 保存固定追加计划，在后续任务 `upload.json` 写入主任务和 BV 绑定，避免以后被单独恢复到另一稿件。目标 BV 号只允许指向主任务已有稿件，不会新建第二个稿件。重跑时使用相同任务集合和 BV 号，已确认部分只核对；提交结果不明时仍只查询远端，禁止重发。

此模式恢复已有分段或补渲染后追加分 P，不生成跨任务合并归档。结果保存在主任务的 `append_resume.json`、`upload_resume.json`，原录制清单及回执保持原值。绑定后的任务必须通过同一追加入口补传；仅恢复媒体仍可使用单任务 `--no-upload`。

## 8. 验证

```bash
python -m unittest discover -s checks -p "test_colab*.py" -v
```

2026-10-10 本地最终验证：88 项离线测试全部通过，git diff --check 通过。真实浏览器、Drive 同步、B 站接口与测试账号投稿尚未执行。离线验证后按用户授权发布本次源码改造，发布不代表真实云端验收完成。

2026-10-11 授权与提前认领修复：初次完整运行 116 项 Colab 回归，115 项通过，1 项因缺少 FFmpeg/ffprobe 跳过；补齐账号信息误点、回执时间戳校验、心跳阻塞和繁体连接状态后，69 项受影响回归通过。随后修正 --once 自动模式提前退出，最终 40 项启动与触发回归通过，Python 语法和 git diff --check 检查通过。真实 Google OAuth 弹窗和 Colab 云端链路未实测。本次仅修改工作区，Git 提交、推送及私有 Notebook 副本更新由用户执行。

2026-10-11 过期恢复入口验证：新增未认领过期请求、活动任务阻止恢复、原 Notebook 地址、归档失败与迟到认领测试；69 项相关回归通过，补齐启动前尚未保存超时标记和浏览器检查期间迟到认领后，27 项恢复与触发回归通过，Python 语法和 git diff --check 检查通过。验证只操作临时目录，未对实际监控执行归档或重新触发。

2026-10-12 分段合并与上传恢复验证：148 项 Colab 回归和 19 项共享手动投稿回归全部通过，Python 语法及 git diff --check 通过。合成媒体复现 25 fps 与 299/12 fps、不同时间基准和编码头，确认规范化合并、统一输出后的快速合并、原文件保留、额外磁盘预算与失败清理；模拟 API 验证 AUTO 跳过失败节点、媒体传输重试、脱敏诊断和提交结果不明时不重发。本次按用户明确授权提交并推送，也包含先前的过期请求恢复入口。未重启实际监控，未重新合并历史 Drive 录像，未进行真实 Colab GPU 编码或 B 站投稿验收。

2026-10-12 跨任务追加验证：94 项追加、投稿事务、单任务恢复和共享手动投稿回归通过。覆盖主任务 P1 保留、后续分 P 顺序、重跑去重、提交超时及迟到确认、账号/BV/房间不匹配、时间重叠、相同源片与 ASS 去重、明确失败分段按序重试、缺失成品恢复及中断恢复、绑定任务禁止单独投稿、Notebook 参数、源码兼容检查与执行顺序。扩展恢复测试在沙箱内创建硬链接被拒绝，获准在沙箱外重跑后通过。示例和测试使用虚构任务编号与直播间；未操作真实 Drive 或投稿，未重启监控。

离线测试只使用模拟浏览器、模拟 API、临时目录和合成媒体。覆盖真实 CLI 参数、Cookie 不调用登录工具、首 P 创建与同 BVID 追加、备份门槛、乱序与重复、全短场、分块失败、提交超时核对、未知结果去重、恢复校验、进程锁、请求过期、损坏状态、查询中断、Notebook 顺序与旧变量防护，并保留原录制、渲染、合并和停止回归。

真实验收仍需依次执行：

1. 保持 enabled: false，验证本地开播 → Drive 同步 → Notebook 单次自动运行 → 同编号回执 → 完整收尾。
2. 用指定测试账号和素材，开启投稿，至少两个有效分段；核对 UID、标题、P 顺序、同一 BVID、Drive 清单及 upload.json。
3. 测试一次受控停止和自然下播，检查尾段、合并、音画与弹幕同步。
4. 记录渲染耗时/分段时长、积压、磁盘使用，以及 Colab UI 的 CU 消耗。不预先认定 T4 或其他 GPU 性能已验收。

自动模式完成后检查 Drive、最终回执和浏览器断开状态；手动模式检查完再由本人断开并删除运行时。新增按需连接与自动释放需实测成功场次、预检失败、渲染失败、受控停止及备份失败保留运行时。未有真实云端证据时，报告仅限本地实现和离线验证。
