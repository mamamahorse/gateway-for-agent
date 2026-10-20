# 开发问题、定位与解决记录

本文件随 AI Gateway 开发持续维护，记录“现象→定位证据→原因及确定程度→修改及理由→验证→限制”。新增复测记录更新状态，不覆盖历史失败。不记录密钥、私人原文或数据库。

## 2026-10-19—20 M2.3.3：202返回后仍有实际模型工作

**调查与问题：** M2.3.2的7个JSON入口和SSE已经共享工作槽，但清单/归组/来源核查/路线的8个202入口仍在原BackgroundTasks执行，部分自动标题也独立运行。代理Permit随202返回归还，不能说明这些工作停止或受总量限制。归组主记录completed后，还可能在同一调用中执行自动来源核查，用主记录状态释放槽也会误计。

**实现与理由：** 扩展原StreamRunner，不另建任务系统。先申请共享槽，再调用原提交函数创建/更新记录；收集其一个后台调用，交给同一受限池后立即202。只有真实Future结束才释放；提交请求断开或Gateway重启不取消已接受工作。原4类pause先使状态/attempt失效，再取消对应Token；4个原执行函数加job_boundary，嵌套核查复用父工作所有权、槽和Token。原OutlineStore.pause同时使运行批次/背景步骤attempt失效，阻止暂停后结果迟到提交；已完成结果不回滚。

**标题与观察：** 标题最多1个，workers>1时留一个未占槽；单worker仅空闲调度且不抢占。忙时跳过、失败使用既有时间字段冷却30秒，不保留无限重试闭包。SSE标题在轮次槽释放后调度。内部快照按request/background/title固定类别统计，Gateway新增受Key保护的固定目标/agent-metrics；原/metrics仍是本地代理观察。取消请求与实际观察取消分开，未合作退出的工作不能因发送过取消就记成cancelled。

## 2026-10-20 M2.3.3：测试环境与旧同步假设

**首轮现象：** 扩展到清单模块后，255项中的早期版本出现大量失败。先对M2.3.2公开基线复跑两个代表用例，确认同样缺少LLM_API_KEY；新业务诊断读取model_client.model_settings，而旧夹具仅mock outline_service.model_settings。隔离副本也未包含只供测试使用的quality_route.py。不是把所有失败归因于新产品代码。

**定位与处理：** 测试进程显式提供公开dummy模型设置，指向127.0.0.1:9并关闭模型下载；quality_route仅复制到测试夹具，不进入镜像。不读原.env、不调用外部模型。M2.3.2代表用例在相同配置下通过，再继续定位剩余失败。四个原测试资产更新：202测试在mock作用域内等待实际工作归零；TestClient退出后重新创建app，避免复用已关闭的池；暂停测试改为验证旧attempt不能发布、步骤failed，并显式resume验证恢复，而不是继续期待暂停后迟到发布。

**兼容问题：** SSE旧测试替换_submit(action, token)，新增after_released关键字曾使其失败。只有实际需要时才传该选项，保留原调用形状，没有删减旧取消/事件预算断言。标题无历史/未取得claim返回None的工作归为skipped，不能伪计为completed。最终结果与失败历史分别保留在m233-python-verified、baseline-diagnosis、baseline-configured等日志。

## 2026-10-20 M2.3.3：审批服务额度与Docker未启动

**审批故障：** 首次ASan/UBSan执行请求未完成自动审批，服务提示额度不足并明确操作未执行，不是不安全判定。告知用户后，本轮经相同require_escalated流程重试成功；没有用间接命令绕过拒绝。普通/内存同批103项均通过，后者47.45秒，记录为m233-sanitizer-tests/details-20261020.log。

**Docker定位：** Windows构建首次提示dockerDesktopLinuxEngine管道不存在；进程列表无Docker Desktop/backend，WSL发行版也没有docker命令。这次证据只支持引擎未启动，不能据此断言旧Inference manager问题复发。正常启动已安装Docker Desktop后，Windows CLI返回Server 29.8.2；没有重置Docker/WSL、修改代理或清理全局资源。首次失败另存m233-compose-engine-unavailable-20261020.log。WSL的localhost代理提示仍存在，但本轮本地编译与公开镜像缓存构建均成功，没有宣称修复了VPN代理。

**容器验收：** 在gateway-m233-validation-20261020独立项目启动Gateway/Agent/Redis/本地模型，使用新合成卷与公开上下文。12组后台治理、8组指标、新建6项和仅替换Gateway恢复5项全部通过；检查实际模型EOF、四类暂停继续、标题去重、202持槽、Gateway替换不取消、Agent SIGKILL后恢复和显式续跑。Gateway/Agent均UID10001，实际镜像64个Python运行模块与上下文哈希一致。核验项目标签/名称/Redis挂载后，仅删除该项目4个容器、2个命名合成卷、Redis匿名卷和网络，复查均为空，镜像保留。完整过程见m233-compose-verification-20261020.json。

