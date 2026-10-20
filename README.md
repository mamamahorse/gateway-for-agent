# C++ AI Gateway

独立的 C++ 后端学习项目，通过 HTTP 连接 Python Knowledge Review。

**2026-10-20：M2.3.3核心范围已完成验收并同步独立Python项目。** JSON/SSE、Redis限流、Permit/FIFO、模型HTTP取消保留；新增8个后台提交/继续入口与标题共享实际工作槽，以及Key保护的`GET /agent-metrics`。后台202和提交者断连不提前释放名额，原pause/continue控制实际工作。普通与ASan/UBSan同批103项、Python相关255项、工具4项通过；真实隔离与容器各同12组治理检查通过。没有外部模型或真实embedding/RAG部署验收。架构见[AI_GATEWAY_PLAN.md](AI_GATEWAY_PLAN.md)，详细改动理由、代码职责、接口、指标口径及运行方法见[后台任务治理说明](docs/JOB_GOVERNANCE.md)，验收汇总见[evidence/m233-verification-20261020.json](evidence/m233-verification-20261020.json)。旧章节保留为阶段历史。

## 当前可以做什么

- 在单个 Asio I/O 循环里异步接收客户端请求、连接上游、读写 HTTP；没有给每个请求创建线程。
- 用 `X-API-Key` 验证业务请求；Key 未配置时启动失败。健康探针不需要 Key。
- 按明确的方法与路径白名单转发已接入的 JSON、SSE 和文档上传 API，保留 query、正文、业务状态码和响应头。
- SSE 使用 16 KiB 缓冲，等每块下游写完成后再读下一块；不会等待上游全部结束，也不拆解或重新编码 SSE/UTF-8 内容。
- 上传保留 multipart boundary，将正文以固定缓冲重新编码为上游 HTTP chunked；支持鉴权后的 `100-continue`，并能透传上游提前拒绝的响应。
- `/livez` 检查网关存活；`/readyz` 异步检查配置的上游探针。后端故障时 live 仍可用、ready 返回 503。
- 为请求头/正文读取、上游连接、请求执行和下游响应写入设置截止时间；SSE 另有首数据、空闲、写入和总期限；统一清理连接与定时器。
- 限制头部、JSON 正文、上游响应正文与总连接数；处理 HTTP chunked framing 和连接专属头。
- 模型请求默认基线为2个执行名额、8个FIFO等待、4MiB排队正文和2秒等待期限；满/超时503，Redis额度耗尽429。原生准入默认关闭、Compose默认开启。
- Python提交前申请共享工作槽，实际Future结束才归还；网关超时后仍在执行的工作不会被误计为已经结束。7个JSON模型入口、SSE、8个后台提交/继续入口和标题共用；后台202不提前释放实际槽，Python满时503且不创建新任务记录。
- 已接入模型JSON/SSE的完整正文后监听FIN/RST，FIN（含写半关闭）表示撤回；普通非模型请求保留半关闭读取响应。Python取消实际模型HTTP、在工具之间/最终提交前检查；不强杀阻塞工具或回滚已保存结果。
- 输出脱敏 JSON 访问日志。不会记录 API Key、问题、回答或完整 URL/query；POST 失败不自动重试。
- `GET /metrics`使用相同API Key读取本地请求/错误计数和准入指标，不访问Redis或Agent。观察到的业务失败与HTTP状态分开；指标与健康轮询不累计为业务请求。
- `GET /agent-metrics`用相同Key读取Python实际工作快照，区分预留、运行、退出与取消；Agent故障时该端点不可用。四类后台工作可通过原pause/continue操作恢复，标题最多一个、忙时跳过，无新增后台等待队列。

当前环境为 Ubuntu 24.04.4、GCC 13.3、CMake 3.28、Boost 1.88.0、OpenSSL 3、C++20。M1.1/M1.2 使用系统 Boost 1.83；M2.1 已在独立构建目录升级到校验后的官方1.88，不替换系统版本。新增的 Boost.Redis 继续使用同一 Asio 事件循环。

## 在你的 Ubuntu 中编译

构建依赖本轮已经准备。新环境需要安装 `build-essential cmake ninja-build curl bzip2 libssl-dev python3 redis-server`；Boost 1.88 使用下面的校验下载脚本，系统仓库的1.83不满足当前构建。

在 WSL Ubuntu 终端进入当前源码目录：

```sh
cd /mnt/c/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78
sh tools/prepare_boost.sh
cmake -S . -B "$HOME/build/ai-gateway-m233" -G Ninja -DCMAKE_BUILD_TYPE=Debug -DBOOST_ROOT="$HOME/build/ai-gateway-deps/boost_1_88_0"
cmake --build "$HOME/build/ai-gateway-m233" -j2
ctest --test-dir "$HOME/build/ai-gateway-m233" --output-on-failure
```

源码当前保存在聊天工作区；本阶段构建产物在Ubuntu的`/home/mamahorse/build/ai-gateway-m233`，旧阶段构建目录保留。VS Code通过WSL打开同一个源码目录即可；编译器和调试器使用Ubuntu内的版本。`sources/`仍是只读同步资料，未向其中写入。

