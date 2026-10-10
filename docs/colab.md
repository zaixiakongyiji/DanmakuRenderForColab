# Colab 单场录制、自动触发与多 P 投稿

流程：本地检测开播 → 打开指定 Notebook 并点击一次全部运行 → 每小时录制一段 → 渲染、分段备份 → 向同一 B 站稿件追加分 P → 下播后完成媒体归档、投稿收尾和结果汇总。

首版限定一个直播间、一个投稿账号、一个活动运行时。不自动清理媒体或释放运行时；原来的 main.py 和本地配置继续使用原路径。

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
| 自动删除媒体、释放运行时、跨运行时续录、多直播间 | 不在本版范围 |

新增行为不能凭离线测试认定已完成云端验收。首次授权、登录凭据和私有配置由用户准备。本地正式任务在验收前继续按原配置运行。

## 1. 源码与观看凭据

分支为 codex/colab-poc，远程 colab 指向 zaixiakongyiji/DanmakuRenderForColab，origin 保留上游。提交时只纳入相关源码、无输出的 Notebook、测试和文档；不纳入个人配置、Cookie、录播、日志或会话文档。Git 提交和推送默认由用户执行；本次发布已由用户明确授权代理执行。

Notebook 默认从 fork 的 codex/colab-poc 拉取，清单记录实际 commit SHA。已有检出不会自动覆盖运行中的代码；更新前结束活动任务，再手动拉取或换新运行时。Drive 中另存的 Notebook 不会随 Git 更新，请重新打开仓库版本并保存私有副本。

手动将已有观看登录 JSON 放到私有 Drive：

```text
MyDrive/DMRColab/credentials/bilibili.json
```

必须包含 cookie_info.cookies 列表和非空 SESSDATA。只复制 Cookie 到临时盘 private 目录，不复制 refresh token，不运行 biliup 登录或续期。文件缺失或错误时停止预检。观看凭据失效或实际画质下降需用户更新凭据；清单保留实际 quality 和 stream_type。

## 2. Notebook 与环境

依次执行源码准备、安装依赖、Drive 授权、参数、预检、运行和结果检查。每个准备单元格先使旧会话失效；参数单元格失败或预检失败时不能沿用旧 COMMAND 运行。预检及正式运行都检查环境，成功后才能录制；已运行会话不能再次直接运行。

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

每个上传块最多三次。全部块成功才合并上传；创建和追加 POST 无自动重试。提交前保存操作编号、内容哈希、远端文件名和预期分 P 序列到本地及 Drive；持久化失败绝不发送 POST。提交响应丢失先核对 BVID；首 P 的 BVID 丢失时有限查询自己的稿件列表，找不到或不能唯一确定则 unknown。稿件锁定、删除、查询失败或外部修改分 P 时不另建稿件。

停止录制后等待下载器完成屏障和所有尾段事件，不靠队列暂空判断结束。媒体阶段完成后进行全场 stream copy 合并，验证媒体参数、时长和全片解码。投稿失败不使媒体归档跳过。若媒体本身失败，合并按原规则报告失败或跳过。

结果分别查看 recording_backup、merge、upload。启用投稿时，至少有一个有效 P 且所有应投 P 都确认提交或按短段规则跳过，投稿才算成功。全短场、失败、unknown 或超时都非零退出。已提交内容不自动撤回。

第一次中断通过 STOP 请求受控停止，仍处理已完成分段；第二次通过 FORCE_STOP 明确强制结束。监督器可终止 worker 及其子进程组；强制结束不宣称成功。程序结束保留运行时和媒体，不继续等待下一场。

最终元数据备份或回执写入失败时，Drive 可能留在旧状态，须结合退出码、本地清单和文件核对；不要只看某份旧 success。

## 5. 本地自动触发和回执

手动打开模式：

```powershell
python colab_trigger.py --url https://live.bilibili.com/<房间号> --drive-sync-root "G:\My Drive"
```

