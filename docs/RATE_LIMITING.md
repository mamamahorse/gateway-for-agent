# M2.1：Redis + Lua 共享限流

2026-10-20更新：M2.3.3已将8个后台202提交/继续入口和标题接入同一实际工作池，新增受Key保护的GET /agent-metrics。202返回不归还Python槽，显式pause请求取消，Future退出才释放；本文旧阶段的未实现描述保留作历史。完整解释与限制见[JOB_GOVERNANCE.md](JOB_GOVERNANCE.md)，最终结果见evidence/m233-verification-20261020.json。

本轮让网关在代理业务请求之前向 Redis 申请一次额度。两个网关不需要互相通信；只要 Redis、prefix、key_id 和类别相同，就使用同一个计数。Python 业务代码与数据结构没有修改。

## 请求怎样经过限流

1. `ProxySession::read_request()` 检查方法/路径、Key、头部大小、上传类型和 Expect。失败请求直接返回，不发送 EVAL。`/livez` 本地返回。
2. 对普通业务按原精确路由表分类：`normal` 是元数据等普通 API；`model` 使用原 `Route::model_request`，包括问答、轮次、重试、出题和评分等；`upload` 是文档上传。Markdown 和 PDF 上传共享 upload 类别。
3. `RedisCheck::start()` 在同一个 Asio 事件循环异步解析地址、连接 Redis、握手并执行 EVAL。等结果前不读剩余业务正文、不连接 Python，也不发送上传的 `100 Continue`。
4. Lua 返回允许才继续原 `receive_body()`；超限在 HTTP/SSE 头提交前返回 429，`Retry-After` 是剩余毫秒向上取整的秒数。Redis 失败、超时、协议不符或异常计数返回 503 `REDIS_UNAVAILABLE`，不把请求转给 Python。
5. 启用限流时 `/readyz` 先执行独立 PING，再探测 Python；不调用 Lua、不消耗业务额度。两个探针先后执行，各有自己的期限。`/livez` 始终不依赖 Redis。

计数是通过路由、Key 和请求头检查的到达次数，不是成功任务数。后续 JSON 解析、Python 业务、连接或模型失败都不退额度；客户端断连、超时重试也不能当作事务回滚。已经开始的 SSE 不会按每个事件再次计数。

## 关键代码，为什么这样写

| 文件 / 函数 | 改变和理由 |
| --- | --- |
| `scripts/rate_limit.lua` | GET、判断上限、INCR、PEXPIRE 在一次脚本执行中完成，避免多命令之间穿插。原计数没有 TTL 时也修复，包含已达到额度的计数；拒绝不会延长正常窗口。 |
| `src/redis_limiter.hpp/.cpp` 的 `RedisCheck` | 每次检查持有自己的 Redis connection、request、定时器、固定三整数响应和回调。使用短连接便于把超时、取消和生命周期限定在一次检查；当前学习规模不先建立连接池。代价是每个业务到达都多一次 TCP/HELLO 握手。 |
| `RedisCheck::run()/complete()` | 统一总期限覆盖 DNS、连接、握手、发送和读取；禁用重连重放。完成标记先置位，再取消网络/定时器，最后移动并调用回调，保证只完成一次。 |
| `src/gateway.cpp` 的 `after_rate_limit()` | 把原来读取正文/发送 100 的步骤放在额度通过之后。Session 持有可取消的检查，shutdown/finish 时一起释放；等待 Redis 不计作客户端读头超时。 |
| `src/main.cpp` / `RedisSettings` | 环境变量集中校验，启动错误只打印配置名和约束。使用非秘密 key_id 组成 Redis key，不把 API Key 放入 Redis 或日志。 |
| `CMakeLists.txt` / `src/rate_limit_script.hpp.in` | 构建时把唯一 Lua 源文件嵌入二进制，部署不需要额外查找脚本路径；修改 Lua 会触发重新配置和构建。 |

异步回调通过 shared_ptr 保留检查对象，直到 Redis 的 async_run/async_exec 和被取消的定时器回调都结束。Session 正在退出时，检查的迟到回调先看到结束标志，不会再发送第二个 HTTP 响应。每次只开自己的连接，所以检查超时不会取消别的请求。

