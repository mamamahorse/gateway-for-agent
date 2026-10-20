# M2.3.2：已接入 JSON/SSE 的模型 HTTP 取消

2026-10-20更新：M2.3.3已将8个后台202提交/继续入口和标题接入同一实际工作池，新增受Key保护的GET /agent-metrics。202返回不归还Python槽，显式pause请求取消，Future退出才释放；本文旧阶段的未实现描述保留作历史。完整解释与限制见[JOB_GOVERNANCE.md](JOB_GOVERNANCE.md)，最终结果见evidence/m233-verification-20261020.json。

本阶段让客户端退出和网关超时传到Python实际模型HTTP请求。保留原同步Agent/练习业务函数、请求与成功响应、SQLite表结构；不新增取消API、业务状态枚举或后台任务系统。M2.3仍有后台202/独立标题治理和Python实际任务指标待做。

## 为什么关闭代理连接还不够

M2.2中Gateway超时会释放代理Permit，但Python工作线程仍等待同步OpenAI SDK，甚至稍后保存completed。线程池Future.cancel不能停止运行中的线程；跨线程关闭共享HTTP客户端也不能可靠证明当前读操作已经结束。

现在的链路是：

```text
客户端FIN/RST，或Gateway超时/停止会话
  → Gateway finish：关闭上游连接、统一清理代理资源
  → Python Request断连 / SSE生成器取消：设置该工作的Cancellation
  → 工作线程内的模型HTTP传输取消异步任务、等待连接关闭
  → GenerationCancelled离开SDK；会话保存failed及安全取消说明
  → 工作函数和清理真正结束；concurrent Future回调释放实际工作槽
```

代理Permit释放和Python工作退出仍是两个时刻；新请求可能短暂遇到Python503。没有把本地指标归零当作实际取消证明，验收还核对了模型测试服务观察到EOF、持久化状态、没有后续模型调用以及新工作成功。

## C++ 改了什么

`src/gateway.cpp`的`ProxySession::begin_upstream()`为模型请求增加`watch_model_client()`。正文已经完整接收，监听用socket异步可读等待与非阻塞peek，不消费数据、不新建线程，可以与下游响应写并行；静默等待上游时也能退出，不必等下一条SSE/心跳或总期限。

监听使用独立`client_cancellation_`，不复用队列取消槽；`cancel_upstream()`统一撤销监听，回调持有Session直到完成，`finish()`原有幂等保护继续防止重复计数/释放。上传、非模型读取保留原传输行为。

**协议变化：所有模型请求在完整正文之后将FIN视为撤回，包含shutdown(SHUT_WR)。** TCP FIN无法区分“只关写端但继续读”和“用户已退出”，因此模型客户端必须保持写方向开放直到响应结束。M2.2该约定只用于排队，现扩展到执行中的JSON/SSE；普通非模型GET仍支持写半关闭后读取响应。每连接一请求，额外流水线数据也会关闭连接。

## Python 改了什么，为什么

| 文件/对象 | 修改及理由 |
| --- | --- |
| `agent_cancel.py` `Cancellation` / `cancellation_scope()` | threading.Event保存取消请求，ContextVar仅绑定当前工作；一项取消不影响另一项，不需要全局任务注册或远程取消端点。 |
| `CancellableTransport.handle_request()` | 保留同步OpenAI API；在当前工作线程内用asyncio.run拥有异步httpx客户端，20ms检查取消。取消task后await清理，连接关闭后才返回工作线程。没有额外常驻网络线程或跨线程close。 |
| `model_http_options()` | 仅有绑定Token时注入传输；CLI和范围外后台调用保持原传输。范围内关闭SDK自动重试，避免不可取消的同步退避；取消本身也不能被当作模型故障重试。原结构化输出的应用层协议回退仍保留。 |
| `model_client.py`、`agent_loop.py`、`practice_model.py`、`conversation_context.py`、`conversation_title.py`、`pdf_vision.py` | 少量创建点接入上述选项，覆盖回答、Agent决策、练习、请求内摘要/标题/视觉模型；无绑定Token时行为保持。没有改写各业务算法。 |
| `agent_loop.run_agent()` | 模型迭代、工具之间和最终阶段检查取消，不继续发起下一轮调用或工具。同步工具自身不是强制可中断的。 |
| `agent_stream.StreamRunner` | 每项JSON/SSE工作绑定独立Token。JSON装饰器通过FastAPI内部签名注入Request，正文解析后检查断连；SSE响应头前也检查，响应开始后用生成器finally请求取消。Future真实完成才释放槽位。 |
| `EventBridge` / `EventResponse` | 事件未正常交付就退出时取消；正常done后的结束不误取消。响应层finally覆盖生成器尚未启动就发送响应头失败的情形。被取消的工作不再新启动SSE标题；标题已启动但结果未正常交付时同样取消。预启动取消也必须唤醒等待meta的HTTP协程。 |
| `web_app.execute_turn()` | 捕获GenerationCancelled，复用ConversationStore.fail保存“生成已取消，原始输入已保存，可以重试。”；最终finish前再检查，保留锁、幂等、恢复和原请求。 |

