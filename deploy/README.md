# 三服务联合部署

从 Gateway 仓库根目录操作。`compose.yaml` 管理 Gateway、Knowledge Review、Redis；`compose.test.yaml` 仅用于公开合成数据验证，另加一个本地模型协议测试服务，不能用于真实业务部署。

## 构建和启动

**M2.3.3（2026-10-20）：** 8个后台202提交/继续入口和标题共享Python实际槽，新增Key保护的/agent-metrics；无需新增生产服务或环境变量。先应用最新版Python适配并导出新上下文，再重建Gateway和Agent，旧镜像不会自动获得新能力。槽位与202/暂停/重启边界见根目录docs/JOB_GOVERNANCE.md。

专用compose.jobs-test.yaml只用于本地公开合成模型验收：同12组治理、指标8项、新建6项、替换恢复5项通过。Gateway/Agent UID10001，镜像64个Python模块与最终上下文相同；gateway-m233-validation-20261020的4个容器、2个命名卷、Redis匿名卷和网络已核验清理，镜像保留。证据见evidence/m233-compose-verification-20261020.json；真实模型/embedding-RAG路径仍未验收。

**M2.1历史验收（2026-10-18再次复检）：** 最小容器、最新Gateway镜像编译及核心联合验收通过。修复build阶段遗漏bzip2后，两台Gateway、Agent、Redis健康；双实例40请求放行7/拒绝33，真实Agent SSE首次6项及替换恢复5项通过，Redis停机503与恢复通过。使用公开合成数据和可控模型服务，没有验证真实模型/embedding检索。测试容器和合成卷已清理，镜像保留。证据见根目录 `evidence/m21-docker-recovery-20261018.json`；此前失败过程保留于 `docs/DEVELOPMENT_LOG.md`。

**M2.2（2026-10-18—19）：** 新增本地共享准入和Python实际工作槽；10月18日Release镜像、首次6项/替换恢复5项、双实例限流和Redis故障恢复回归通过。后续收尾及最终镜像标识见`evidence/m22-verification-20261019.json`；旧阶段成绩不替代本次记录。

10月19日最终镜像重新构建，核实Gateway准入配置及Python2个共享槽，原合成会话再次恢复5项通过；已删除本次`gateway-m22-validation-20261018`的5个测试容器、2个合成卷和网络，镜像保留。普通/内存检查72项及真实受控模型8项属于原生/隔离Python验收，未声称在容器内重跑全量并发故障套件，也未验证真实模型/完整embedding-RAG。

网关构建阶段需要从官方发布站下载约137MiB的Boost源包并校验，首次构建会比此前使用系统Boost1.83更慢；缓存生效后不重复下载。

**M2.3.1（2026-10-19）：** 当前Release镜像已提供API Key保护的`GET /metrics`，使用同一Gateway端口，不需要新增环境变量、服务或监控平台。快照只属于当前Gateway进程，不依赖Redis/Agent；重启Gateway后重置，不代表Python实际运行任务数。字段解释、代码设计与命令见根目录`docs/METRICS.md`。

本阶段容器指标检查8项、业务新建6项、仅替换两台Gateway后的业务恢复5项通过；另验证两台Gateway计数独立、重建后归零、Redis停机时指标仍200且业务503、Redis恢复后业务恢复。使用公开构建上下文、受控本地模型和新合成卷，没有修改原Python项目。验收项目`gateway-m231-validation-20261019`的5个容器、2个命名卷、Redis匿名卷和网络已核验清理，镜像保留。证据见`evidence/m231-compose-verification-20261019.json`；未在容器内重跑原生87项，也未验证完整模型取消或真实embedding/RAG。

**M2.3.2（2026-10-19）：** 当前镜像已包含请求范围模型HTTP取消，无新增生产环境变量或服务。需重新应用Python适配并导出新上下文，随后重建Gateway与Agent；旧Agent镜像不会自动获得取消能力。模型请求完整正文后的FIN（包括写半关闭）表示撤回，客户端需保持写方向开放；边界见根目录docs/CANCELLATION.md。

