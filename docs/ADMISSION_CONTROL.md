# M2.2：耗时请求的并发控制与有界排队

2026-10-20更新：M2.3.3已将8个后台202提交/继续入口和标题接入同一实际工作池，新增受Key保护的GET /agent-metrics。202返回不归还Python槽，显式pause请求取消，Future退出才释放；本文旧阶段的未实现描述保留作历史。完整解释与限制见[JOB_GOVERNANCE.md](JOB_GOVERNANCE.md)，最终结果见evidence/m233-verification-20261020.json。

Redis回答“这个窗口来了多少请求”，不能回答“现在还运行多少任务”。本阶段在它后面增加本地准入，并让Python的JSON/SSE入口共用实际工作槽。请求/成功响应及数据库结构不变；新增加的行为是容量耗尽时返回503。

2026-10-19更新：M2.3.2已增加请求范围模型HTTP取消，详见docs/CANCELLATION.md。本文M2.2的“超时后模型继续完成”及仅排队FIN撤回属于历史验收；当前执行中模型请求的FIN也表示撤回，实际工作槽仍等清理/线程退出才释放。

## 请求怎样经过两层控制

```text
路由/Key/请求头检查 → Redis额度 → 完整接收有上限的JSON正文
  → Gateway本地准入：立即获得名额，或FIFO等待，或503
  → 连接Python → Python共享实际工作槽：立即提交或503
  → 原有业务/模型/校验/保存 → 返回JSON或SSE
```

网关按现有`Route.model_request`选择ask、turns、stream、retry、practice、answers、grade和review等路由。SSE的请求正文仍是JSON，因此可以与普通JSON模型请求进入同一个等待队列。上传不进这个队列，保留固定缓冲传输；元数据读取和健康探针不消耗模型名额。总连接上限仍是128，没有额外承诺为普通请求预留64条连接。

Redis拒绝是429；队列满、排队正文预算不足或排队超时是503，错误码`GATEWAY_OVERLOADED`及`Retry-After: 1`。Python实际工作槽满是503和原FastAPI detail格式。Redis不可用仍为503 `REDIS_UNAVAILABLE`，没有混用错误原因。先通过限流又被过载拒绝的请求仍消耗一次窗口额度。

## C++代码为什么这样组织

| 文件/对象 | 职责与理由 |
| --- | --- |
| `src/admission.hpp/.cpp` `AdmissionController` | 保存当前名额、FIFO条目和已排队正文总字节。单个io_context线程访问，无需mutex，不在网络等待期间占线程。 |
| `Permit` | move-only RAII所有权；转移不增加名额，reset/析构只归还一次。避免在每个成功/错误分支手工减计数。 |
| `Waiter` | 一个排队条目、一个steady_timer及完成回调。超时/取消都先从队列删除、返还字节预算，再做后续操作。 |
| `ProxySession` | 只有获得Permit才开始连接上游；持有Permit直到代理finish。结束路径统一撤销排队、取消监听和释放名额。 |
| `Listener` | 为本进程所有Session提供同一个Controller；停机先关闭准入，再清理会话。 |

Controller通过`post()`交付结果，避免回调在修改队列的栈中重入。释放名额时还检查等待截止时间，已经过期但timer回调尚未执行的条目不会被放行。队列监听使用独立cancellation slot，撤销排队监听不会误取消随后开始的HTTP响应写操作；监听回调持有Session，保证其取消信号一直存活到回调完成。

正文先按1MiB上限接收，再参与排队预算；排队名额不被慢速上传正文占住。4MiB预算只计算排队JSON正文，不代表进程全部内存：执行中的正文、读取中的正文、每连接头部/传输缓冲仍分别受原有上限和总连接数约束。

## 排队连接约定

排队期间用异步可读等待加非阻塞peek观察关闭，不为排队请求创建线程。该阶段客户端发送FIN视为撤回，移除队列项并返还字节；**不支持排队时shutdown(SHUT_WR)后仍等待响应**。TCP的FIN不能证明客户端读端也已关闭，这是本项目明确选择的接口约定，不是完整检测所有TCP断连。

每连接只处理一个请求，不支持HTTP流水线，额外数据会关闭连接。M2.2仅监听排队撤回；M2.3.2已把FIN撤回和实际模型HTTP取消扩展到执行中的模型请求，协议变化与阻塞工具边界见取消说明。

## Python为什么还需要一层