`cancel_on_connection_lost=true`、`cancel_if_unresponded=true`，连接重连等待设为0。EVAL 可能已经执行，但响应丢失；这种情况返回503，可能消耗了一次额度，不能安全地自动重放。测试用故障代理确认真实 Redis 已加1、网关未收到结果时，没有发送第二次 EVAL。

响应使用 `std::array<long long,3>`，而非根据服务端数组长度 reserve 的 vector；Redis 输入缓冲最多64 KiB。检查对象总量受已有128个下游连接上限约束，没有每请求新建工作线程。Boost.Redis 的该版本整数适配器使用 long long，Linux 上 int64_t 是 long，直接使用后者编译不通过，因此在客户端适配处明确采用前者。

## 算法和状态边界

Key：`<prefix>:<key_id>:<normal|model|upload>`，例如 `gw:rl:default:model`。当前每个网关只支持一个 API Key；key_id 是该 Key 的公开身份配置，不是自动计算的秘密 Key 指纹。副本应保持一致，额度、窗口也应一致；不同公开 key_id 的额度独立。若轮换秘密 Key 而保留 key_id，继续使用当前计数。

窗口从该类别首次放行时开始，默认60秒，过期后自动清零。边界两侧可能短时放行接近两倍额度，这是固定窗口的特点，不是平滑限流。Redis 使用自己的过期计时，不依赖各网关系统时钟同步。

原计数无过期时补设一个窗口；非法数字、负数、非整数、错误 Redis 类型或脚本错误会失败关闭，不回显 Redis 错误正文。本轮 Redis 不持久化，重启后计数重置；它不是持久配额/Token 账本。

Lua 的原子性表示执行过程不与其他命令穿插，不表示脚本出现错误会回滚先前的 Redis 写入。当前脚本短小固定；后续修改仍需考虑这一边界。

## 配置

| 环境变量 | 默认值 | 约束 / 用途 |
| --- | --- | --- |
| `GATEWAY_RATE_LIMIT_ENABLED` | `0` | 仅0/1；原生开发默认关闭，Compose 默认开启 |
| `GATEWAY_REDIS_HOST` | `127.0.0.1` | 主机名或 IPv4，1–253字符 |
| `GATEWAY_REDIS_PORT` | `6379` | 1–65535 |
| `GATEWAY_REDIS_TIMEOUT_MS` | `100` | 每次检查总期限，1–60000ms |
| `GATEWAY_RATE_LIMIT_PREFIX` | `gw:rl` | 1–64字符，字母/数字/冒号/下划线/短横线 |
| `GATEWAY_KEY_ID` | `default` | 1–64字符，字母/数字/下划线/短横线 |
| `GATEWAY_RATE_LIMIT_WINDOW_MS` | `60000` | 1–3600000ms |
| `GATEWAY_RATE_LIMIT_NORMAL` | `120` | 每窗口普通请求额度，1–1000000 |
| `GATEWAY_RATE_LIMIT_MODEL` | `10` | 每窗口耗时模型请求额度，1–1000000 |
| `GATEWAY_RATE_LIMIT_UPLOAD` | `5` | 每窗口上传请求额度，1–1000000 |

即使关闭限流也校验配置格式。当前 Redis 使用私有开发网络的无密码连接；没有加入 Redis AUTH、TLS、Sentinel、Cluster、连接池或EVALSHA缓存管理。不要把这份学习配置当作公网 Redis 部署方案。

## Ubuntu 构建和运行

Boost.Redis 需要 Boost≥1.84；本项目固定到原计划候选1.88.0。使用官方发布包并校验 SHA-256，放在 Ubuntu build 目录，不替换系统 Boost1.83。Boost.System 以头文件方式使用，新增 OpenSSL 开发依赖；OpenSSL 是 Redis 客户端构建依赖，不代表 HTTP 上游已支持 HTTPS。

在网关根目录、Ubuntu 环境安装 `build-essential cmake ninja-build curl bzip2 libssl-dev python3 redis-server` 后：

```sh
sh tools/prepare_boost.sh
cmake -S . -B "$HOME/build/ai-gateway-m21" -G Ninja -DCMAKE_BUILD_TYPE=Debug \
  -DBOOST_ROOT="$HOME/build/ai-gateway-deps/boost_1_88_0"
cmake --build "$HOME/build/ai-gateway-m21" -j2
ctest --test-dir "$HOME/build/ai-gateway-m21" --output-on-failure
```

