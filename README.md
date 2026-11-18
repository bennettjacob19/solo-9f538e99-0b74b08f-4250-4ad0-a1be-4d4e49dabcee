# svc.test. 区域发布系统

面向运维的内网 `svc.test.` 区域发布平台：FastAPI 操作网页 + dnslib UDP 权威 DNS + SQLite 版本存储。
网页整版提交 A/CNAME 记录、查看当前版本、发起真实 UDP 查询；发布原子生效，查询永不被发布打断。

## 快速开始

```bash
docker compose up --build
# 网页:  http://localhost:8000
# DNS:   dig @127.0.0.1 -p 5353 app.svc.test. A
```

单容器同时运行网页（8000/tcp）与权威 DNS（5353/udp），SQLite 落在命名卷 `zone-data` 里，重启不丢版本。

本地直跑（Python 3.11+）：

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
ZONE_DB=data/zone.db DNS_BIND=127.0.0.1 DNS_PORT=5353 WEB_PORT=8000 .venv/bin/python -m app.main
```

## 区域文本格式

每行一条记录，`#` 起为注释，名称可写相对名或 FQDN：

```
web A 10.0.0.10 60
web A 10.0.0.11 60        # 同名多 A 轮询
api CNAME web             # 别名只能指向本区域已定义的名称
app CNAME api             # 允许多级链，必须终结于 A 记录
```

每次提交即一个**完整新版本**（不是增量）。规则：

- CNAME 目标必须是本版本内已定义的 `svc.test.` 区域内名称；悬空、循环（含自环）、指向区域外，整版拒绝；
- 同一名称不能同时有 A 与 CNAME；A 值必须是合法 IPv4；
- 校验或写入任一失败，整版不生效，当前版本继续服务。

## 子域委派（NS + 胶水）

把子域交给下级权威时，在整版文本里写一处委派加一条胶水：

```
dev.svc.test. NS ns.dev.svc.test.     # 委派点：恰一条 NS
ns.dev.svc.test. A 10.0.0.53          # 胶水：NS 目标的地址，随父区返回
```

校验规则（违反即整版拒绝，旧版继续应答）：

- NS 只能指向**该子域内**一个有胶水的名称；目标不在子域内、缺胶水 A、或一个委派点写了多条 NS，均拒绝；
- 委派点本身不得再设 A/CNAME；
- 子域内（委派点之下）除 NS 目标的胶水 A 外不得有任何其他记录（A/CNAME/NS 均不可）；
- CNAME 不得指向委派点，但允许指向胶水名。

查询行为（真实 UDP）：

- 查询委派点及其任意子名（含胶水名本身、任意 qtype），父区返回**非权威转介**：
  `aa=0`、Answer 为空、Authority 含 NS 记录、Additional 仅含对应胶水 A；
- 经区域内已有 CNAME 指到胶水名时，应答只含该 CNAME，胶水不会被当成业务 A 答案返回；
- 其他名称照旧权威解析（`aa=1`）。

## 一致性与原子性设计

- **整版原子发布**：版本行 + 全部记录行（含 NS 与胶水）+ `current_version` 指针在同一个
  SQLite 事务（`BEGIN IMMEDIATE … COMMIT`）中落库；任何写入失败整体回滚，库中不会留下
  半版（也不会留下半套委派），重启后按指针恢复的只会是完整版本。
- **发布期间照常解析**：DNS 线程只读内存中的不可变 `Snapshot`；发布提交成功后一次性
  原子换引用，查询不等待、不阻塞。
- **单次查询同一版本**：每个查询在开始时捕获一次快照引用，整条 CNAME 链、最终 A 地址，
  以及转介的 NS（Authority）与胶水（Additional）都来自这同一个已提交版本，绝不跨版本拼接。
- **重启恢复**：启动时从 SQLite 读取 `current_version` 指向的版本重建快照；另有每
  2 秒（`REFRESH_INTERVAL`）的对账线程兜底，DB 指针与内存快照不一致时以库为准。
- **旧库兼容**：`records.rtype` 的 CHECK 约束在引入 NS 后变为 `('A','CNAME','NS')`。
  SQLite 不能修改 CHECK，启动时会在**单个事务**内重建 records 表并逐行复制全部旧数据；
  旧库的所有区域版本原样可读，迁移失败则整体回滚、库保持原状。

## 网页与 API

| 入口 | 说明 |
| --- | --- |
| `GET /` | 当前版本记录、发布表单（预填当前版）、查询表单、版本历史 |
| `POST /publish` | 表单 `zone_text` + `note`；校验失败 400、写入失败 500、成功 303 |
| `GET /query?name=&qtype=` | 网页发起真实 UDP 查询（A/CNAME/NS/ANY），显示 Answer/Authority/Additional 三段 |
| `GET /api/current` | 当前版本 JSON |
| `GET /api/versions` | 版本历史 JSON |

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ZONE_DB` | `./data/zone.db`（镜像内 `/data/zone.db`） | SQLite 路径 |
| `DNS_BIND` / `DNS_PORT` | `0.0.0.0` / `5353` | UDP 权威 DNS 监听 |
| `WEB_HOST` / `WEB_PORT` | `0.0.0.0` / `8000` | 网页监听 |
| `DNS_QUERY_HOST` | `127.0.0.1` | 网页发起查询的目标地址 |
| `REFRESH_INTERVAL` | `2.0` | 内存快照与 DB 对账周期（秒） |

## 验证

```bash
.venv/bin/python tests/e2e_test.py
```

覆盖：事务原子性（唯一约束触发半途失败 → 无半版）、整版校验拒绝（悬空/循环/外部目标/
类型冲突/非法地址/区域外名称）、委派校验（缺胶水/委派点带 A 或 CNAME/子域内多余记录/
NS 目标在子域外/多条 NS/顶点 NS/CNAME 指向委派点）、委派解析（委派点及子名返回
aa=0 转介、Authority 含 NS、Additional 仅含胶水、CNAME 到胶水不泄露为业务 A）、
链式解析与 NXDOMAIN/REFUSED/NODATA、发布期间并发查询的单版本一致性（转介的 NS 目标
与胶水 IP 永远配对于同一版本）、写入失败（DB 路径被替换为目录 → 500、旧版照常应答、
恢复后可继续发布）、重启后版本与委派完整恢复、旧版（无 NS 约束）数据库的启动迁移与
旧版本可读性。