历史补记来自本聊天开发过程、当前源码和 evidence 记录，不表示2026-10-18重跑了旧阶段全部测试。证据路径相对本仓库。项目生成的AGENTS.md为只读，维护约定另写入README。

## 2026-10-20 M2.3.3：最终验收与独立源码同步

**最终结果：** 普通/ASan同批103项通过，不相加计数。最后标题跳过计数修正后，Python相关255项重新通过（191.565秒，含新增治理16项），不是原项目全量discover。工具最初误指定-s tools发现0项，日志保留且不计通过；改为tests/test_integration_tools.py后4项实际执行并通过。旧请求取消9组另行回归通过；最后公开副本再跑12组真实治理通过、64个运行模块输入哈希不变。容器同12组治理、指标8组、新建6项、替换恢复5项和清理均通过，详见上节和汇总。

**源码同步：** 先只读核对独立项目84个公开运行/资产文件与已验收M2.3.2基线完全一致；完整预检确认20个目标版本均已识别。备份后精确同步10个运行模块（含新增agent_work.py）、5个测试和5份说明。既有9个运行模块修改，其余75个公开文件保持；新增后85个与最终容器/测试上下文逐一匹配，重复预检0待修改。生产工具仍拒绝任何未知独立修改，没有放宽SHA校验。没有启动原项目或读取.env/业务数据库/个人文档/模型；未改只读sources或当前工作区根AGENTS，未提交/推送。

**范围与交接：** M2.3.3核心范围完成。详细“为什么这样做/改变了什么”、真实接口、计数定义、编译和运行见docs/JOB_GOVERNANCE.md；根README、方案及部署说明已更新。单Uvicorn worker、进程级计数、单worker标题不抢占；队列有界在Gateway实现，Python没有额外等待队列。原恢复逻辑与显式继续保留，不自动重放中断模型工作。真实模型/完整embedding-RAG部署、分布式并发/V3仍后续；未强杀阻塞或证明供应商停算。最终evidence/m233-verification-20261020.json链接各项原始证据，早期失败只作历史。

## 2026-10-18：Docker 恢复与 M2.1 容器复核（未通过）

**现象与定位：** 此前Docker引擎不可用；用户本次重启后，执行有10秒期限的版本查询，引擎正常响应，服务端29.8.2。确认独立测试项目 `gateway-m21-20261018` 没有同名容器后，导出83个公开运行文件生成全新构建上下文，不复制.env、业务数据库、个人文档或模型。

**结论：** 当前引擎可用。用户重启之后恢复不等于旧socket故障底层原因已经确定或永久修复。

**工作与验证：** 合并配置解析通过，Agent镜像构建完成；Gateway停在构建RUN阶段。独立探测发现容器停在Created，未完成业务检查。完整后台重启命令返回成功提示后，版本API和hello-world仍各超时20秒。本次容器验收未通过，10月16日的61项原生结果仍属于历史验收。最终观察见 `evidence/m21-docker-recheck-20261018.json`。

## 2026-10-18：测试配置隔离个人环境文件

**定位：** 基础Compose含可选agent.env。只覆盖LLM三个字段，仍可能继承其他业务环境配置；合成测试需要清空整个env_file。

**修改及理由：** `deploy/compose.test.yaml` 显式设置 `env_file: !override []`；没有读取agent.env内容，也未观察到凭据泄漏。这是测试隔离设置，不是已发生的泄漏事故。新增 `compose.validation.yaml` 仅为双网关验收提供第二实例。

**验证：** 合并配置解析通过；容器未成功运行，未核实其实际注入环境。正式部署env_file的行为保持原样。

## 2026-10-16 M1.2：上传超时先关闭 socket，408 发不出去（历史补记）

**现象：** 停滞上传用例未收到408，连接先被关闭。

**定位：** 对照 `tests/test_streaming.py` 与 `ProxySession::read_upload_block()`。Beast tcp_stream的读超时会先关闭socket，后续错误响应没有可写连接。

**修复及理由：** 改为独立应用层读取计时器，超时先发送408再清理。读取完成时取消计时器并更新版本号，使旧超时的迟到回调不能终止下一阶段。

**验证：** M1.2普通及ASan/UBSan同批45项通过，含停滞上传408。证据：`evidence/m12-normal-tests-20261016.log`、`evidence/m12-sanitizer-tests-20261016.log`；修复保留在 `src/gateway.cpp`。

**限制：** 关闭网关连接不代表Python业务已取消。

## 2026-10-16 M1.2：ASan 大文件探针被测试期限打断（历史补记）

**现象：** 32MiB上传普通构建通过，ASan出现BrokenPipe。

**定位：** 比较阶段与耗时，ASan样例约7秒，超过原测试约5秒预算；不能仅凭BrokenPipe认定流式缓冲有问题。