## 先运行一个可观察的代理闭环

Ubuntu 终端一，启动公开合成测试后端：

```sh
python3 tests/fake_agent.py --port 8000
```

Ubuntu 终端二，进入同一源码目录后启动网关：

```sh
export GATEWAY_API_KEY='dev-only-change-me'
export GATEWAY_UPSTREAM='http://127.0.0.1:8000'
"$HOME/build/ai-gateway-m233/ai-gateway"
```

另一个终端发起请求：

```sh
curl -i http://127.0.0.1:8080/livez
curl -i http://127.0.0.1:8080/readyz
curl -i http://127.0.0.1:8080/api/documents
curl -i -H 'X-API-Key: dev-only-change-me' 'http://127.0.0.1:8080/api/documents?collection_id=root'
curl -fsS -H 'X-API-Key: dev-only-change-me' http://127.0.0.1:8080/metrics
```

预期分别为存活 200、就绪 200、未鉴权 401、成功代理 200。这里的 Key 是公开的本地演示值，实际配置通过环境变量提供，不提交 `.env`。合成后端只用于观察 HTTP 传输，不提供 RAG/Agent 业务。两个服务都用 Ctrl+C 停止；集成测试会自动启动和回收自己的进程，本轮没有留下常驻测试服务。

沿用上述合成后端，可以观察三个间隔发送的事件与最终 done：

```sh
curl --no-buffer -i -H 'X-API-Key: dev-only-change-me' \
  -H 'Content-Type: application/json' -d '{}' \
  http://127.0.0.1:8080/api/conversations/public-chat/turns/stream
curl -i -H 'X-API-Key: dev-only-change-me' -F 'files=@README.md;type=text/markdown' \
  http://127.0.0.1:8080/api/documents
```

第一个示例展示传输时序，不是调用真实 Agent。第二个示例在合成后端返回接收字节数与哈希；真实 Knowledge Review 返回其原有文档数组。M1.3 已在独立 Python 项目加入 `/turns/stream`；真实请求需使用有效 TurnRequest，不能照抄合成后端的空 JSON。接口及运行方法见下方 M1.3。

连接真实 Knowledge Review 时，将 `GATEWAY_UPSTREAM` 指向它的 HTTP origin。M1.3 已新增 Python `/healthz`，当前使用默认探针即可。若连接尚未应用适配的旧 Python 版本，可临时设置：

```sh
export GATEWAY_UPSTREAM_HEALTH_PATH='/api/documents'
```

这项探针只证明实际HTTP入口能返回成功响应，不检查embedding或云端模型。早期接入检查仅使用隔离副本；M1.3/M2.2的最小HTTP适配已通过版本校验应用到独立Python项目，原业务数据保留。若Python仍在Windows、网关在WSL NAT，两侧的127.0.0.1含义不同，需要配置可达的Windows主机地址。

## 配置

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `GATEWAY_API_KEY` | 必填 | 业务 API 的 Key，最多 512 字节，不允许换行 |
| `GATEWAY_LISTEN_ADDRESS` | `127.0.0.1` | 监听地址 |
| `GATEWAY_PORT` | `8080` | 网关端口 |
| `GATEWAY_UPSTREAM` | `http://127.0.0.1:8000` | 固定上游，只接受 HTTP origin；当前不支持 TLS、IPv6 origin 或路径前缀 |
| `GATEWAY_UPSTREAM_HEALTH_PATH` | `/healthz` | 后端就绪探针路径 |
| `GATEWAY_HEADER_TIMEOUT_MS` | `5000` | 客户端头部/正文读取及下游写入的阶段期限 |
| `GATEWAY_CONNECT_TIMEOUT_MS` | `3000` | DNS 与连接建立期限 |
| `GATEWAY_REQUEST_TIMEOUT_MS` | `10000` | 普通上游请求总期限 |
| `GATEWAY_MODEL_TIMEOUT_MS` | `180000` | 问答、出题、评分等上游请求总期限 |
| `GATEWAY_READINESS_TIMEOUT_MS` | `1000` | 上游探针总期限 |
| `GATEWAY_STREAM_TIMEOUT_MS` | `300000` | SSE 总期限，心跳不会延长它 |
| `GATEWAY_STREAM_FIRST_BYTE_TIMEOUT_MS` | `10000` | 上游响应头通过校验后等待首个正文数据的期限 |
| `GATEWAY_STREAM_IDLE_TIMEOUT_MS` | `45000` | 上一块转发完成后，等待后续上游正文数据的期限 |
| `GATEWAY_STREAM_WRITE_TIMEOUT_MS` | `15000` | SSE 响应头、每块正文及最终 HTTP chunk 的写入期限 |
| `GATEWAY_UPLOAD_TIMEOUT_MS` | `120000` | 上传、上游接收与处理的总期限 |