最新容器同9组取消、同8组指标、新建6项及替换Gateway恢复5项通过；验证实际模型HTTP的EOF、failed状态、后续新任务、UID10001及镜像63个Python文件与最终上下文一致。取消测试额外使用compose.cancel-test.yaml暴露本地合成模型观测端口、缩短超时并提高测试额度，它不是生产配置。`gateway-m232-validation-20261019`的4个测试容器、2个命名合成卷、Redis匿名卷及网络已核验清理，镜像保留；证据见`evidence/m232-compose-verification-20261019.json`。无外部模型/真实RAG验收。

先将SSE/共享工作槽/请求取消/后台治理适配应用到独立Python项目，再生成仅包含运行代码、公开网页资产和示例的构建上下文。工具支持已校验的M1.3升级，未知版本仍拒绝覆盖：

```sh
python3 tools/apply_knowledge_review.py --project '/mnt/e/agentic project/knowledge-review' --apply
python3 tools/prepare_agent_context.py --project '/mnt/e/agentic project/knowledge-review'
cp deploy/agent.env.example deploy/agent.env
```

编辑 `deploy/agent.env`，填入原应用所需的模型配置。该文件只在运行时传入，不会被构建上下文复制。上下文目录默认 `build/agent-context`，已存在时工具会拒绝覆盖，更新时请选择新输出目录并设置 `KNOWLEDGE_REVIEW_CONTEXT` 为它的绝对路径。

```sh
read -rsp 'Gateway API key: ' GATEWAY_API_KEY
export GATEWAY_API_KEY
docker compose -p knowledge-review-gateway -f deploy/compose.yaml config --quiet
docker compose -p knowledge-review-gateway -f deploy/compose.yaml up -d --build --wait
curl -fsS http://127.0.0.1:8080/readyz
```

默认构建 `WITH_EMBEDDINGS=0` 的轻量 Agent，支持文档/数据库和不触发语义搜索的模型路径。**它没有本地 embedding 依赖，不能用于需要 search_notes 的完整 RAG。** 要使用本地检索，设置 `WITH_EMBEDDINGS=1` 重新构建；该可选路径会安装 CPU PyTorch 和原 requirements-local.txt，尚未在本轮完成构建/模型下载验证。

模型缓存正式服务只读挂载在 `/app/.models`，不会在普通请求里下载。先用 `WITH_EMBEDDINGS=1` 构建 Agent，再显式使用同一模型卷的可写准备配置：

```sh
export WITH_EMBEDDINGS=1
docker compose -p knowledge-review-gateway -f deploy/compose.yaml build agent
docker compose -p knowledge-review-gateway -f deploy/compose.yaml -f deploy/compose.models.yaml run --rm --no-deps agent python main.py --download-model
docker compose -p knowledge-review-gateway -f deploy/compose.yaml up -d --build --wait
```

准备配置关闭离线开关，正式配置恢复只读和离线读取。简单追加 `compose run --volume ...:rw` 在本机没有覆盖服务的只读标记，所以改用明确替换 volumes 的配置；已经检查模型卷可由 UID 10001 写入并恢复只读。必须单独验证 embedding 依赖、实际下载与离线搜索，本轮没有运行这些耗时步骤，不能把 healthz 成功当成 RAG 就绪。Docling 不包含在基础镜像中。

镜像使用发行版版本标签，尚未建立完整依赖锁文件或固定 digest 的发布流程。构建会访问官方系统软件源与 Python 软件源；VPN/代理连通性需要分别在宿主机、WSL、Docker 构建环境中检查，本部署不自动改代理。

## 服务与持久化