自动模式需 Google Drive for desktop 同步目录、专用浏览器 profile 及 loopback CDP。预先登录、授权并确认 Notebook 使用 GPU 且运行时已连接：

```powershell
python colab_trigger.py --url https://live.bilibili.com/<房间号> --drive-sync-root "G:\My Drive" --notebook-url "<自己的 Notebook URL>" --auto-run --cdp-url http://127.0.0.1:9222
```

自动浏览器适配依赖本地 playwright，可用 python -m pip install playwright 安装。连接已有专用浏览器，不启动或控制日常浏览器。CDP 只接受 http 的 127.0.0.1、localhost、::1 和显式端口，不接受外部地址。

只操作配置的 Notebook，至多点击一次“全部运行/Run all”。通过可见按钮、状态区和弹窗判断；登录、授权、运行中、运行时未连接或 UI 无法辨别时 needs_attention。不点击授权、不重启运行时、不保活。页面 DOM 可变化，真实浏览器适配必须另行验收；无法识别时宁可交给人工检查。

默认 60 秒轮询，连续两次开播触发；上场已经回执终结且连续三次下播后才能重新武装。查询失败清空连续计数，不把活动任务判为结束。OS 进程锁防止本地重复启动，启动意图先写盘再操作浏览器。状态损坏、旧协议状态或启动结果不明不会自动重置和重新点击。

```text
<同步根目录>/DMRColab/control/
  active.json                  当前请求编号
  runs/<run_id>/request.json    schema_version=2，15 分钟有效
  runs/<run_id>/claim.json      单运行时认领
  runs/<run_id>/ack.json        accepted/running/draining/success/failed
```

同一编号贯穿请求、认领、运行目录、清单和回执，校验版本、有效期及路径。认领仅用于约定的单运行时，不声称提供 Drive 跨机器分布式互斥。手动参数设置每次生成新编号。

认领前由 Notebook 写 accepted 或预检失败，启动后由单一监督器负责回执，避免 worker 与父进程竞争写入。每 30 秒更新心跳，附媒体阶段及投稿状态；超过 5 分钟未更新或 worker 状态长时间停滞则需人工检查，不推断任务结束。15 分钟未收到认领不重复发起。损坏状态需要先人工核实活动任务，再归档本地状态重新开始；不能删除状态作为自动重试策略。

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

## 7. 验证

```bash
python -m unittest discover -s checks -p "test_colab*.py" -v
```

2026-10-10 本地最终验证：71 项离线测试全部通过，git diff --check 通过。真实浏览器、Drive 同步、B 站接口与测试账号投稿尚未执行。离线验证后按用户授权发布本次源码改造，发布不代表真实云端验收完成。

离线测试只使用模拟浏览器、模拟 API、临时目录和合成媒体。覆盖真实 CLI 参数、Cookie 不调用登录工具、首 P 创建与同 BVID 追加、备份门槛、乱序与重复、全短场、分块失败、提交超时核对、未知结果去重、恢复校验、进程锁、请求过期、损坏状态、查询中断、Notebook 顺序与旧变量防护，并保留原录制、渲染、合并和停止回归。

真实验收仍需依次执行：

1. 保持 enabled: false，验证本地开播 → Drive 同步 → Notebook 单次自动运行 → 同编号回执 → 完整收尾。
2. 用指定测试账号和素材，开启投稿，至少两个有效分段；核对 UID、标题、P 顺序、同一 BVID、Drive 清单及 upload.json。
3. 测试一次受控停止和自然下播，检查尾段、合并、音画与弹幕同步。
4. 记录渲染耗时/分段时长、积压、磁盘使用，以及 Colab UI 的 CU 消耗。不预先认定 T4 或其他 GPU 性能已验收。

完成后检查 Drive 网页和本地退出码，再由本人断开并删除运行时。未有真实云端证据时，报告仅限本地实现和离线验证。