**修复及理由：** 大文件内存/完整性探针使用独立网关与30秒预算；停滞上传继续单独以短期限验证408，没有为测试放宽产品默认期限。

**验证：** 两套构建的哈希及内存检查通过。最终样例中途RSS增长普通384KiB、ASan10240KiB，均低于该探针12MiB阈值。证据：`evidence/m12-verification-20261016.json`。

**限制：** 单次合成样例不是生产容量结论，未实际验证416MiB整批上传。

## 2026-10-16 M1.3：临时 rw 参数未覆盖模型卷只读标记（历史补记）

**现象：** 使用 `compose run --volume ...:rw` 准备模型，写入探针仍报Read-only file system。

**定位：** 同一测试模型卷、UID10001下的探针确认失败在挂载权限，尚未进入模型下载。

**修复及理由：** 新增 `deploy/compose.models.yaml`，明确替换volumes并关闭准备阶段离线开关；正式服务继续只读挂载。准备缓存与提供服务采用不同权限。

**验证：** 准备配置可写入/删除公开探针，普通Agent模型卷不可写。证据：`evidence/m13-model-volume-20261016.log`。

**限制：** 不代表embedding依赖、实际模型下载及离线检索通过。

## 2026-10-16 M2.1：Boost.Redis 整数适配器编译失败（历史补记）

**现象：** `response<vector<int64_t>>` 报 `converter<long int, false>` 类型不完整。

**定位：** 模板错误链指向Boost1.88适配器；本机int64_t是long，而适配器支持long long。初次失败保存在 `evidence/m21-build-20261016.log`。

**修复及理由：** Redis适配处使用long long；进一步检查发现vector按服务端聚合长度reserve，改为固定 `std::array<long long,3>`，Redis输入限制64KiB。协议只需三个整数。

**验证：** 最终普通与ASan/UBSan同批61项通过，含异常聚合响应。证据：`evidence/m21-build-final-20261016.log`、`evidence/m21-normal-tests-20261016.log`、`evidence/m21-sanitizer-tests-20261016.log`。

## 2026-10-16 M2.1：压力测试中的后端与断言问题（历史补记）

**现象：** 初次并发只见6个HTTP成功；另有合成POST实际201但预期200，以及脱敏断言中的 `-1` 碰巧匹配正常request_id。

**定位：** 区分Redis放行和上游HTTP成功；检查合成后端默认小backlog与短上游期限，不能把少一个HTTP成功直接归因于Lua原子性失败。另两个断言错误可由返回值直接确认。

**修复及理由：** 测试后端backlog设128，普通请求预算2.5秒；仍严格断言总放行7/拒绝33、Redis计数7和上游到达7。POST按真实合成契约断言201，脱敏检查精确比对安全error对象。

**验证与确定程度：** 最终两套构建均通过。backlog和期限同时调整，未单变量复现，故不将上游失败唯一归因于backlog。证据：`tests/test_rate_limit.py`、`evidence/m21-verification-20261016.json`。

## 2026-10-16 M2.1：EVAL 提交后丢失响应，不能重放（历史补记）

**风险与定位：** 失败响应不等于Redis未执行。故障代理先完成真实EVAL，再丢弃网关回复，可重现“状态改变、客户端不知道”。

**实现选择：** `RedisCheck::run()` 禁用重连及未响应请求重放；返回503，接受可能已消耗一次额度。它是针对协议歧义的防护，不声称已经发生过重复计数事故。

**验证：** 故障用例只见一次EVAL、Redis计数1、网关503且未转发Python。证据：`test_11_committed_eval_lost_reply_is_never_replayed` 与M2.1最终测试记录。

## 2026-10-16/18：Docker Inference 本地端点异常（历史补记）

**现象：** CLI存在但引擎不可用/API超时；后端日志报 `initializing Inference manager`、`remove .../Docker/run/dockerInference: The file cannot be accessed by the system`，随后后端崩溃。

**定位：** 只读检查后端日志、端点元数据及重解析信息；端点带Archive/ReparsePoint属性，重解析查询也失败。正常socket也可能有同样属性，不能仅凭属性认定损坏。

**处理与验证：** 建议先退出重启；没有执行恢复出厂、清理业务卷、删除VHDX或改名运行目录。用户本次重启后引擎恢复响应。旧容器未验证状态保留在 `evidence/m21-verification-20261016.json`，本次另存复测结果。

**未确定：** 旧端点为何异常、是否还有版本/系统状态变化、是否复发，均无足够证据认定。


## 2026-10-18：版本 API 可用，但容器停在 Created（原因待定位）

**现象：** 新版Agent镜像使用缓存构建完成；Gateway镜像进入第一个RUN后没有命令输出。两个独立curl探测容器也不输出结果。

**定位顺序：** 先尝试同一Ubuntu软件源的HTTP/HTTPS小探测，随后查看容器状态。两个容器均停在Created，说明尚不能确认curl已执行，不能将无响应直接归因于软件源或VPN。容器inspect查询有10秒超时；Docker WSL能执行shell，MemAvailable约14GiB；runc初始化子进程处于等待状态。