- 宿主机只开放 Gateway 的 `127.0.0.1:8080`；可以用 `GATEWAY_HOST_PORT` 改端口。Agent 和 Redis 无宿主机端口。
- Gateway 使用 `http://agent:8000`，容器内的 localhost 指向本容器；`/readyz` 调用 Agent `/healthz`。M2.1默认开启Redis+Lua限流，按普通/模型/上传分别计数；ready额外执行PING，Redis不可用返回503。可显式设 `GATEWAY_RATE_LIMIT_ENABLED=0` 关闭，完整参数见根目录docs/RATE_LIMITING.md。
- 一个Uvicorn worker，Compose默认`KNOWLEDGE_REVIEW_MODEL_WORKERS=2`，7个JSON模型入口、SSE、8个后台提交/继续入口和标题共用实际工作槽，运行中同步工作不会因HTTP等待取消而提前释放；独立Python默认4。Gateway和Agent镜像使用UID/GID10001，`init: true`帮助回收子进程。
- Compose默认`GATEWAY_ADMISSION_ENABLED=1`，单Gateway模型并发2、FIFO等待8、正文总预算4194304字节、等待2000ms。可用`GATEWAY_MODEL_CONCURRENCY`、`GATEWAY_MODEL_QUEUE_LIMIT`、`GATEWAY_MODEL_QUEUE_BYTES`及`GATEWAY_MODEL_QUEUE_TIMEOUT_MS`调整；完整取舍见根目录docs/ADMISSION_CONTROL.md。多Gateway共享Redis额度，但队列/Permit各自独立。
- `agent_data` 保存 Python 原有 `.data` 目录，包括 SQLite、导入资料和锁文件；`agent_models` 保存模型缓存。新卷由镜像目录初始化所有权。
- 原 Windows 数据库和文档没有迁入容器。绝对路径参与 source_id，旧数据迁移需要单独验证，不能直接复制原数据库当成已迁移成功。
- `depends_on` 只控制启动等待；上游运行中故障仍由网关处理，健康检查不自动重启服务。

停止服务并保留业务卷：

```sh
docker compose -p knowledge-review-gateway -f deploy/compose.yaml down
```

不要对真实业务项目执行 `down -v`。各阶段验收使用单独的验证项目和全新的合成数据卷，清理仅针对经过名称及标签核验的该阶段资源；最新M2.3.1项目名见上文。

## 可控集成验证

无需模型凭据，以下 Key 是公开演示值：

```sh
export GATEWAY_API_KEY=public-synthetic-compose-test
docker compose -p gateway-m13-validation -f deploy/compose.yaml -f deploy/compose.test.yaml up -d --build --wait
python3 tests/compose_smoke.py --port 18080 --key public-synthetic-compose-test --phase create --report evidence/m13-compose-create.json
docker compose -p gateway-m13-validation -f deploy/compose.yaml -f deploy/compose.test.yaml restart agent gateway
python3 tests/compose_smoke.py --port 18080 --key public-synthetic-compose-test --phase restore --report evidence/m13-compose-restore.json --state evidence/m13-compose-create.json
docker compose -p gateway-m13-validation -f deploy/compose.yaml -f deploy/compose.test.yaml down -v
```

`compose.test.yaml` 使用 Compose `!override` 和可选 env_file，需支持这些语法的 Compose；本机已验证配置解析的版本是 v5.1.4。测试模型服务只执行固定协议响应，不提供真实 AI 能力。验证脚本检查文档导入、真实 Agent 工具调用后的 SSE 结果、SQLite 会话及重启恢复；Redis PING、容器健康与挂载还需结合实际 Compose 检查记录。

M2.1双实例复检另加`-f deploy/compose.validation.yaml`，公开Key同上；本次使用端口18180/18181、`GATEWAY_RATE_LIMIT_NORMAL=7`、独立`test:m21:recovery:20261018`前缀及`gateway-m21-recovery-20261018`项目。两个Gateway必须配置相同prefix/key_id/额度才共享计数。为分别验收突发与业务链路，仅在合成测试Redis中删除该前缀的精确normal key，没有清空数据库。实际Compose版本为v5.6.0；生产默认额度保持不变。
