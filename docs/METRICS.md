# M2.3.1：本地请求指标与业务失败观察

2026-10-20更新：M2.3.3已将8个后台202提交/继续入口和标题接入同一实际工作池，新增受Key保护的GET /agent-metrics。202返回不归还Python槽，显式pause请求取消，Future退出才释放；本文旧阶段的未实现描述保留作历史。完整解释与限制见[JOB_GOVERNANCE.md](JOB_GOVERNANCE.md)，最终结果见evidence/m233-verification-20261020.json。

本阶段增加受保护的`GET /metrics` JSON快照，帮助观察限流、排队、上游故障和资源回收。使用现有API Key，不访问Redis/Python，不增加监控平台或新的服务。本节描述M2.3.1交付。后续M2.3.2已实现请求范围模型HTTP取消（见docs/CANCELLATION.md），后台任务治理与Python实际任务指标仍待做，M2.3整体尚未完成。

## 如何读取

```sh
curl -fsS -H 'X-API-Key: dev-only-change-me' http://127.0.0.1:8080/metrics | python3 -m json.tool
```

示例Key必须与本地网关的`GATEWAY_API_KEY`一致。没有Key、错误Key或重复Key返回401；成功和鉴权错误响应均为`Cache-Control: no-store`。只有GET开放，未增加写入、清零或持久化接口。

快照`schema_version=1`、`scope=gateway_process`。每个进程各自统计，重建/重启从零开始；多个Gateway共享Redis额度，并不共享指标。`/metrics`绕过Redis限流和模型准入，但仍占用TCP连接，受原有总连接上限约束。

## 字段怎样理解

| 字段 | 含义 |
| --- | --- |
| `requests.finished` | 已finish的非探针/非指标请求尝试，包括401、404、解析失败、429、503和中断；不是完成的Agent任务数量。 |
| `requests.http_status` | 已选择的HTTP响应状态码计数。0表示没有开始响应；写入失败时仍可能记录原先选择的200，不保证客户端收到完整响应。只输出非零项。 |
| `requests.model_http_in_flight` | 已通过Key验证且匹配模型路由、尚未finish的HTTP请求，包括读正文、Redis、排队、转发和写回；不是Python实际运行任务数。 |
| `admission.held_permits` | 本Gateway已经预留/持有的模型代理名额；`queued`和`queued_body_bytes`为等待条数与正文预算占用。`enabled`说明准入是否开启。 |
| `admission.*_limit` / `queue_timeout_ms` | 当前本进程准入配置，便于核对部署是否生效。 |
| `end_reasons` | 固定原因集合，如`rate_limited`、`queue_full`、`queue_timeout`、`redis_unavailable`、`timeout`、`upstream_error`和流错误；未知程序原因归other。 |
| `requests.upstream_http_errors` | 完整转发的上游4xx及以上状态；连接失败/超时由对应结束原因计数，二者含义不同。 |
| `requests.observed_business_failures` | 在上游成功HTTP响应中识别出的业务失败，按代理请求去重；不是独立任务失败总量或全应用错误率。 |
| `requests.business_inspection_unavailable` | 本次请求因JSON观察预算、编码/格式或无效SSE done等原因，无法完成相应状态观察。 |
| `requests.streams_without_done` | 已开始代理SSE，但finish前网关没有观察到有效done.completed/failed；可能由上游中断、客户端退出或协议不匹配造成。 |
| `requests.duration_ms_total` | 非探针/非指标请求从Session创建到finish的累计毫秒数，包含排队和错误路径；不提供百分位延迟。 |
| `request_body_bytes_total` / `response_body_bytes_total` | requests中的正文计数，沿用访问日志口径，见下文；不是精确的线上流量测量。 |
| `connections.accepted_total` / `limit_rejected_total` | TCP accept成功总数，以及随后因总连接上限被拒绝的数量。 |
| `connections.active` | 当前TCP会话数，包含正在读取快照的连接和探针，因此空闲时访问metrics也可能看到1。 |

GET `/livez`、`/readyz`、`/metrics`不累计到`requests`或`end_reasons`，避免探针/指标轮询制造业务流量；它们仍影响TCP连接计数。请求头尚未解析出路径就中断的连接无法识别为探针，会计入非探针失败尝试。总连接上限拒绝尚未创建Session，只计入连接拒绝数。