独立的公开数据演示，分别运行：

```sh
redis-server --bind 127.0.0.1 --port 16379 --save "" --appendonly no
```

```sh
python3 tests/fake_agent.py --port 8000
```

```sh
export GATEWAY_API_KEY=dev-only-change-me
export GATEWAY_RATE_LIMIT_ENABLED=1
export GATEWAY_REDIS_PORT=16379
export GATEWAY_RATE_LIMIT_PREFIX=dev:gw:rl
export GATEWAY_RATE_LIMIT_NORMAL=2
export GATEWAY_RATE_LIMIT_WINDOW_MS=10000
"$HOME/build/ai-gateway-m21/ai-gateway"
```

```sh
curl -i -H 'X-API-Key: dev-only-change-me' http://127.0.0.1:8080/api/documents
```

10秒内重复上述业务请求三次，预期200/200/429，429带 Retry-After；健康请求仍正常。以上 Key 是公开演示值，合成后端不调用模型。

ASan/UBSan：

```sh
cmake -S . -B "$HOME/build/ai-gateway-m21-asan" -G Ninja -DCMAKE_BUILD_TYPE=Debug \
  -DBOOST_ROOT="$HOME/build/ai-gateway-deps/boost_1_88_0" -DGATEWAY_SANITIZERS=ON
cmake --build "$HOME/build/ai-gateway-m21-asan" -j1
ctest --test-dir "$HOME/build/ai-gateway-m21-asan" --output-on-failure
```

## 验收范围

普通构建和 ASan/UBSan 均通过同批61项黑盒检查：原有23项HTTP + 22项SSE/上传 + 新增16项Redis限流，CTest登记为3个入口，不把两套构建相加为122项独立测试。

新测试启动独立 Redis7.0.15、随机端口和临时目录，不读写开发者已有 Redis。两个真实网关对同一类别并发40次，额度7时总放行7、拒绝33，上游仅收到7次。还覆盖独立类别、key_id隔离、TTL到期/不刷新/缺失修复、异常状态、上传Expect、健康探针、服务停止/暂停/重启、错误响应、丢失已提交结果、100次检查的FD/线程恢复及停机取消。

另用真实 Knowledge Review 的隔离副本验证 Gateway→Redis→Agent→可控本地模型服务10项检查，执行真实read_source和最终校验/保存。原62个运行源码文件哈希前后相同，没有外部模型调用或真实模型质量评价。

Dockerfile已升级Boost/OpenSSL，并在2026-10-18容器复检中补齐build阶段bzip2解压依赖。此前Docker不可用导致的验收阻塞现已解除：最新Release镜像构建、双实例40请求放行7/拒绝33且Redis计数7、未认证请求及健康探针不扣额度、Redis停机503与重启恢复均通过。实际Agent SSE首次6项及替换容器恢复5项另见`evidence/m21-compose-create-20261018.json`和`evidence/m21-compose-restore-20261018.json`。汇总为`evidence/m21-docker-recovery-20261018.json`；没有在本轮重跑原生61项或将旧原生成绩计入容器结果。

M2.2已新增跨JSON/SSE入口的本地Permit/FIFO及Python实际工作槽，详见`docs/ADMISSION_CONTROL.md`；M2.3.1提供不依赖Redis的本地指标，见`docs/METRICS.md`。Redis自身仍只限制到达速度，不取消已开始的Python/模型工作；完整业务取消、全部后台作业治理及Python实际工作指标尚未实现。

参考固定版本的 [Boost.Redis 文档](https://www.boost.org/doc/libs/1_88_0/libs/redis/doc/html/index.html)、[request 取消配置源码](https://github.com/boostorg/redis/blob/boost-1.88.0/include/boost/redis/request.hpp) 和 [官方发布包校验信息](https://archives.boost.io/release/1.88.0/source/boost_1_88_0.tar.bz2.json)。

2026-10-19：M2.3.2已实现已接入JSON/SSE的请求范围模型HTTP取消，见docs/CANCELLATION.md；Redis仍只管额度。全应用后台治理、Python实际任务指标和真实RAG部署未完成，不能将Redis计数/代理Permit当作实际工作完成证明。