Redis检查和模型排队期限为1–60000ms，其余毫秒配置为1–3600000范围内整数，端口1–65535。固定限制：头部32KiB、JSON请求1MiB、非流式响应4MiB、SSE累计64MiB、上传416MiB、总连接128；每文件/批次仍由Python校验。上传/SSE每方向16KiB正文缓冲，另有每端最多64KiB解析输入缓冲，内核缓冲额外计算。达到总连接上限的新连接会关闭；M2.2在此基础上增加独立模型Permit及FIFO，配置表和范围见docs/ADMISSION_CONTROL.md。

路由以 [policy.cpp](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/src/policy.cpp>) 为准：原 19 类 JSON API，加上 `POST /api/documents` 与 `POST /api/conversations/{id}/turns/stream`，共 21 个方法/路径模式。白名单按方法匹配，未知路径、未开放方法、尾斜杠形式返回 404。尚未开放 PDF 资源读取、网页静态文件、后台清单任务或检索审核接口。

## 一次请求如何执行，为什么这样写

1. **先读请求头，再检查路由和 Key。** 验证通过后才读有界 JSON 正文，避免为未授权请求缓冲大正文。解析器也会独立拒绝明显超限的 HTTP 消息。
2. **重建上游请求的连接信息。** 保留业务 query/body，清除客户端 Key、伪造的用户/转发身份和 hop-by-hop 头，重设 Host、链路 ID 与消息长度。`Connection` 声明的额外头也会清除，避免跨连接错误转发。
3. **异步解析地址、连接、写请求、读响应。** 网络等待将执行权交回事件循环，所以一个耗时请求期间 `/livez` 仍能及时响应。Asio 的地址解析可能使用内部辅助线程，不是每个业务请求创建线程。
4. **读取响应头后再选择正文模式。** JSON 路径继续使用有界完整正文；新 SSE 路径的成功响应必须是未压缩的 `text/event-stream`，否则在提交下游头前返回 502。合法 SSE 转为 `buffer_body` 解析器，按实际可用字节逐块转发；上游 4xx/5xx 仍以原状态及有界正文透传。
5. **正常返回或错误都汇入清理流程。** 上游故障为 502、上游截止时间为 504、客户端读取超时为 408；上游本身返回的 422/502 等状态透传。HTTP 200 + 业务 `status=failed` 保留原样。

关键代码及修改原因：

| 文件 | 职责与原因 |
| --- | --- |
| [main.cpp](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/src/main.cpp>) | 解析小范围环境配置并启动服务。错误只输出配置名和约束，避免把上游凭据或 Key 值打到日志中。 |
| [gateway.hpp](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/src/gateway.hpp>) | 保存当前确实需要的配置；没有引入通用配置框架。 |
| [gateway.cpp](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/src/gateway.cpp>) | `Listener` 持有在途 `ProxySession`；Session 拥有两端连接、解析器、响应和定时器。这样异步回调运行时不会引用已销毁的局部对象。 |
| [policy.cpp](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/src/policy.cpp>) | 明确列出允许的真实接口，集中处理 Key 与连接头。路由是数据表和普通函数，不建立策略继承层。 |
| [test_proxy.py](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/tests/test_proxy.py>) | 启动实际 C++ 可执行文件，以网络黑盒测试可观察行为，避免只测试实现细节。 |
| [test_streaming.py](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/tests/test_streaming.py>) | 对每个事件记录到达时间，检查上传/慢读内存、消息完整性、超时及文件描述符/线程回收。 |
| [knowledge_review_smoke.py](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/tests/knowledge_review_smoke.py>) | 将实际 Python 运行源码、公开 examples 和 web 复制到临时目录，不复制配置、数据库、私人文档或模型。验证完成后删除副本，原应用保持只读。 |

Session 的异步完成回调持有 `shared_ptr`，使缓冲与 socket 保持有效；定时器通过 `weak_ptr` 观察 Session，避免循环持有。阶段定时器还带版本号，旧阶段的迟到回调不能终止新阶段。`finish()` 幂等关闭连接、取消定时器并移除在途 Session；关闭 socket 后仍可能收到回调，所以每个回调先检查结束状态。SIGTERM/SIGINT 停止接收新连接并取消在途网络工作，事件循环处理完取消回调后退出。

### M1.2 为什么这样改

流式改造集中在 [gateway.cpp](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/src/gateway.cpp>)，复用现有 Session，未另建网络框架：

- `start_stream()/read_stream_block()` 在只读完头、尚未开始解析正文时转换解析器；每次设置固定缓冲，按实际填充字节数发送，再等待写完成。`need_buffer` 是需要下一块缓冲的正常状态。HTTP chunk、TCP 包和 SSE 事件边界互不相等，因此不逐块解码 UTF-8，也不按一个读取回调解析一个事件。
- 等待异步写完成限制了继续向发送队列添加数据的速度，不表示浏览器已经处理该事件。内核缓冲仍会暂存数据；慢客户端最终由写期限结束。正常完成才发送 HTTP 最终 chunk；上游不完整、读写超时或总期限到达时直接终止，不能把流中错误换成新的 HTTP 504 或补造 done。
- `receive_body()/read_upload_block()` 对上传采用同样的一读一写推进方式，只保留 MIME 头与 boundary，不在 C++ 解析 Markdown/PDF。上游响应头与上传发送可并行等待，后端提前拒绝时立即停止继续转发并返回其错误。
- `100-continue` 在路由、大小、类型和 Key 检查通过后才发送给客户端，并从上游请求删除 Expect，避免双重握手。非上传路径的 Expect 仍返回 417。
- 上传读取使用独立的应用层计时器，超时先发送 408 再清理。测试曾发现直接使用 socket 读取超时会先关闭连接、使 408 无法送达，已据此修复；取消旧读取时同步撤销其计时器版本，防止迟到超时误伤后续写入。
- 缓冲、HTTP chunk 对象和头部 serializer 都由 Session 持有，直到对应异步回调完成；关闭连接时不提前销毁仍被 I/O 引用的内存。流模式继续使用原有幂等清理与总截止时间。

