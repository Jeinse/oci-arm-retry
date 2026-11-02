# 代码审查与执行计划

审查日期：2026-11-02。上游：WendyChen330/oci-arm-retry，基准提交 d1c7e4eccd6b9eb32c7ea068c330c206610d624a。本 Fork 仅保留新加坡 E2.Micro 路径。

## 已移除的上游行为

| 上游行为 | 当前实现 |
| --- | --- |
| 创建 VCN、互联网网关、子网 | 完全移除，必须指定已有子网 |
| 更新路由和安全列表、开放端口 | 完全移除，无此类 OCI 调用 |
| 每 90 秒循环及约 330 分钟进程 | cron 单次运行，无创建循环 |
| 工作流失败后自行 dispatch 接力 | 完全移除；没有 workflow_run 接力或 dispatch API |
| 对多种 API/网络错误一概继续 | 仅明确缺货允许继续；429 退避；其他停止 |
| 从源码读取上游硬编码 SSH 公钥/租户 | 完全移除；由 GitHub Secrets/Variables 明确提供 |
| 需要长期 GitHub PAT | 使用内建短期 GITHUB_TOKEN |
| ARM 与 Micro 可以同时运行 | ARM 文件与工作流已移除 |

## OCI 调用边界

读取接口覆盖区域订阅、AD、区间、实例、E2 额度、各 AD 启动卷、块卷和两类卷备份、已有子网、指定镜像及其形状兼容性。

OCI 非 GET 操作只有：

1. `create_compute_capacity_report`：诊断报告，不分配实例。
2. `launch_instance`：仅手动授权的活动任务可达，每次运行最多一次。

源码中没有 terminate、stop、reboot、网络创建/更新、卷更新、IAM 创建或修改接口。新实例自己的启动卷和 VNIC 属于一次 launch 的关联资源，不修改已有 VCN、路由、安全规则或实例 IP。

`launch_instance` 固定 shape=VM.Standard.E2.1.Micro、地区=ap-singapore-1、50 GB 启动卷、10 VPUs/GB，不设置 fault_domain 或 Flex shape_config。拒绝超过 200 GB 的共同存储用量、未知的卷大小、自定义/社区/Pro 镜像、非主区域以及不完整的读取结果。

真实 SDK 的 `list_boot_volumes`、`list_volumes` 使用当前版本要求的命名参数。手工遵循全部分页并关闭每个 API 调用的重试，避开 SDK pagination helper 额外套用默认重试的行为。页标记重复或超过分页上限会停止，不默认为空清单。

## 默认状态及授权

初始状态 paused；cron 先用 GitHub 只读状态门禁检查，暂停、完成或异常状态直接跳过 OCI 凭据处理和 SDK 安装。

`plan` 只读检查并生成容量报告，不调用 launch、不创建状态分支。手动 `start` 同时要求：

- 输入 `CREATE_ONE_E2_MICRO`；
- `OCI_FREE_CONFIG_APPROVED=true`；
- 所有实时前置条件通过；
- 没有前一笔待核实的创建请求；
- 没有永久 completed 记录；
- 距离上次创建/检测满足退避或最小间隔。

本次交付不会运行 start，也不会设置上述费用确认变量。

## 防止重复创建

持久状态在仓库 e2-retry-state 分支。GHA concurrency 串行化所有本工作流操作；每次状态保存带旧 blob SHA，冲突时停止。

创建前必须先成功写入 launch_pending=true 和 OCI retry token。SDK launch 显式使用 NoneRetryStrategy。收到明确缺货回复才清除待决状态，429 则退避一小时。超时、通用 5xx、权限或配额错误均保留未决标记并停止，不自动重复 POST。

如果 OCI 已接受创建但“完成状态”保存失败，仍保留待决标记；后续仅能先读取并确认目标，不能发新的创建。固定实例名、稳定管理标签及实例清单提供另外的识别层。成功后 completed 永久阻止创建；之后人工删除目标也不会自动补建。

这些措施不能锁住账号之外的手工操作或另一套独立创建工具。读取与创建之间存在跨服务竞态，活动期间应避免其他来源并行创建 E2、建卷或扩容。人工删除整个状态分支或恶意修改源码会破坏应用层保证；程序不提供 IAM 级硬性费用上限。

## 凭据和通知

API 私钥只从 Secret 进入进程内存，不写工作区或仓库，不打印原始异常/请求体。SSH 使用用户自己的公钥，私钥不进入程序。动作表达式通过环境变量传递，不拼接为 Shell 源代码。

GitHub 令牌仅需要 contents:write（状态分支）及 issues:write（成功通知）；不授予 Actions 管理权、不开新 PAT。仓库已启用 Issues；成功通知指派仓库所有者 Jeinse，是否收到邮件/手机推送取决于其 GitHub 设置。若通知失败，先保存的完成状态继续阻止创建。

状态分支不保存用户的原始 OCI OCID、公网 IP 或密钥。工作流 Summary 可显示区域、形状、数量、存储总量和容量请求 ID；公开仓库运行日志的可见性由 GitHub 仓库决定。

## 验证证据

Python 3.12 + 真实 OCI Python SDK 2.189.0，39 项离线测试全部通过。测试调用以 mock、模拟分页响应和临时生成的测试签名密钥完成；未使用用户 OCI 凭据。

覆盖范围：默认不启动、plan 无创建/状态写入、手动确认与费用确认、单次创建、完成后永久停止、缺货快照跳过、严格识别缺货错误、429 一小时退避、网络超时、工作流取消后的未决状态、完成记录写入失败、状态写入失败、暂停恢复及最小间隔、配置改变、任务到期、150+50 GB 边界、151 GB 拒绝、未知大小、子区间存储、第二个已存在 E2、名称冲突、标签目标、目标已终止、自定义镜像、非主区域、权限拒绝、固定 launch 请求体、硬件不支持、状态数量矛盾、私网子网、全部分页、分页 429 不被隐式重试、真实 SDK 客户端初始化、工作流结构与 GitHub 状态 CAS。

Actions 的 checkout、setup-python 均固定为实际查验的官方 v6 commit SHA；直接 OCI 依赖固定 2.189.0。传递依赖并非完整哈希锁定；来源为 Python 官方包索引，首次线上运行仍需用户检查运行结果。

## 尚未进行的线上步骤

没有运行任何 GitHub 工作流，没有触发 OCI 创建，没有代填或读取用户 Secrets，没有设置活动状态，没有部署自动创建，也没有对真实 OCI 做本轮联调。

用户最后手动填写 Secrets/Variables，执行 plan，核实自己的免费资格与配置后，再决定 start。程序的免费资格前置检查不等于 Oracle 的零费用保证。官方容量是快照、GitHub cron 可能延迟或跳过，均不能保证抢到实例。
