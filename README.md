# 新加坡 E2.Micro：每 30 分钟单次尝试

此仓库是 [WendyChen330/oci-arm-retry](https://github.com/WendyChen330/oci-arm-retry) 的正式 Fork，基于上游提交 `d1c7e4eccd6b9eb32c7ea068c330c206610d624a` 修改。仅保留 E2.Micro 路径，上游的网络自动创建、ARM 工作流、90 秒循环和工作流接力已移除。

**默认暂停。代码提交和启用 Actions 本身不会创建 OCI 实例。由仓库所有者先运行 `plan`，检查结果后再手动运行 `start`。**

## 固定执行计划

| 项目 | 此 Fork 的行为 |
| --- | --- |
| 地区 | `ap-singapore-1`，必须是账号主区域且订阅 READY |
| 型号 | 仅 `VM.Standard.E2.1.Micro` |
| AD | 从真实列表查找唯一 AD-1 |
| FD | 不指定，由 OCI 自动分配 |
| 目标 | `e2-micro-singapore-2`，只为已有一台 RUNNING E2 的账号创建第二台 |
| 网络 | 只读取并使用你指定的已有子网；新实例关联一个新 VNIC，不修改既有网络配置或其他实例 IP |
| 镜像 | 你明确指定的 Oracle 官方 Ubuntu 22.04 或 24.04 平台镜像，检查与 Micro 兼容；拒绝自定义/社区/Pro 变体 |
| 新启动卷 | 固定 50 GB、10 VPUs/GB；不附加其他块卷，不创建备份 |
| 额度 | 检查根区间及目标区间的 E2 服务可用量，且所有可见区间当前仅有一台未终止 E2 |
| 存储 | 枚举主区域各 AD、根区间及所有活动子区间；现有启动卷和块卷加 50 GB 须不超过 200 GB |
| 调度 | UTC 每小时 `:17`、`:47`，意图间隔 30 分钟；GitHub 可能延迟或跳过调度，不保证整点准时 |
| 每次运行 | 先读容量；明确缺货就不提交创建；容量可用时至多一次 `launch_instance` |
| 重试 | 明确主机缺货等待下次；429 退避 1 小时；其他错误停止等待审查 |
| 成功 | 写入永久完成状态，后续不再创建；成功通知为 GitHub Issue，指派给仓库所有者 |
| 最长活动期 | 每次手动 `start` 最长 30 天；到期暂停，可再次手动开始未完成的任务 |

程序每次都重新读取额度和资源，不把历史快照当成当前余额。例如 150 GB 现有卷 + 50 GB 新卷 = 200 GB；任何新增或扩容都会减少余量，无法完整读取时停止。

## 1. 在 GitHub 页面填写配置

打开本仓库 **Settings → Secrets and variables → Actions**。所有配置都由你在 GitHub 正常界面填写，不把账号密码、API 私钥或 SSH 私钥发到聊天，也不写进源码。

### Repository secrets

| Secret | 填什么 |
| --- | --- |
| `OCI_USER` | 现有 API 凭据所属的 OCI 用户 OCID |
| `OCI_TENANCY` | 你的租户 OCID |
| `OCI_FINGERPRINT` | 对应 API 公钥的指纹 |
| `OCI_PRIVATE_KEY` | 对应 OCI API 私钥 PEM 的完整内容；只填入此 Secret |
| `OCI_SSH_PUBLIC_KEY` | 新实例登录所用 SSH **公钥**的完整一行，如 `ssh-ed25519 ...`；对应 SSH 私钥自行保管 |
| `OCI_KEY_PASSPHRASE` | 可选；API 私钥加密口令，没有则不设置 |

此版本不需要 `GH_PAT`，也不需要创建新的 GitHub PAT。工作流使用 GitHub 提供的短期 `GITHUB_TOKEN`。OCI API 密钥与 SSH 登录密钥是两套不同凭据，不能混填。

你可以使用已有且有相应权限的 OCI API 凭据。本次代码交付没有为你创建 API 密钥、IAM 用户或策略；是否新增任何 OCI 身份凭据，由你在正常授权界面另行决定。

### Repository variables

| Variable | 填什么 |
| --- | --- |
| `OCI_SUBNET_ID` | 已有的新加坡公共子网 OCID；保留该子网的现有安全规则和路由 |
| `OCI_IMAGE_ID` | 明确选定的官方 Ubuntu x86 平台镜像 OCID；必须为新加坡镜像且支持 Micro |
| `OCI_COMPARTMENT_ID` | 可选；留空使用租户根区间，与你此前日志一致 |
| `OCI_FREE_CONFIG_APPROVED` | 初始留空或 `false`。先看 `plan`，核实费用后才设置为 `true` |

`OCI_REGION` 固定在工作流中，不需要填写。子网及镜像 OCID 可从 OCI 对应详情页复制。程序不自动选择你的其他网络或自动切换镜像，以免创建到你未确认的位置。

**费用确认的实际边界：** 程序可核对主区域、型号、平台镜像、服务额度和共同存储容量，并固定新卷性能；不能替代 Oracle 的实际计费和免费适用标记。PAYG 下仍应在控制台核实所选镜像、新卷及其他关联资源符合你的免费条件，再将 `OCI_FREE_CONFIG_APPROVED` 设为 `true`。这不是零费用保证，也不会影响或降低你现有卷的 120 VPUs/GB 配置。

## 2. 先运行只读计划

打开 **Actions → E2 Micro - one attempt every 30 minutes**。如果 Fork 的工作流尚未启用，先启用 Actions；这不会将任务切换为 active。

点击 **Run workflow**，分支选 `main`，`operation` 选 `plan`，确认文本留空。

`plan` 会查询 OCI 数据并生成容量报告，展示主区域、形状、目标名称、50 GB/10 VPU、现有数量和存储、容量状态及配置摘要哈希。它不会调用实例创建 API，不写入持久任务状态，也不会创建或修改网络。

所有实际资源 OCID 来自你在 GitHub 配置的值，日志不输出 API 私钥、SSH 公钥、实例 OCID或公网 IP。容量 Request ID 可保留供排查。

## 3. 你手动授权实际创建并开始调度

确认只读计划与配置后：

1. 在 Variables 中将 `OCI_FREE_CONFIG_APPROVED` 设置为 `true`。
2. 再次 **Run workflow**，分支仍选 `main`，`operation` 选 `start`。
3. 在 `confirmation` 输入框逐字输入 `CREATE_ONE_E2_MICRO`。
4. 点击运行。这一次才会将未完成任务置为 active，并在容量可用且所有检查通过时尝试创建一台。容量不足则等待后续 cron。

工作流文件仅接受 `Jeinse` 所有的仓库和 `main` 分支。后续如果你有意转移仓库所有者，需要先审查并修改这一限制。

## 4. 暂停、恢复、停止

| 操作 | 做法 |
| --- | --- |
| 查看状态 | 手动运行 `operation=status` |
| 暂停创建 | 手动运行 `operation=pause`；不需要 OCI 凭据 |
| 恢复未完成任务 | 先运行 `plan`，再运行 `start` 并输入确认文本；不会绕过 30 分钟最小间隔 |
| 完全停止工作流调度 | Actions 中打开此工作流，右上角 `… → Disable workflow` |
| 重新启用调度 | `Enable workflow`；只有状态为 active 的未完成任务才会尝试 |
| 当前任务正在进行 | 普通 pause 会按 concurrency 排队，不能撤回已经发出的 OCI 请求；取消运行也不等于撤回已提交的请求 |

如果是 `uncertain` 或 `launch_pending=true`，表示先前创建可能已被 OCI 接受，后续自动调度不再发新请求。手动 `start` 只会先检查已有目标；没有找到时继续停下，不能盲目重试。应先在 OCI 控制台确认那次请求的结果。

## 5. 持久状态与幂等

第一次手动开始或暂停时，工作流在同一仓库建立 `e2-retry-state` 分支，状态文件为 `state/e2-micro.json`。它不是 Actions cache，runner 换机、工作流取消或日志过期也不会丢失这个记录。

- 同一个 repo 的所有本工作流操作使用同一 concurrency 组，不取消正在处理的任务。
- 状态文件更新使用 GitHub 内容 blob SHA 作冲突检查；写入失败就不会 launch。
- 调用创建前先记录 `launch_pending=true` 和一次性 OCI retry token，并明确关闭 SDK 自动重试。
- 使用固定实例名和管理标签识别目标；重命名已有目标仍通过标签检测。
- 创建成功后永久写为 `completed`。即使以后你手动删除那台实例，工作流也不会自动补建。
- 已存在状态分支但状态文件丢失时停止，不把它当作首次启动。
- 状态不包含 OCI 凭据、公网 IP 或原始资源 OCID，实例只记哈希。

请保留状态分支的记录。人工删除整个状态分支、修改源码、其他工具并行创建资源等外部操作不在本程序的并发锁范围内。API 读取和创建也不是跨云原子事务，因此应避免在抢注任务活动期间另行扩容、建卷或手动创建另一台 E2。

## 6. 成功通知

成功后会创建一条 Issue 并指派 `Jeinse`，正文给出对应的工作流运行链接；工作流 Summary 也会保存结果。手机推送或邮件取决于你自己的 GitHub 通知设置。通知失败不会重新创建实例，完成状态会先保存。

“CREATION_ACCEPTED”指 OCI 已接受创建、常见状态是 PROVISIONING，不是保证已经启动完成或 SSH 可达。去 OCI 实例页确认启动和公网 IP，使用你自己的 SSH 私钥连接。

Fork 仓库默认可能关闭 Issues。本次已为成功通知启用 Issues；如之后关闭，仍会保留运行日志与 Summary，但不能保证收到 Issue 通知。

## 7. 已验证与尚未执行

代码已通过离线模拟测试，并使用真实 OCI SDK `2.189.0` 检查模型与方法参数。测试不需要用户密钥，不访问真实云账号。详见 [安全检查报告](docs/SAFETY_REVIEW.md)。

本次交付没有运行 GitHub Actions，没有设置 OCI Secrets/Variables，没有部署 active 调度，没有提交 OCI 创建请求。首次只读联调及实际启动由你在正常 GitHub 页面完成。

GitHub cron 只在默认分支运行，不保证准时；公开仓库长时间无活动可能自动停用定时工作流。查看 Actions 页面确认调度状态。

## 本地离线验证

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m unittest discover -s tests -v
```

正常运行脚本需要 GitHub Actions 上下文；本地直接运行不会创建实例。

## 官方参考

- [Oracle Always Free](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [OCI Python ComputeClient](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/core/client/oci.core.ComputeClient.html)
- [GitHub schedule 行为](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [GitHub workflow token 权限](https://docs.github.com/en/actions/security-for-github-actions/security-guides/automatic-token-authentication)