## 验证记录与边界

M1.1 历史结果（2026-10-16）：

- Ubuntu Debug 构建成功，23 项 HTTP 黑盒检查通过；CTest 中登记为一个集成测试入口。
- ASan + UBSan 构建成功，同样的 23 项检查通过，未报告内存或未定义行为错误。它们是同一批用例，不相加为 46 项独立测试。
- 覆盖 Key、状态/正文/query 透传、业务失败 200、chunked 双向 framing、动态 hop-by-hop 头、错误响应、大小限制、超时、不重试 POST、异步健康检查、SIGTERM 和连接回收。
- 连续 100 次客户端断连后，等待 FD 数量稳定回到基线容差内；在进程中观察到 Linux `eventpoll` 描述符。
- 真实 Knowledge Review 的隔离副本完成 7 项接入检查：探针、文档列表/详情、SQLite 会话创建/历史、FastAPI 422 透传与数据隔离；61 个被复制的运行源码文件哈希前后相同。
- 安装依赖时 Ubuntu 能访问软件源与 PyPI。没有修改 WSL 的代理、网络或 VPN 配置，不能据此推断所有外部模型服务都可达。

记录见 [普通构建测试](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/evidence/m1-normal-tests-20261016.log>)、[内存检查测试](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/evidence/m1-sanitizer-tests-20261016.log>)、[真实后端接入记录](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/evidence/knowledge-review-smoke-20261016.json>)。

M1.2 新增逐事件到达时间、UTF-8 跨块、首次/空闲/总截止时间、心跳、慢读背压、上游断流、100 次流式断连回收，以及 multipart/未知长度/早拒绝/100-continue/大文件内存测试。当前真实后端隔离检查已扩展到 **11 项**，包括经过网关的 Markdown/PDF 导入及 PDF 统一结构读取；原 61 个运行源码文件哈希前后相同。M1.2 当时也验证了原 Python 缺少 SSE 路径并返回 404；M1.3 已新增真实适配，下述历史记录保留。

本批最终结果为 **45 项黑盒检查（原有 23 项 + 流式/上传 22 项）通过**；ASan/UBSan 同样通过这 45 项，未报告内存或未定义行为错误。CTest 登记为两个集成测试入口，不把同批普通/内存检查相加为 90 项独立测试。

本机公开合成数据的观测：普通构建的三个进度事件约在 1/251/501 ms 到达，HTTP 流约 752 ms 完成；100 次流式断连前后，网关 FD 均为 9、线程均为 2。32 MiB 上传到中途时，普通构建 RSS 比基线增加约 384 KiB，ASan 构建约 10240 KiB，均低于该探针的 12 MiB 增长上限；ASan 的额外开销不代表正文被完整缓冲。大文件内存/完整性探针有独立的 30 秒传输预算，停滞上传另用短期限检查 408。上述数字是单次环境观测，不是生产容量或稳定吞吐结论。

M1.2 记录见 [验收汇总](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/evidence/m12-verification-20261016.json>)、[普通构建测试](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/evidence/m12-normal-tests-20261016.log>)、[内存检查测试](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/evidence/m12-sanitizer-tests-20261016.log>)、[真实上传接入记录](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/evidence/knowledge-review-m12-smoke-20261016.json>)。测试计数和实测时序以本批最终记录为准，不与历史结果相加。

重新运行真实后端隔离检查，使用本轮建立的 Ubuntu 虚拟环境：

```sh
"$HOME/build/knowledge-review-smoke-venv/bin/python" tests/knowledge_review_smoke.py \
  --project '/mnt/e/agentic project/knowledge-review' \
  --binary "$HOME/build/ai-gateway-m233/ai-gateway"
```

该虚拟环境只安装 Python 应用基础依赖，没有下载 embedding 模型，也没有进行云端模型调用。以上不代表 RAG、模型答案质量或原项目全量回归通过。

M2.2已补共享准入与实际工作槽，M2.3.1提供基础指标，M2.3.2实现请求范围模型HTTP取消。尚未实现：全部后台202/独立标题治理、Python实际任务指标、网页SSE交互和完整真实RAG部署。取消不强杀CPU/文件/SQLite/DNS等阻塞操作，不能证明供应商停止远端算力；模型请求完整正文后的FIN按撤回处理，普通非模型GET仍支持写半关闭。JSON继续完整缓冲，SSE与上传使用固定缓冲；32MiB合成上传不等于已验证416MiB整批PDF。


