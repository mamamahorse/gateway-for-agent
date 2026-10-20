# M2.3.3：后台工作、标题与实际任务指标

本阶段把已有清单生成、归组、来源核查、路线生成的8个202入口，以及自动标题，接入原来的Python共享工作池。保留FastAPI、同步业务函数、SQLite结构和原有提交/暂停/继续契约。Gateway负责HTTP代理与准入，Knowledge Review拥有实际工作。

## 为什么202需要另一种生命周期

原BackgroundTasks跟随HTTP响应执行；网关只能看到很快返回的202，无法用代理连接数推断后续模型任务数。若在202时释放实际槽位，连续提交仍可能让后台工作无限增长。

现在先申请Python槽位，再调用原函数创建或更新记录。没有容量立即503和Retry-After: 1，不新建任务记录。DetachedTask收集原函数添加的单个后台调用，随后提交到同一受限执行器；202无需等待模型完成。槽位从预留、开始执行，一直持有到真实Future结束，包含模型连接清理。没有新增应用等待队列、消息中间件或数据库表。

```text
模型JSON/SSE → Gateway限流/Permit → Python共享槽 → 原同步业务
后台提交     → Gateway限流/Permit → Python先预留槽/原记录 → 202
                                                └→ 工作退出归还槽
历史读取/轮次完成 → 标题调度 → 同一Python池（忙则跳过，最多一个标题）
显式暂停 → 原状态/attempt失效 → 取消Token → 模型HTTP关闭 → 真正退出
GET /agent-metrics → Key鉴权 → 固定/internal/metrics → Python工作快照
```

Gateway的Permit到HTTP请求结束归还；后台任务不依赖它持有执行名额。已接受工作的提交连接断开、Gateway重启，不撤销后台工作；停止它使用原pause接口。请求JSON/SSE仍沿用M2.3.2的断连取消。两者都在真实工作退出后才归还Python槽。

## 代码为什么这样改

| 文件/函数 | 改动及原因 |
| --- | --- |
| `integrations/knowledge-review/agent_stream.py:StreamRunner` | 在原共享池增加有界工作所有权和固定类别统计；提交前预留、完成回调释放，取消信号和202都不能代替完成确认。 |
| 同文件`detached()` | 保留FastAPI原签名，收集原提交函数的单个后台调用；预留失败不产生任务，执行/清理中请求继续先409。 |
| 同文件`title()` | 最多一个标题，占用同一容量；使用已有title_started_at冷却30秒，忙时不排队，不保留无限重试闭包。 |
| `agent_work.py:job_boundary()` | 在4个原后台执行函数外增加协作式取消边界，复用原状态恢复；嵌套自动来源核查复用父工作槽与Token。 |
| `agent_cancel.py:Cancellation.check()` | 区分请求取消与观察到取消，避免把没有合作退出、仍成功完成的工作记成已取消。 |
| `runtime/web_app.py` | 8个提交/继续入口添加装饰器；4个暂停先持久化再取消；标题改为轻量调度；增加内部快照。 |
| `runtime/outline_store.py:pause()` | 同一事务使执行中的批次/背景步骤attempt失效，阻止暂停后旧结果迟到发布；已完成结果保留。 |
| `runtime/conversation_title.py:auto_title()` | 取消也清理本次标题claim；发布前检查取消，返回完成/失败/跳过结果。 |
| `src/policy.cpp:route_for()` | 显式加入16个真实业务方法/路径；8个后台提交/继续扣模型类Redis额度，不开放任意Python路径。 |
| `src/gateway.cpp:ProxySession` | 增加受Key保护的固定目标`/agent-metrics`代理，分别观察Gateway请求数与Python工作数。 |

WorkLease只保留当前工作状态。所有权键在活动期间存在，完成删除，不维护无限历史映射。归组主记录可能先completed，之后同一工作仍执行自动来源核查；不能用主记录completed当作整个工作完成。嵌套核查的暂停键指向同一Token，能取消当前模型调用；不再抢一个槽造成单worker死锁。

## 真实后台接口契约

标识符使用后端实际返回值。请求模型仍以独立项目`web_app.py`的Pydantic类为准，Gateway原样转发业务字段。

| 方法与路径 | 原请求模型/行为 |
| --- | --- |
| `POST /api/collections/{id}/outlines` | OutlineRequest，含request_id/objective/response_language；202 `{"id":...}`。 |
| `POST /api/outlines/{id}/resume` | 无新字段；202返回原id。 |
| `POST /api/outlines/{id}/grouping` | OutlineGroupRequest，保留候选范围、语言、request_id及call_limit；202返回归组run id。 |
| `POST /api/outline-group-runs/{id}/retry` | OutlineGroupContinueRequest；202返回run id。 |
| `POST /api/outline-groups/{id}/source-reviews` | OutlineSourceReviewRequest；202返回核查id。 |
| `POST /api/outline-source-reviews/{id}/continue` | OutlineGroupContinueRequest；202返回核查id。 |
| `POST /api/outlines/{id}/routes` | OutlineRouteRequest，保留人工审核要求；202返回路线run id。 |
| `POST /api/outline-routes/{id}/continue` | OutlineGroupContinueRequest；202返回路线id。 |