**确认与推测：** 已确认元数据查询恢复不等于容器运行恢复。阻塞点在容器初始化阶段，尚未定位其底层原因，当前没有内存不足的证据。没有将其记为Gateway业务bug。

**处理：** 原有用户容器查询时均为停止状态，Ubuntu分发也停止。已终止本次挂起的验证客户端；尝试一次保留镜像/数据卷的Docker后台重启，命令返回成功提示，但之后版本查询与最小容器都在20秒期限内未返回。没有恢复出厂或清理业务数据。

**验证方法改进：** 环境恢复依次验证版本API、最小容器执行、依赖构建、业务闭环和故障恢复；第一层通过不能替代后续层。观察及状态保存于 `evidence/m21-docker-recheck-20261018.json`。


**本轮收尾：** 新版容器验证0项业务检查通过，不能宣称M2.1容器验收完成。仅尝试删除本次具名探测容器；清理请求也超时，尚不能确认这几个测试容器已删除，待引擎恢复后按精确名称清理。未删除任何业务卷或镜像。

## 2026-10-18：再次恢复后，最小容器实际运行通过

**现象与定位：** 用户报告Docker Desktop恢复后，再次执行有期限的版本查询和`hello-world`。首次访问Docker管道被本会话沙箱拒绝；在获准访问本机引擎后，两项检查均退出0。权限拒绝属于工具访问限制，不能归因于Docker服务故障。

**实际观察：** Desktop报告4.95.0、Engine29.8.2；版本查询约0.18秒，最小容器约0.82秒正常退出。按`gateway-m21`名称筛选的容器列表为空，未发现上轮具名探测容器残留。随后在全新`gateway-m21-recovery-20261018`项目内构建，系统依赖下载已有连续输出，之前Created阻塞暂未复现。

**确定程度与边界：** 已确认容器程序能够运行；此前Inference端点和runc等待的底层原因仍未确定，不能把恢复归因于某个未经验证的操作。此时镜像构建及业务验收仍在进行，最终结果另行追加。使用公开合成凭据和独立数据卷，不调用外部模型。

**证据：** `evidence/m21-docker-recovery-20261018.json`、`evidence/m21-compose-recovery-build-20261018.log`。

## 2026-10-18：Gateway 镜像遗漏 Boost 解压依赖 bzip2

**现象：** Docker恢复后首次完整下载Boost1.88源码包，SHA256校验显示OK，但`tar -xjf`失败，报`tar (child): bzip2: Cannot exec: No such file or directory`，构建退出2。

**定位与原因：** `deploy/Dockerfile.gateway`使用Ubuntu最小镜像，并以`--no-install-recommends`安装工具，没有显式安装处理`.tar.bz2`所需的bzip2。问题发生在源码包校验后、C++编译前，是构建依赖遗漏，不是网络下载失败或Docker容器初始化故障。此前WSL原生构建成功不能证明最小镜像依赖完整。

**修改与理由：** 在build阶段显式安装bzip2，独立一层保留已成功的编译器安装缓存，避免重下全部编译工具。最终运行阶段不增加该工具；Boost版本及校验不变。接下来重建和验证，结果追加到本文件。

**失败证据：** `evidence/m21-compose-recovery-build-20261018.log`。

**修复验证：** 重建退出0，Boost1.88校验/解压成功，Release目标完成链接；两台Gateway、Agent和Redis健康。日志为`evidence/m21-compose-recovery-build-fixed-20261018.log`，首次失败日志保留。该问题已修复并实际验证。

## 2026-10-18：Windows 验收脚本按 GBK 解码 Docker UTF-8 输出失败

**现象与定位：** 共享限流、首次6项、替换恢复5项和UID检查已经通过，随后收集`docker compose ps --format json`时，Python子进程读取线程报`UnicodeDecodeError: 'gbk' codec can't decode byte 0xa6`，stdout变为None，脚本因此失败。Windows的默认文本编码不适合该Docker输出，属于验收工具问题，不能记为业务失败或忽略后宣称整轮通过。

**修改与理由：** 本次临时验收脚本`build/m21_compose_recovery_check.py`的子进程读取显式指定`encoding='utf-8'`；没有修改应用协议或系统全局编码。首轮失败JSON保存为`evidence/m21-compose-recovery-encoding-failure-20261018.json`，修正后完整执行该次容器检查，成功采集状态并完成剩余Redis故障测试。

**验证结果：** 最终`evidence/m21-compose-recovery-checks-20261018.json`为passed，首次业务6项、替换恢复5项通过，两个应用Gateway和Agent均UID10001。两台Gateway共40请求放行7/拒绝33，Redis计数7；Redis停止时live200、ready及业务503，启动后不用重启Gateway即恢复。SSE首事件26ms、首进度46ms、result754ms，仅代表此次受控观测。该临时脚本在忽略目录，正式业务验证入口仍为`tests/compose_smoke.py`。