## M1.3：真实 Agent 事件与 Docker 联合运行

已将经过测试的最小适配应用到独立项目 `E:\agentic project\knowledge-review`，没有导入它的配置、访问模型服务或修改原业务数据。仅调整两个运行文件，新增一个运行模块和一个测试文件，并同步相关说明；没有重构数据库或网页。

| 改动 | 为什么这样做、行为如何改变 |
| --- | --- |
| [web_app.py](<E:/agentic project/knowledge-review/web_app.py>) 的 `create_app()/execute_turn()/generate_answer()` | 新增 healthz 和流式入口，复用原会话锁、幂等、权限检查及事务保存；原 JSON 接口保持。成功保存后才能发布 result。 |
| [agent_loop.py](<E:/agentic project/knowledge-review/agent_loop.py>) 的 `run_agent()` | 通过可选回调报告真实模型/工具阶段；不发送隐藏思维链，不把已经生成的答案拆成假流式文本。 |
| [agent_stream.py](<E:/agentic project/knowledge-review/agent_stream.py>) | 用有界线程池和线程安全队列衔接同步业务与异步 HTTP。最多4个 SSE 实际工作，无等待队列；满时 HTTP 503。断连后实际工作结束才释放名额。 |
| [apply_knowledge_review.py](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/tools/apply_knowledge_review.py>) | 所有源文件哈希匹配才应用补丁，已经应用则跳过，独立项目有新修改则拒绝覆盖。完整补丁保存在 integrations/knowledge-review。 |
| [prepare_agent_context.py](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/tools/prepare_agent_context.py>) | 从运行代码和公开资产导出单独构建上下文，排除 .env、私人资料、数据库、模型和测试。 |
| [compose.yaml](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/deploy/compose.yaml>) 与两个 Dockerfile | 用服务名连接、只发布网关端口、独立数据卷和非 root 应用进程。Redis 现阶段只启动，不把 PING 当成限流已经实现。 |

完整事件契约、预算、异常语义及逐函数解释见 [Python SSE 适配说明](<E:/agentic project/knowledge-review/docs/gateway-sse.md>)；Docker 启动、环境配置、模型准备和停止方法见 [部署说明](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/deploy/README.md>)。本批 C++ 传输代码未改动，ASan/UBSan 的45项结果仍为前一阶段记录。

新接口为 `POST /api/conversations/{identity}/turns/stream`，正文沿用原 TurnRequest：

```json
{
  "request_id": "one-stable-id-per-submission",
  "document_ids": ["<真实文档ID>"],
  "focus_document_id": "<同一个真实文档ID>",
  "focus_source_id": "<文档详情返回的当前source_id>",
  "question": "请解释这份资料",
  "strict": false,
  "response_language": "zh",
  "retrieval_mode": "dense"
}
```

使用 `curl --no-buffer`、JSON 正文和 `X-API-Key` 读取 `meta/progress/result/error/done`。这是真实执行进度 SSE，最终回答仍整体校验和保存，不是逐 token 输出。健康接口不验证模型配置和 RAG 可用性。资料范围预检查在 HTTP 头前完成；流中错误用安全的 error/done，断流无 done 时从 messages 查询业务状态。

在已有 Ubuntu Python 基础依赖环境中启动真实后端：

```sh
"$HOME/build/knowledge-review-smoke-venv/bin/python" -m uvicorn web_app:app \
  --app-dir '/mnt/e/agentic project/knowledge-review' --host 127.0.0.1 --port 8000 --workers 1
```

该命令会运行原项目，实际模型配置继续由原项目管理；后续是否触发语义检索仍取决于 Agent 选择。前文合成后端应先停止，避免端口冲突。若要本地搜索，须另装 requirements-local.txt 并准备模型；本轮测试虚拟环境没有这些模型。

本轮通过的验证分别记录，不与 M1.2 历史成绩相加：

- 新增 Python SSE 测试12项，原有相关回归80项（51项 Agent/会话/网页/记忆 + 29项练习/学习/模型客户端）。这是相关回归，不是原项目全量。
- 真实 Gateway → Uvicorn → Agent → 本地可控模型 HTTP 服务10项链路检查：真实 read_source、逐事件到达、结构化结果提交、失败保存、幂等重放、四槽过载拒绝及名额回收。
- 实际 Python Markdown/PDF/SQLite 隔离接入11项；62个运行源码文件在检查期间哈希保持不变。
- 构建上下文排除私有状态、补丁拒绝覆盖新版本的保护测试2项，在 Ubuntu 通过。Windows 沙箱的临时目录权限问题不属于应用失败，未为此修改业务权限。
- Docker Release 镜像实际构建成功，Gateway/Agent/Redis 三服务健康，Redis PONG，两个应用进程均 UID/GID 10001；可控模型测试服务没有宿主机端口。
- 容器首次业务检查6项、替换 Agent/Gateway 后恢复检查5项：同一文档 source_id、同一保存轮次、重放不新增记录。模型准备覆盖配置可写探针通过，普通 Agent 模型卷仍只读。