网关结束HTTP请求后，Python同步SDK调用可能还在执行。此时只归还C++ Permit再放行新请求，会低估实际工作数量。因此复用`agent_stream.StreamRunner`，让以下7个同步函数加`@streams.bounded`，与SSE共用提交入口：

`ask`、`submit_turn`、`retry_turn`、`create_practice`、`submit_practice_answer`、`retry_practice_grade`、`review`。

装饰器保留函数签名，FastAPI继续校验原来的Schema、路径参数与BackgroundTasks注入；原业务函数在线程池中执行。`_submit()`在提交前非阻塞申请BoundedSemaphore；满则拒绝，不把工作堆到ThreadPoolExecutor的无界队列。每次提交对应一个真实Future，完成回调归还名额；JSON等待使用shield，取消HTTP等待不会取消Future或提前归还名额。异常状态仍传播给原错误处理，迟到异常也会被取出，避免无人观察的Future警告。

JSON名额只覆盖装饰函数实际执行，原BackgroundTasks标题、读取触发的标题、其他后台202作业未纳入。SSE的标题后处理原本就在worker内，仍占名额直到完成。不声称覆盖整个应用的全部模型任务。

## 配置与运行

| 环境变量 | 默认值和范围 |
| --- | --- |
| `GATEWAY_ADMISSION_ENABLED` | 原生0，Compose1；只接受0/1 |
| `GATEWAY_MODEL_CONCURRENCY` | 2；1–128，每网关进程 |
| `GATEWAY_MODEL_QUEUE_LIMIT` | 8；1–128，实际还受总连接上限约束 |
| `GATEWAY_MODEL_QUEUE_BYTES` | 4194304；1–134217728，排队正文总量 |
| `GATEWAY_MODEL_QUEUE_TIMEOUT_MS` | 2000；1–60000 |
| `KNOWLEDGE_REVIEW_MODEL_WORKERS` | 独立Python默认4，Compose默认2；1–32，每应用进程 |

这些是学习项目的可调基线，不是压测容量结论。排队有独立截止时间；执行期限从获得网关名额、开始上游时计算。多台Gateway共享Redis到达额度，各自有独立准入队列；共用单个Python进程时，其实际工作槽对上述JSON/SSE入口共同生效。多个Python进程之间不共享任务名额。

按`deploy/README.md`导出更新后的Python上下文并启动Compose，默认开启准入。直接运行原生网关需显式设置`GATEWAY_ADMISSION_ENABLED=1`。已有M1.3适配的独立项目用以下命令升级；工具先校验全部版本，重复执行为空操作：

```sh
python3 tools/apply_knowledge_review.py --project '/mnt/e/agentic project/knowledge-review' --apply
```

Ubuntu测试：

```sh
cmake -S . -B "$HOME/build/ai-gateway-m22" -G Ninja \
  -DCMAKE_BUILD_TYPE=Debug -DBOOST_ROOT="$HOME/build/ai-gateway-deps/boost_1_88_0"
cmake --build "$HOME/build/ai-gateway-m22" -j2
ctest --test-dir "$HOME/build/ai-gateway-m22" --output-on-failure

# 在Python项目目录及其已安装基础依赖的环境中运行
python -m unittest -v test_agent_sse test_agent_admission
```

## 验证及剩余范围

普通构建及ASan/UBSan通过同批72项：23项HTTP、22项SSE/上传、16项Redis、11项准入。准入覆盖2执行+8FIFO、503、JSON/SSE共享、排队超时/断连/字节预算、上游失败/代理超时/停机、100次排队断连FD11→11、半关闭约定与非法容量配置；两套构建不相加为144项独立测试。

Python新增7项和原SSE12项通过，相关原有80项回归通过；版本升级/构建隔离工具4项通过。真实Gateway→Redis→Uvicorn→可控模型8项通过：2条JSON超时后仍占Python槽，JSON/SSE均被实际工作守卫拒绝且没有创建新轮次，工作真正结束后恢复。两个超时请求仍最终保存一次，这正是尚未实现取消的边界。

M2.2容器Release构建、首次业务6项、替换恢复5项及Redis故障恢复另行记录；不把普通健康检查当作容器内全量并发压测。证据集中在`evidence/m22-*`。后续M2.3.1已提供Gateway本地指标（见docs/METRICS.md）；M2.3.2已实现请求范围模型HTTP取消；所有后台202/独立标题治理、分布式并发名额、Python实际工作指标及真实embedding/RAG部署仍未完成。