## 2026-10-18：M2.1 容器验收收尾及剩余范围

**收尾：** 保存镜像标识与运行日志后，校验全部容器/卷的项目标签和名称，仅清理`gateway-m21-recovery-20261018`的5个测试容器、2个合成数据卷及网络，随后查询均为空。镜像保留，未修改原项目数据。收尾操作首次自动审批超时，未执行；按工具允许重试一次后成功，属于工具审批等待，不是Docker故障。

**结论与边界：** 最新Release镜像及M2.1核心容器链路验收完成，前面的失败记录保留。83个上下文运行文件与原Python项目逐一哈希相同；没有新增C++/Python业务修改或重跑旧61项原生测试。测试只使用本地可控模型协议服务，没有验证真实模型质量、embedding依赖/模型下载/离线RAG，原Windows数据迁移也未执行。M2.2跨入口任务准入、有界排队、完整业务取消及指标仍未实现。

**证据：** `evidence/m21-docker-recovery-20261018.json`、`evidence/m21-recovery-source-check-20261018.json`、`evidence/m21-compose-images-20261018.json`、`evidence/m21-compose-runtime-20261018.log`及首次/恢复两个业务报告。

## 2026-10-18—19 M2.2：代理名额不能代表实际 Python 工作已结束

**问题与定位：** 仅在Gateway增加并发计数，HTTP超时后归还名额，但同步Python/模型调用可能继续。`test_admission.py::test_07_active_timeout_releases_only_proxy_permit`明确观察到旧工作未结束时新工作开始；这不是完整任务取消。真实链路进一步验证两条JSON返回504后，原工作仍可能最终保存一次。

**实现与理由：** Gateway新增`AdmissionController`，同一进程的JSON/SSE模型请求共用2个默认Permit、8个FIFO等待条目、4MiB排队正文预算和2秒等待期限；超额或等待过期503，上传/普通读取保留独立原流程。`Permit`为move-only RAII，finish统一归还；所有队列状态只在现有单io_context线程操作。Python在原StreamRunner中用提交前BoundedSemaphore限制真实工作，7个JSON函数通过保留签名的装饰器共用SSE执行器；Future真正完成才释放，取消HTTP等待不会提前释放。

**验证：** 10月18日普通与ASan/UBSan同批72项通过，新增准入11项；2执行+8FIFO、JSON/SSE共享、队列满/超时/字节预算、异常/停机/非法配置均覆盖。100次排队断连FD11→11。Python新增7项+原SSE12项、相关旧80项、工具4项通过。真实Gateway/Redis/Uvicorn/受控模型8项通过：原工作未结束时新JSON/SSE均被Python503拒绝，且不创建新轮次；实际完成后恢复容量。证据为`evidence/m22-normal-details-20261018.log`、`m22-sanitizer-details-20261018.log`、`m22-python-*.log`、`m22-real-agent-20261018.json`。

**限制：** 仍非完整取消；JSON BackgroundTasks标题、读取触发标题和后台202作业未纳入共享池。多Gateway的准入各自独立；共享Redis额度不等于分布式并发名额。完整说明及运行命令见`docs/ADMISSION_CONTROL.md`。

## 2026-10-18 M2.2：排队断连监听的生命周期及 FIN 约定

**代码审查：** 排队期间socket可读等待绑定Session内的cancellation_signal。最初用弱引用捕获Session，结束时可能早于取消回调释放；改为回调持有Session，确保信号存活到操作完成。没有复现独立UAF事故，不把潜在风险写成已发生的内存错误；最终内存检查和停机/反复断连用例通过。

**网络约定：** 已收齐有限JSON正文后才排队，使用异步wait和非阻塞peek，不启动等待线程。FIN视为撤回，回收队列条目/字节；TCP FIN无法证明对方读端也关闭，所以明确不支持“排队阶段shutdown写端后仍等响应”。额外流水线请求也会关闭连接。已开始代理的传输逻辑保持；`test_10_queued_write_half_close_means_withdrawal`验证该约定，不能称为无条件支持全部HTTP半关闭行为。

## 2026-10-18 M2.2：受控模型测试用了错误阻塞标记

**现象与定位：** 新真实链路脚本要求两条JSON因截止返回504，但断言失败。检查复用的`ModelHandler`发现它识别`hold-public`，新脚本误用了`block-public`，因此根本没有制造等待模型的场景。首次断言也没有记录实际返回状态，不足以定位产品问题。

**修改与验证：** 修正测试输入标记，并让断言携带两条实际状态。失败日志保留为`evidence/m22-real-agent-marker-failure-20261018.log`；重跑真实8项通过，没有为使测试通过修改产品超时或放宽业务约束。

## 2026-10-18—19 M2.2：已有 SSE 适配版本的安全升级