`GenerationCancelled`特意继承BaseException，越过SDK和原业务中的宽泛`except Exception`；只有StreamRunner/会话等明确边界处理它，不拦截KeyboardInterrupt/SystemExit。无会话的JSON入口将取消转成安全499；已断开的客户端通常收不到这个响应。会话仍使用既有failed，未增加cancelled数据库状态。工作范围中禁用SDK重试是有意的行为变化，CLI/范围外调用不受影响。

内层读取完整模型JSON后交给SDK，与原非流式模型API一致，不宣称逐token生成；Gateway的SSE仍是执行进度加最终结果，按固定缓冲转发。已验收普通JSON、gzip响应、结构化parse和response_format不支持时的submit_result回退。

## 明确的取消边界

- 取消可中止本服务等待的模型HTTP连接；供应商收到断开后是否停止远端算力，未验证、无法保证。
- CPU工具、文件/SQLite操作及底层DNS等不可取消阻塞不会被强杀，需操作退出/到达检查点。Python线程不提供抢占式结束；本阶段不是所有同步操作的硬截止时间。
- 取消与提交有竞争。提交已完成则保留completed；已保存的答案、评分、摘要等不回滚。客户端无done时读历史确认，不能直接认定事务取消或重做POST。
- JSON BackgroundTasks标题、读取触发标题和其他后台202没有绑定请求Token，仍待M2.3.3治理；不是全应用任务取消/指标系统。SSE已有标题后处理在绑定范围内，但结果正常发完后不等于后处理已结束。
- StreamRunner lifespan请求取消剩余绑定工作；Uvicorn的HTTP排空先后顺序和停机宽限仍影响实际停机，未声称已验证所有生产停机路径。
- Gateway指标只观察传输结果；客户端退出后Python保存的failed未必再送回网关，因此不能将业务失败指标作为全量取消计数。

## 运行与验证

Ubuntu源码目录中构建，Boost依赖沿用既有1.88：

```sh
cmake -S . -B "$HOME/build/ai-gateway-m232" -G Ninja \
  -DCMAKE_BUILD_TYPE=Debug -DBOOST_ROOT="$HOME/build/ai-gateway-deps/boost_1_88_0"
cmake --build "$HOME/build/ai-gateway-m232" -j2
ctest --test-dir "$HOME/build/ai-gateway-m232" --output-on-failure
```

独立Python项目先用带SHA校验的工具检查/应用适配，再重新生成公开构建上下文；不要用旧上下文重建Agent：

```sh
python3 tools/apply_knowledge_review.py --project '/mnt/e/agentic project/knowledge-review'
python3 tools/apply_knowledge_review.py --project '/mnt/e/agentic project/knowledge-review' --apply
python3 tools/prepare_agent_context.py --project '/mnt/e/agentic project/knowledge-review' \
  --output build/agent-context-m232-new
```

按deploy/README设置KNOWLEDGE_REVIEW_CONTEXT到新目录后重建/启动Gateway和Agent；无需新增生产配置或依赖服务。httpx已是现有OpenAI依赖，没有新增安装包。Python项目的基础依赖环境可运行：

```sh
python -m unittest -v test_agent_sse test_agent_admission test_agent_cancellation
```

2026-10-19验收：同批94项普通与ASan/UBSan（23 HTTP、22流式/上传、16 Redis、11准入、15指标、7取消）；100次静默断连FD9→9。Python115项相关回归（原80+SSE12+共享槽7+取消16），另有工具4项。真实隔离Agent与容器分别通过同9组取消检查，核对模型EOF/失败保存/最终生成取消/JSON总期限/SSE空闲期限/12次重复取消/新工作和幂等；同8组业务指标也通过。容器另完成新建6项、Gateway替换恢复5项、UID10001和63个Python运行文件与最终上下文哈希一致。

这不是原Python项目全量回归或真实模型/RAG质量验收。仅使用临时副本/合成卷与本地模型服务。证据、源码同步与精确清理见`evidence/m232-*`；定位经过见`docs/DEVELOPMENT_LOG.md`。