其余8个新增方法/路径：`POST /api/collections`，`GET /api/collections/{id}/outlines`，`GET /api/outlines/{id}`，四类`POST .../{id}/pause`，`PATCH /api/outline-groups/{id}`。清单读取保留嵌套归组/核查/路线结构；PATCH保留revision和人工decision校验。并未开放整个清单编辑/学习计划API。

容量满503，额度耗尽429，执行/清理中请求继续409。范围、资料版本、幂等request_id与审核仍由原后端校验。同request_id不会重复执行；满容量时重放也可能先503，待空闲再读取/重试，没有为幂等命中引入另一套跨层数据库查询。

## 标题为什么尽力调度

标题是可选后处理，不应让问答排在大量标题后。workers大于1时，标题最多一个且至少留一个未占槽；单worker时仅空闲才开始，已经运行的标题不会被新问答抢占。跳过不影响已保存答案，后续读取可以再触发。失败30秒冷却避免读取风暴，没有自动定时重试服务。人工标题和已生成标题保持原值。

SSE成功后，通过完成回调在释放原轮次槽后调度标题，避免workers=1时嵌套抢槽。JSON原BackgroundTasks现在只调用很短的调度函数，不在那里执行模型。

## 指标含义与边界

`GET /metrics`仍是Gateway本地观察；`GET /agent-metrics`固定请求Python的`/internal/metrics`。两个端点都需要X-API-Key、返回no-store、绕过Redis额度与Gateway模型排队，轮询不增加业务请求统计；连接数仍按真实连接反映。内部路径没有公共白名单。Python本身没有新增登录鉴权，Agent应保持仅Compose内部可达。

Agent故障时`/agent-metrics`返回502，达到就绪探针期限504；`/metrics`仍可读取。快照不是独立监控存储或多进程总量。

| 字段 | 含义 |
| --- | --- |
| scope/uptime_ms/workers | python_process，单个应用进程的StreamRunner；进程重启重置，使用一个Uvicorn worker。 |
| held_slots | 所有类别reserved + running，不因HTTP等待取消提前减少。 |
| queued=0 / queue_limit=0 | 没有应用等待队列；reserved含准备与线程开始前的短暂调度，不代表已经调用模型。 |
| owned_keys | 当前所有权及嵌套别名数量，可能多于任务数；不输出键内容，完成删除。 |
| types.request/background/title | 三种固定类别；无问题、会话/任务id、路径、Key或异常文本标签。 |
| reserved/running | 当前预留/实际进入执行函数的工作数。 |
| submitted/started/exited | 累计成功提交/开始/真正退出；仅预留后释放不伪造submitted/exited。 |
| completed/failed/cancelled/paused/stale/other | 退出观察结果；cancelled要求观察到检查点或取消异常，不等于发送取消。 |
| cancel_requested | 退出时发现曾请求过取消的工作数，不是实时待取消数；可与completed并存。 |
| rejected/deduplicated/skipped | 拒绝/命中当前所有权/可选工作跳过的次数，不是独立业务任务总数，不能相加计算成功率。 |

background结果复用原持久化终态，嵌套流程不另累计为一项submitted。CLI、未开放模型接口及其他进程不在统计内，不能称所有Python模型调用的全局治理。

## 运行、验收和恢复

Ubuntu沿用Boost1.88和既有环境：

```sh
cmake -S . -B "$HOME/build/ai-gateway-m233" -G Ninja -DCMAKE_BUILD_TYPE=Debug \
  -DBOOST_ROOT="$HOME/build/ai-gateway-deps/boost_1_88_0"
cmake --build "$HOME/build/ai-gateway-m233" -j2
ctest --test-dir "$HOME/build/ai-gateway-m233" --output-on-failure
curl -fsS -H "X-API-Key: $GATEWAY_API_KEY" http://127.0.0.1:8080/agent-metrics
```

先按`integrations/knowledge-review/README.md`应用带SHA校验适配，再按`deploy/README.md`导出新Agent上下文并重建两个应用镜像。只更新Gateway不能治理旧Python后台工作。生产环境变量不变；测试用compose.jobs-test.yaml额外提供可控模型端口，仅用于合成数据。

验收覆盖：真实SDK模型连接EOF；四类后台提交/暂停/继续；满容量无新记录；标题去重和容量保留；单worker嵌套核查；Gateway重启不影响已接受工作；Agent硬退出后原数据库恢复与显式继续。Agent重启不自动恢复模型调用：原恢复逻辑将中断running工作转为可继续状态，queued仍沿用原恢复宽限规则，不能承诺任意状态立即恢复。

普通与ASan/UBSan同批103项已通过，两种构建不相加。Python相关255项覆盖原请求与6个清单模块，含新增治理16项，不是原项目全量discover。最终工具、真实链路、容器及源码同步状态见`evidence/m233-verification-20261020.json`。中间失败日志保留作定位证据，不能算通过。

已保存结果不回滚。取消不强杀CPU/文件/SQLite/DNS等阻塞，也不能证明供应商停止远端算力。真实模型质量、embedding/RAG部署、分布式并发租约、V3负载均衡/熔断/Token配额仍未实现或未验收。