**问题：** 原工具仅接受初始基线或已安装的最终版本，直接更新bundle会使已安装M1.3的文件被判为未知修改。不能取消校验后覆盖独立项目。

**修改：** 每个变化文件增加明确的历史SHA256及对应升级编辑；仍先验证全部文件再写入。未知改动继续拒绝，最终状态重复执行为空操作。独立项目实际只修改web_app的7个装饰器、agent_stream共享槽模块，新增针对性测试并同步5份说明/交接文档，保留agent_loop、数据库结构、原资料和配置。工具4项通过，包括首次/已知版本升级同结果、未知改动不写任何待处理文件。

**补充收尾：** 10月19日发现SSE的Server响应头仍写0.3，而普通响应和CMake版本已为0.4，统一为0.4。仅修正版本标识，最终构建与容器复核另存当日记录；10月18日验收文件保留。

## 2026-10-19 M2.2：最终复核、文档同步与测试资源收尾

**复核：** 普通及ASan二进制完成增量重建，各定向通过11项准入检查；同批72项全套结果保留在18日文件，不把重跑计作新增用例。最终Release镜像重新构建，两台网关健康且Server0.4；实际环境为准入开启、2执行/8等待/4194304字节/2000ms，Python共享槽2，两个修改模块的容器内哈希与构建上下文及独立项目一致。旧合成文档/会话再次恢复5项通过。

**升级与保留：** 独立项目M2.2适配及5份说明/交接文档已同步，再次检查0个待修改文件。额外用重建的原始基线验证10文件首次安装及重复执行，均通过。83个运行文件仅web_app.py、agent_stream.py改变，其余运行代码/网页资产不变；没有访问原业务数据库、文档或密钥，也未自动提交/推送。

**清理：** 保存镜像标识、服务状态和运行日志后，校验项目标签和名称，仅删除`gateway-m22-validation-20261018`的5个测试容器、2个合成卷与网络；随后查询均为空。最终镜像保留，原项目及其他Docker项目数据未清理。

**范围：** M2.2核心阶段完成。完整模型取消、全部后台202/标题任务治理、分布式并发名额、指标接口和真实embedding/RAG部署仍未实现。未把容器健康/基础业务回归称为容器内全套并发压测。

**证据：** `evidence/m22-verification-20261019.json`、`m22-compose-final-20261019.json`、`m22-compose-restore-20261019.json`、`m22-source-preservation-20261019.json`及当日build/定向检查/cleanup日志。

## 2026-10-19 M2.3.1：HTTP 200 不能当作 Agent 业务成功

**调查与问题：** 原同步`model_client.answer_question()`/`agent_loop.run_agent()`和独立后台202/标题流程尚不能仅靠取消HTTP等待实现完整停止。现有`execute_turn()`也会将模型失败保存为failed后以HTTP200返回，SSE则可能在200之后发送error/done.failed；仅累计状态码会漏报这些可观察业务失败。

**本轮范围与实现：** 先交付M2.3.1基础指标，为后续取消验证提供观测；没有重写Python模型调用或宣称M2.3全部完成。新增本地GET /metrics，沿用API Key鉴权、不消耗Redis额度或模型名额，健康/指标请求不计入业务请求统计。计数在单io_context线程更新，固定状态/结束原因维度，不以原始URL或用户数据作为标签。新请求/排队/连接/拒绝/超时计数由finish统一记录，防止结束回调重复累加。

**为什么有界观察：** 使用现有Boost1.88附带Boost.JSON，单TU编译实现，无新增安装依赖。模型JSON只观察最多64KiB、深度16内的未压缩application/json；超预算/格式不可判定单独计为inspection unavailable，响应照原样转发。SSE按分块观察error/done，只保留最多8KiB单行和8KiB事件数据内容，结果大行跳过后仍识别后续终态；不缓冲完整流。支持LF/CRLF/CR和起始BOM，按请求去重error及done.failed，未观察到有效done单独计数。

**验证：** 普通及ASan/UBSan同批87项通过（原72+新增15），包括JSON误报防护、HTTP200业务失败、SSE分块/多行/超大结果/断流、实时排队指标、Redis停机快照、100次快照FD9→9等；两套结果不相加。真实Gateway/Redis/Uvicorn/本地可控模型8项通过，JSON和SSE各一次真实失败增加总计2，成功Agent read_source仍先返回进度再提交最终结果。当前记录在`evidence/m231-normal-details-20261019.log`、`m231-sanitizer-details-20261019.log`及`m231-real-agent-20261019.json`；容器验收另行追加。

**范围限制：** 指标只表示本Gateway进程观察到的代理结果；模型HTTP/Permit归零不证明Python实际工作已停。完整模型取消、后台202/标题治理和Python实际任务指标仍未实现；无外部模型或质量测试，没有修改Python业务代码/数据。

## 2026-10-19 M2.3.1：真实链路测试输入的自动审批拒绝