容器构建首次因 Docker Hub 超时失败，重试官方 Ubuntu 拉取成功后完成；没有更换镜像源、修改 VPN 或代理。验收使用全新的合成数据卷和隔离项目，完成后已删除这些测试容器与测试卷，没有留下常驻模型测试服务。

默认 Agent 镜像为 `WITH_EMBEDDINGS=0` 的轻量模式；完整本地检索需显式启用 embedding 依赖和准备模型，尚未完成该可选镜像/真实搜索验收。没有调用外部模型或验证回答质量，也没有迁移原 Windows 数据。更详细记录见 [M1.3 验收汇总](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/evidence/m13-verification-20261016.json>)。


## M2.1：共享限流已完成原生与核心容器验收

[限流实现说明](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/docs/RATE_LIMITING.md>) 包含完整配置、代码职责、取舍、运行和故障语义。

- [rate_limit.lua](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/scripts/rate_limit.lua>) 将计数、上限判断和TTL放在一次原子执行中；按 normal/model/upload 分开计数，key只含公开身份。固定窗口从首次允许的请求起算。
- [redis_limiter.cpp](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/src/redis_limiter.cpp>) 用短连接异步检查、独立总期限、固定三整数响应与64KiB输入界限，避免引入连接池。禁用EVAL重放；提交后丢失响应可能已消耗额度，返回503而不重复计数。
- [gateway.cpp](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/src/gateway.cpp>) 在Key和请求头检查后、读业务正文/发送100 Continue/连接Python之前申请额度；429带向上取整的Retry-After，Redis异常为503；live不依赖Redis，ready做PING而不消耗额度。
- 原生默认 `GATEWAY_RATE_LIMIT_ENABLED=0`，便于独立观察传输；Compose默认1并连接redis服务。启用时默认普通120、模型10、上传5次/60秒，公开 `GATEWAY_KEY_ID=default`。副本使用同一prefix/key_id和配置才共享额度。其余设置见实现说明。

本轮普通构建与ASan/UBSan均通过同批 **61项检查**（23项HTTP + 22项SSE/上传 + 16项限流），CTest为3个入口，不相加成122项。两个真实网关并发40次，额度7时放行7、拒绝33；100次Redis检查后FD9→9、线程2，未报告内存或未定义行为错误。这些是本机工程观测，不是生产容量结论。

另完成开启真实Redis的 Knowledge Review 隔离链路10项检查：执行真实Agent read_source，经最终校验保存、幂等重放、失败恢复和任务槽边界。原62个运行源码文件哈希保持；本轮没有修改原Python项目、原业务数据或调用外部模型。

记录见 [验收汇总](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/evidence/m21-verification-20261016.json>)、[普通检查](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/evidence/m21-normal-tests-20261016.log>)、[内存检查](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/evidence/m21-sanitizer-tests-20261016.log>)、[真实链路](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/evidence/m21-real-agent-20261016.json>)。

Docker初次复检曾因引擎/容器初始化异常无法完成验收；2026-10-18恢复后补齐M2.1镜像和核心容器验收，修复build阶段遗漏bzip2。共享限流、真实Agent SSE、容器替换及Redis故障恢复通过。后续M2.2的共享本地准入现已完成，完整业务取消仍待M2.3。


## 开发问题记录约定

后续开发持续维护 [开发问题与解决日志](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/docs/DEVELOPMENT_LOG.md>)。每个问题记录现象、定位步骤与证据、已确认原因及推测、修复方法和理由、验证结果、剩余限制。开始实质修改前阅读相关记录；修复或里程碑完成后追加，保留历史失败，不把未执行验证写为通过，不记录密钥或私人业务内容。


## 2026-10-18 Docker 首次复检（失败历史）

用户重启后，版本API曾恢复并返回29.8.2；合并配置和Agent镜像构建通过。随后发现实际容器停在Created，Gateway构建RUN未执行完成；Docker的WSL可运行shell、内存充足，但runc初始化等待。一次完整后台重启后，版本查询和hello-world仍各20秒未返回，因此本版容器验收仍未完成。

本次未修改C++或Python业务源码，没有重新运行旧61项原生测试，也没有将其计作容器通过。问题现象、定位证据、恢复尝试及清理边界见 [开发问题日志](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/docs/DEVELOPMENT_LOG.md>) 和 [本次复检记录](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/evidence/m21-docker-recheck-20261018.json>)。

## 2026-10-18 Docker 再次复检（核心验收通过）

最小容器正常退出；修复`deploy/Dockerfile.gateway`遗漏的bzip2后，Boost1.88源码校验、解压及Release编译成功。bzip2仅安装到build阶段，最终运行阶段不增加该工具，C++/Python业务源码未修改。初次失败与修复后的构建日志分别保留。