正文口径直接沿用原日志：JSON请求在进入上游阶段按已知正文长度记账，尚在排队就拒绝的正文不计入；上传按完成的写块累加。非流式响应按准备发送的正文长度计，SSE按完成的写块累加。它们不含HTTP framing，也不能证明客户端最终接收了多少字节。

## 为什么不能把200当业务成功

Knowledge Review的JSON轮次失败会保存`status=failed`并返回HTTP200；SSE也可能在200后发出error/done.failed。网关分别记录HTTP状态与可观察业务失败，不用200减失败计数来推导业务成功率。

- JSON仅检查模型路由、2xx、未压缩application/json，使用Boost.JSON解析顶层status。最多64KiB、深度16；大结果、编码或格式不可检查时只增加unavailable，响应保持原样。嵌套status、回答文本中的failed不会误计；缺少业务status不代表成功。
- SSE只增量观察原事件契约：有data的error事件，以及有效done.failed。两者出现在同一请求只累计一次。progress中的failed不算终态失败；支持LF、CRLF、CR、起始BOM和多行data。
- 观察器只保存最多8KiB单行和8KiB事件数据内容，不积累完整结果。超大result行跳过后，后续error/done仍能识别；原始正文逐块转发，不重编码、不等最终事件。
- 观察发生在读取上游块时，计数在请求finish时累加；不证明失败事件已成功送达客户端。读取历史失败记录不会再次计数，但同一任务的再次代理/重放可能再次产生一次观察值。

没有单独的业务成功计数。`streams_without_done=0`只代表网关观察到了终态，不证明客户端收到完整数据；`model_http_in_flight=0`或Permit归零也不证明Python同步工作已停止。实际工作与HTTP请求的差异已经在M2.2真实链路验证，不能用这些指标宣称完整取消已实现。

## 代码改了什么，为什么

| 文件 | 职责 |
| --- | --- |
| `src/metrics.hpp/.cpp` `Metrics` | 固定状态码/结束原因维度与累计计数。只在现有单io_context线程访问，快照一致，不增加锁、后台线程或动态URL标签。 |
| 同文件 `StreamObserver` | 有界的SSE行/事件观察器，只识别所需终态，不保留问题/答案或完整流。 |
| `inspect_business_json()` | 对有限JSON作结构解析，避免字符串搜索误报；超过预算不影响代理业务。 |
| `src/gateway.cpp` | 本地鉴权路由；接入模型HTTP/连接开始、finish统一计数；读上游块时观察，仍沿用原背压和错误清理。 |
| `CMakeLists.txt` | 单TU编译已有Boost1.88附带的Boost.JSON实现，新增指标测试入口；没有新增安装包或运行服务。 |

## 编译、运行与验收

在Gateway源码目录：

```sh
cmake -S . -B "$HOME/build/ai-gateway-m231" -G Ninja \
  -DCMAKE_BUILD_TYPE=Debug -DBOOST_ROOT="$HOME/build/ai-gateway-deps/boost_1_88_0"
cmake --build "$HOME/build/ai-gateway-m231" -j2
ctest --test-dir "$HOME/build/ai-gateway-m231" --output-on-failure
```

按README启动合成后端/网关或按deploy/README部署Compose后，即可使用同一网关端口读取指标；没有额外端口。原生Redis及准入仍分别由原开关控制，指标本身无需开关。

2026-10-19普通与ASan/UBSan同批87项通过：23 HTTP、22 SSE/上传、16 Redis、11准入、15指标。不相加为174项独立测试。新增测试覆盖错误Key、固定维度/脱敏、JSON误报防护、不同SSE分块/换行、多行/2MiB结果/断流、排队指标、Redis故障、100次快照FD9→9；原传输/资源/限流行为回归通过。

原生隔离Agent与容器分别运行同一8项检查，验证真实JSON/SSE失败总计增加2、SQLite状态一致，成功read_source先返回进度再返回结果。容器另验证两个进程指标独立、网关重建计数归零但资料会话保留、Redis故障快照仍可读。仅使用公开合成数据及本地可控模型，未评价模型质量。

结果和清理记录见`evidence/m231-*`；本轮没有修改Python应用源码、原数据、配置或M2.2适配包，没有重跑旧Python99项并将其计入本轮。

2026-10-19 M2.3.2更新：指标口径不变，实际取消另通过模型EOF/持久化/新任务核验。客户端退出后Python保存的failed可能没有被网关读到，observed_business_failures不能充当全量取消数；Permit/在途归零仍不代表不可取消阻塞已经结束。