**工具问题：** 第一次命令把原项目路径作为只读源码输入传给会创建临时副本的测试脚本。自动审批因担心真实JSON/SSE流程可能写入原会话/SQLite而拒绝，命令没有执行；不是产品测试失败。

**更安全的处理与验证：** 按审批建议改用先前已排除.env、业务数据库/文档及模型缓存的公开构建上下文作为输入，脚本再复制到/tmp并运行受控服务。该操作获准，真实链路8项通过，输入运行源码哈希前后保持相同。后续容器同样使用公开上下文与全新合成卷，没有借助间接路径访问原业务数据，也未要求用户重复授权。

## 2026-10-19 M2.3.1：容器故障验证、统计口径与资源收尾

**验证目的与定位方法：** 指标存在不等于可用，特别需要确认故障时仍可读取、两个Gateway不会被误认为同一计数源。构建当前Release镜像，在独立项目启动两台Gateway、Agent、Redis和本地模型协议服务，分别读取快照；只对一台发业务请求检查另一台计数不变；仅重建两台Gateway检查计数重置，再读取原合成资料/会话确认业务卷仍保留。停止该项目Redis检查ready与业务返回503、metrics仍200，重启Redis检查业务无需重启Gateway即恢复。所有应用容器以UID10001运行。

**实际结果：** 容器同8项指标检查、业务新建6项、替换Gateway后恢复5项均通过；进程独立/重置、Redis故障与恢复均通过。原生普通与ASan/UBSan同批87项、隔离真实Agent8项成绩保持，不把相同用例在两种环境相加，也没有声称容器跑过原生全套。原Python83个运行/公开文件与两个公开构建上下文逐一哈希一致；本轮未改Python，未重跑其旧测试，未访问原业务数据库/文档/密钥。

**统计口径复核：** 模型HTTP在途和Permit是代理生命周期，不能替代Python实际工作统计；HTTP状态为选定响应，不保证客户端收到。业务失败只计已识别的顶层JSON状态或SSE error/done.failed，大/无效内容不能当作成功。字节字段沿用既有日志记账点：JSON正文可能在准备代理/响应时累计，流式正文按完成写块累计，不能当作精确线速流量。上述边界已写入METRICS说明，未为获得指标缓冲完整SSE。

**清理与证据：** 校验项目标签、名称和卷挂载后，仅删除`gateway-m231-validation-20261019`的5个测试容器、2个命名合成卷、Redis匿名卷及网络，复查均不存在；镜像保留。汇总为`evidence/m231-verification-20261019.json`，容器过程为`m231-compose-verification-20261019.json`，源码保留为`m231-source-preservation-20261019.json`。自动审批输入路径问题已通过公开上下文方案解决，无待授权操作。

**尚未实现：** M2.3整体未完成；完整Python/模型取消、全部后台202/标题任务治理、Python实际任务指标、分布式并发名额以及真实模型/embedding-RAG部署仍待做。方案、README、部署说明与指标解释已同步。

## 2026-10-19 M2.3.2：代理结束不等于同步模型工作结束

**问题与定位：** 复读ProxySession、StreamRunner、execute_turn、Agent和六处模型客户端创建点。M2.2证明代理总期限到期后线程仍会继续调用模型并可能保存completed；取消asyncio等待/Future不能停止已运行线程。原网关只在队列或读写错误时发现客户端退出，SSE静默等待模型时可能要等心跳/期限才能关闭上游。

**方案与原因：** 模型完整JSON正文后增加独立socket可读监听和非阻塞peek，FIN/RST立即走既有幂等finish并关闭上游。独立取消槽与强Session捕获避免撤销其他写操作或悬空取消信号。明确把执行中模型请求的写半关闭也视为撤回；普通非模型GET保留半关闭读取，不能声称TCP区分了完整退出与半关闭。

Python保留同步业务函数与SDK，新增请求Token/检查点和由工作线程拥有的异步httpx传输；取消后await任务/连接清理，再返回线程，真实Future完成才释放工作槽。GenerationCancelled专门越过SDK/宽泛Exception捕获，由StreamRunner和会话边界处理；请求范围内禁用SDK重试，保留应用层结构化协议回退。execute_turn复用failed保存安全取消说明和原输入，最终提交前再次检查，不新增表结构或另写Agent。CLI/范围外后台仍走原传输。

**验证方法：** 本地模型服务分别停在响应头前、部分正文和最终结构化请求中，观察实际连接EOF；通过Gateway查询failed记录、检查后续模型调用不再增加，再提交正常任务及相同request_id确认容量恢复和幂等。测试包含JSON断连、SSE断连、代理JSON总期限及SSE空闲期限，不只检查代理指标归零。

**边界：** 不强杀CPU/文件/SQLite/DNS等底层阻塞，不回滚已经保存的结果，也不能证明供应商停算。取消与提交竞争时completed可保留。后台202/独立标题和Python实际任务指标仍待M2.3.3；M2.3整体未完成。