- 两台Gateway、Agent和Redis健康，三个应用进程均使用UID10001；83个构建上下文运行文件与独立Python项目相同。
- 同一Redis额度7下，两台网关共40个并发请求返回7个200/33个429，Redis计数7；429含Retry-After，未认证请求和健康探针不扣额度。
- 实际Agent首次业务6项、替换Agent/两台Gateway后的恢复5项通过：Markdown导入、read_source工具、SSE、SQLite结果与幂等重放。最终记录首事件26ms、进度46ms、result754ms，这是单次观测，不是性能承诺。
- Redis停机时live保持200，ready和业务返回503；恢复Redis后无需重启Gateway即可继续处理业务。
- 已清理本次项目5个测试容器、2个合成卷和网络，并核实无该项目残留；构建镜像保留。

使用独立合成数据与本地可控模型服务，没有外部模型调用、真实回答质量或embedding搜索验证，也没有重跑旧61项原生测试。完整汇总见 [本次容器验收记录](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/evidence/m21-docker-recovery-20261018.json>)，具体检查见 [共享限流与恢复记录](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/evidence/m21-compose-recovery-checks-20261018.json>)。部署命令见 [联合部署说明](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/deploy/README.md>)。

## M2.2：共享本地准入与实际工作槽（2026-10-18—19完成）

Redis限流后，模型请求在接收受限JSON正文后进入同一个本地Controller：默认2个执行名额、8个FIFO等待、4MiB排队正文和2秒等待期限。队列满/正文预算不足/等待到期503，与Redis429区分；只有拿到move-only RAII Permit才连接Python。队列FIN按撤回处理并回收条目及字节，不支持排队阶段写半关闭后继续等待；上传和普通读取不进入模型队列。

Python保留原同步业务函数，仅为7个JSON模型入口添加保留FastAPI签名的装饰器，与SSE共用提交前受限的执行器。真实Future完成才释放实际槽位；HTTP取消等待或代理超时不释放仍运行的工作。原生Python默认4、Compose默认2。JSON后台标题、读取触发标题和其他202作业尚未纳入，完整模型取消/指标接口继续留到M2.3。

实际验证：

- 10月18日普通构建及ASan/UBSan同批72项通过（23 HTTP + 22 SSE/上传 + 16 Redis + 11准入），不合计为144项独立测试。包含2执行+8FIFO、超时/断连/异常/停机及100次排队断连FD11→11。
- Python新增7项、原SSE12项及相关原有80项通过；升级/构建隔离工具4项通过。真实Gateway→Redis→Uvicorn→本地可控模型8项通过，明确观察到两条HTTP超时请求仍运行并各保存一次，期间新JSON/SSE均被实际工作守卫拒绝，结束后恢复。
- M2.2 Release容器首次6项、替换恢复5项及Redis故障恢复通过；10月19日统一响应版本号后，普通/ASan二进制重建，各定向复核11项准入，容器重新构建并再次恢复原会话5项通过。这些恢复检查不相加成额外独立功能数量。
- 最终核实两台Gateway启用准入且配置2/8/4MiB/2秒，Python配置2槽；容器内两个修改模块的哈希与独立项目及导出上下文一致。83个运行文件中只修改web_app.py和agent_stream.py，其他运行文件及网页资产不变；另同步针对性测试和交接文档。
- 清理仅限本次5个测试容器、2个合成卷和网络，项目标签查询无残留，镜像保留。未提交/推送，无外部模型调用或真实质量/完整embedding-RAG验收。

原因、类职责、完整配置、编译/测试与运行方法见 [并发与排队说明](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/docs/ADMISSION_CONTROL.md>)；结果见 [M2.2验收汇总](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/evidence/m22-verification-20261019.json>)。问题定位和修复记录已追加 [开发日志](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/docs/DEVELOPMENT_LOG.md>)。实际业务部署仍按deploy说明显式提供模型配置和独立数据卷。

## M2.3.1：本地指标与业务失败观察（2026-10-19完成）

新增Key保护的GET /metrics，使用现有网关端口，返回进程级JSON快照及no-store。可观察HTTP状态、固定结束原因、认证模型HTTP在途数、Permit/队列/正文预算、连接与拒绝、耗时及已识别业务失败；不访问Redis或Python，不增加后台线程、URL标签或外部监控服务。

JSON200.failed和SSE error/done.failed在协议范围内按代理请求计数，不会与HTTP200混为成功。JSON观察最多64KiB/深度16，SSE只保留有上限的行/事件片段；超预算/未知状态不影响原响应，不缓存完整结果流，也不据此推导业务成功率。指标是Gateway观察，不是Python任务计数或取消证明。

- 同批87项普通与ASan/UBSan检查通过（原72+新增15），新增覆盖鉴权、误报防护、BOM/换行/跨块/多行SSE、2MiB结果、断流、排队指标、Redis故障及100次快照FD9→9。两套构建不相加。
- 同一真实Agent检查分别在原生隔离环境和容器各通过8项：JSON/SSE失败总计增加2、与SQLite状态一致，成功read_source仍先返回进度再提交最终结果。供应商为本地可控协议服务，没有外部模型或质量验收。
- 容器验证两个Gateway指标独立、重建计数归零、上传/会话恢复、Redis停机快照可读及恢复；新建6项和Gateway替换恢复5项通过。仅清理本次5个容器、2个命名合成卷、Redis匿名卷和网络，镜像保留。
- Python应用及83个公开运行文件与M2.2一致，本轮没有改动独立项目代码/数据、重跑旧Python99项或把旧结果计入新验收。完整模型取消、后台202/标题治理及Python实际任务指标继续后续开发。