## 2026-10-19 M2.3.2：两个 SSE 启动/结束竞态

**预启动问题：** 若Token在工作线程进入action前就被取消，_submit的检查可能直接抛出，work内的announce没有执行，HTTP协程可能一直等待bridge.ready。复核路径后给真实Future增加完成观察：未产生meta时明确唤醒预响应等待，取消返回安全499。定向测试在submit前取消，验证不会挂起、槽位仍按实际完成释放。

**响应边界问题：** 初版只在事件生成器finally请求取消；若ASGI响应头send失败，生成器可能根本没开始，finally不会运行。另一个窗口是业务结果已排队、标题仍运行，不能以producer finished代表客户端已经正常输出完毕。新增EventResponse的finally覆盖响应启动失败，EventBridge只在终态被正常迭代交付后标记delivered；异常退出同样取消仍绑定的后处理。正常done结束不误取消。新增“响应头发送失败”和“结果排队后断连”两项复现测试，均通过后重跑最终相关回归、真实链路与容器验收。

## 2026-10-19 M2.3.2：适配版本校验与审批服务故障

**构建夹具问题：** 公开镜像上下文不含项目README/AGENTS/开发文档，直接做全量补丁预检会因缺少文档拒绝；已生成的中间测试模块再次更新也会被视为未知版本。这是预期的校验保护，没有放宽独立项目覆盖规则。一次性准备脚本仅从先前隔离安装夹具补齐公开文档、重置生成目录的已知输入；生产工具仍先校验全部目标再写入。

**最终小版本升级：** 14项取消测试已同步后补了2项竞态测试，清单曾丢失14项版本的识别，预检拒绝且没有写入。按AST从16项资产移除准确的两个新增用例，逐字比对确认是先前已发布版本，才补入该确定SHA。未知独立修改仍拒绝覆盖；最终三文件升级及重复检查通过。没有把原源码内容任意加入可接受版本来绕过检查。

**工具故障：** 最后三个文件的首次自动审批因stream disconnected未完成，工具明确表示操作未执行、并非不安全判定。向用户说明后通过同一require_escalated审批流程重试，获准并成功；未绕过审批，没有待授权操作。

## 2026-10-19 M2.3.2：最终验收、源码同步与清理

**实际结果：** 普通与ASan/UBSan同批94项通过：23 HTTP、22流式/上传、16 Redis、11准入、15指标、7取消；两种构建不相加。100次静默模型断连FD9→9，FIN/RST/响应头前/JSON/半关闭/单次计数均通过。Python最终115项相关回归（原80+SSE12+共享槽7+取消16）通过，工具4项通过。覆盖真实SDK gzip、parse、response_format回退、两个工作取消隔离和连接清理前不释放槽位；不是原Python项目全量测试。

真实隔离Agent与最终容器分别通过同9组取消、同8组业务指标检查，含12次交替JSON/SSE取消、模型EOF、failed保存、没有后续调用、恢复和幂等。容器业务新建6项及仅替换Gateway后恢复5项通过；应用UID10001，实际镜像63个Python运行文件与最终公开上下文逐一哈希一致。没有外部模型、回答质量或完整embedding/RAG验收。

**独立源码同步：** 通过全文件版本预检、备份和精确清单同步15个不同文件（9个运行模块含新增agent_cancel.py、1个测试、5份说明），最后3个文件补齐竞态修复再同步。原83个运行/公开文件中只修改8个运行模块，其余75个保持；新增模块后84个与最终验收上下文一致。最终重复检查0待修改。未运行原应用、读取.env/业务数据库/个人文档/模型，也未修改只读sources或当前聊天生成的根AGENTS；未提交/推送。

**容器收尾：** 仅对`gateway-m232-validation-20261019`核验项目标签/名称和Redis挂载后，删除4个测试容器、2个命名合成卷、Redis匿名卷及网络；再次查询均为空，镜像保留。最终复验同样完成清理，没有全局Docker/WSL重置或删除用户业务卷。

**证据与下一步：** `evidence/m232-verification-20261019.json`汇总最终记录；普通/内存、Python verified、真实链路verified、compose和source-preservation文件保存各自结果，早期113项和中间版本日志只作历史，不替代最终115项。设计及命令见docs/CANCELLATION.md，M2.3.3下一步治理后台202/独立标题和Python实际任务指标。

**补充环境问题：** 收尾时误在Windows受限进程运行工具夹具，四项均在TemporaryDirectory内创建/清理文件时报PermissionError，尚未执行到工具断言；保留`m232-integration-tools-final-20261019.log`作为失败历史，不计为通过。没有改宿主机ACL或工具保护逻辑；在目标Ubuntu环境重跑最终4项通过，证据为`m232-integration-tools-verified-20261019.log`。本阶段验收平台是Ubuntu，不宣称Windows临时目录问题已修复。