解释、字段口径和运行命令见 [指标说明](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/docs/METRICS.md>)，结果见 [M2.3.1验收汇总](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/evidence/m231-verification-20261019.json>)。

## M2.3.2：请求范围模型 HTTP 取消（2026-10-19完成）

新增模型请求的主动断连监听，将客户端退出及代理超时传播到Python实际模型HTTP传输。保留原同步SDK/业务函数，用请求Token和工作线程内拥有的异步httpx客户端中止I/O；连接清理、工作退出后才归还实际槽位。会话复用failed和安全取消说明，不改Schema；已提交结果保留，供应商远端算力或阻塞工具不承诺强制停止。范围内关闭SDK重试，原应用层结构化协议回退仍可用。

- 同批94项普通和ASan/UBSan通过，新增7项覆盖静默SSE/JSON、响应头前断连、FIN/RST、模型写半关闭、单次计数及100次断连FD9→9；不把两种构建相加。
- Python115项相关回归通过（原80+SSE12+共享槽7+取消16），工具4项通过。新增覆盖真实HTTP响应头/部分正文取消、结构化parse/协议回退/gzip、两个工作取消隔离、清理前不释放槽位、预启动竞态、响应头发送失败及结果排队后的标题取消。
- 真实隔离Agent与容器分别通过同9组取消、同8组指标检查；核对模型服务EOF、failed保存、没有后续模型调用、JSON/SSE超时、新工作恢复和幂等。容器新建6项、Gateway替换恢复5项、UID10001与63个Python运行文件哈希一致。
- 独立Python适配通过完整版本校验同步，既有运行/公开文件仅8个运行模块改变，另新增agent_cancel.py，75个既有运行/公开文件保持；不访问原.env/数据库/文档/模型数据。未知独立修改仍拒绝覆盖，重复检查无待写入项。
- 仅清理本阶段4个测试容器、2个命名合成卷、Redis匿名卷与网络，镜像保留。所有模型请求为本地受控协议服务，不代表真实模型质量或完整RAG验收。

设计、代码解释和准确运行方式见 [取消说明](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/docs/CANCELLATION.md>)，问题与修复见 [开发日志](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/docs/DEVELOPMENT_LOG.md>)，结果见 [M2.3.2汇总](<C:/Users/mamahorse/.codex/.chatgpt-projects/g-p-6ad08dfdf08c8191a4ab034424278e78/evidence/m232-verification-20261019.json>)。下一阶段M2.3.3处理后台202/独立标题与Python实际任务指标；不把本阶段称为M2.3全部完成。


## M2.3.3：后台工作与Python实际任务指标（2026-10-20完成）

原清单、归组、来源核查和路线的8个202提交/继续入口，在创建/更新记录前先取得共享槽。提交连接结束后工作继续，Future真正退出才释放；显式pause使旧attempt失效并请求取消模型HTTP。嵌套自动核查复用父槽/Token，保留原数据库、人工审核、幂等和续跑逻辑。标题最多一个并尽力调度，失败30秒冷却；SSE标题在轮次槽归还后调度。

- 同批103项普通与ASan/UBSan通过（23 HTTP、22流式/上传、16 Redis、11准入、15指标、7取消、9治理代理）；两种构建不相加。100次治理指标轮询FD9→9。
- Python相关255项通过，覆盖已有JSON/SSE/取消及6个清单测试模块，含新增治理16项；工具4项通过，不是原项目全量discover。旧请求取消9组另行回归通过。
- 真实隔离链路与容器各通过同12组治理检查，含实际模型EOF、202持槽、四类暂停继续、标题去重、拒绝前不建记录、Gateway替换及Agent硬退出恢复；容器另过指标8项、新建6项及Gateway替换恢复5项。
- 版本校验并备份后同步独立Python项目20个代码/测试/说明文件；原84个公开运行/资产文件中9个运行模块修改、75个保持，新增agent_work.py后85个与最终验收上下文一致；重复预检0待修改。未启动原应用或访问原.env/业务数据库/个人资料/模型。
- Gateway/Agent容器UID10001，64个镜像内Python模块哈希一致；仅清理本次项目4个容器、2个命名合成卷、Redis匿名卷及网络，镜像保留。没有提交/推送。

部署仍按[deploy/README.md](deploy/README.md)重建两个应用镜像；原运行环境变量不变。Key保护的`/agent-metrics`依赖Agent，`/metrics`保持本地可用；两者进程级计数重启重置。单Uvicorn worker、不强杀阻塞操作、不保证供应商远端停算。真实模型/完整RAG部署与V3仍后续独立验收。细节见[治理说明](docs/JOB_GOVERNANCE.md)及[开发问题日志](docs/DEVELOPMENT_LOG.md)。
