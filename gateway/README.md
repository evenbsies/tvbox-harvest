# 边缘网关（可选）

让 **Tier 3 / Tier 4 站点真正能在 TVBox 里用起来** 的那座桥。

---

## 为什么需要它

GitHub Actions 是**批处理**，TVBox 是**运行时**。对 Tier 1/2 站这个落差是零——站点自己就会应答 MacCMS 格式的请求，订阅直接指向它即可。对 Tier 3/4 站不是：

- Tier 3 站的播放地址藏在页面里，没有可供播放器调用的接口
- Tier 4 站的地址由页面 JS 在运行时生成，而且常常带**签名**（例如 4kvm 的 `/video/play?...&s=<32位签名>&t=<时间戳>&k=<密钥>`）——**签名无法自行构造，只能被观察**

批处理任务能捕获它，播放器不能复现它。

所以采集流程把结果写成**与 MacCMS 响应完全同形**的快照，这个 Worker 再把快照按同一种形状吐回去。它是一层只读投影，这正是它可靠的原因：

- 没有会失效的上游
- 没有会限流我们的站点
- 客户端不需要任何插件，因为 `type: 1` 本身就等于「MacCMS JSON」

---

## 路由

| 路由 | 说明 |
|---|---|
| `GET /vod/{key}?ac=class` | 分类表 |
| `GET /vod/{key}?ac=list&pg=1&t=<分类>|&wd=<关键词>|&limit=20` | 列表 / 搜索（首页带分类表，与 MacCMS 一致） |
| `GET /vod/{key}?ac=detail&ids=<sid,sid>` | 详情，含 `vod_play_from` / `vod_play_url` |
| `GET /tvbox/index.json` | **订阅配置本身**（另有 `index.gateway.json` / `index.experimental.json`） |
| `GET /tvbox/sites/{key}.json` | 单站订阅配置 |
| `GET /` | 自查页：列出所有站点、片量、快照生成时间 |
| `GET /health` | 存活与当前快照基址 |
| `POST /admin/refresh` | HMAC 签名，触发仓库的 `repository_dispatch` 重新采集 |

订阅地址可以**就是网关自己**：

```
https://<your-worker>/tvbox/index.gateway.json
```

为什么不直接用 jsDelivr：它的**分支引用**在每个边缘可缓存 12 小时，而且 purge 之后各边缘还会分歧
几分钟（实测：push 完 `cdn` 与 `testingcf` 已是新版本，`fastly` 与 `gcore` 还是旧的）。订阅者正好
落在旧边缘上，看到的就是一个悄悄丢了最近一次采集的源列表。走网关，陈旧窗口被压到 `CACHE_TTL`
（默认 10 分钟），而配置和数据在同一个 origin 上——顺带少了「这个域名通不通」一个变量。

配置类路由是**白名单**，不是通用透传：这个 Worker 是若干已知文件的投影，订阅地址下面挂一个开放
代理是另一回事。

订阅地址形如：

```yaml
key: <key>
name: <name>
type: 1
api: https://<your-worker>.workers.dev/vod/<key>
searchable: 1
quickSearch: 1
```

用 `harvest generate --gateway https://<your-worker>.workers.dev` 生成时，所有 T3/T4 站会自动填好这个地址，产在 `data/tvbox/index.gateway.json`。

---

## 部署

```bash
cd gateway
npm install
npx wrangler login          # 浏览器登录，账号绑定，必须在你自己的机器上做

# 确认 wrangler.toml 里的 REPO 是你的仓库（public 仓库才能走 jsDelivr）
# 默认已填 evenbsies/tvbox-harvest

npx wrangler deploy
```

**首次部署不需要任何 secret。** `DISPATCH_TOKEN` 只服务于 `/admin/refresh`（主动触发重采），
读路径一个都不用。先部署、先验证订阅能用，再决定要不要配它。

`npx wrangler deploy --dry-run` 可以在登录前先确认打包没问题（当前产物 9.32 KiB / gzip 3.29 KiB）。

`DISPATCH_TOKEN` 需要一个 GitHub **fine-grained PAT**，权限只要两项：

- `Contents: Read`
- `Actions: Read and write`

把它填进 `wrangler secret` 后，同一个值也用作 `/admin/refresh` 的 HMAC 密钥。

### 部署后自查（拿真实 URL 验一遍）

`wrangler deploy` 成功会打印 `https://tvbox-harvest-gateway.<子域>.workers.dev`。先用它跑这四条：

```bash
GW="https://tvbox-harvest-gateway.<子域>.workers.dev"

curl -s "$GW/health"
# {"ok":true,"snapshot_base":"https://cdn.jsdelivr.net/gh/<REPO>@main/data/snapshot","ttl":600}

curl -s "$GW/" | head -c 400
# 自查页：已发布站点 + 片量 + 快照时间

curl -s "$GW/vod/validation-lziapi?ac=class" | head -c 200
# {"code":1,...,"list":[{"type_id":"1","type_name":"电影"},...]}  (44 个分类)

curl -s "$GW/vod/validation-lziapi?ac=list&limit=2"
```

`/health` 的 `snapshot_base` 是**唯一**需要盯的字段：它必须是 jsDelivr 上真实存在的那条路径。
若它指向别处，`/` 和 `/vod/*` 都会 500，而 `/health` 依然 200——这个不对称是有意的，健康检查
不该因为数据源缺失就判定 Worker 挂了。

T1/T2 站走直连，不需要网关；用它验证的意义是**证明 Worker 能读到快照**，这一步通了，之后启用
T3/T4 站才只取决于采集，不取决于部署。

### 让 Actions 生成时带上网关地址

T3/T4 站的 `api` 字段由 `harvest generate --gateway <url>` 填入。要让**定时运行**也带上它，
在仓库层面加一个变量（Settings → Secrets and variables → Actions → **Variables** 标签 →
New repository variable）：

| Name | Value |
|---|---|
| `HARVEST_GATEWAY` | `https://tvbox-harvest-gateway.<子域>.workers.dev` |

装了 `gh` 的话等价于：

```bash
gh variable set HARVEST_GATEWAY --body "https://<your-worker>.workers.dev"
```

> 用 Variables 而不是 Secrets：这个地址会出现在**公开的** `index.gateway.json` 里，本来就不是
> 秘密；放进 Secrets 反而无法在 `wrangler.toml` 之外复用。


---

## 不连外网也能验证（本地镜像）

`wrangler dev --local` 里的 `fetch` **不走系统代理**。在这台机器上直接指着 jsDelivr 会得到
`{"code":500,"msg":"Network connection lost."}` —— 那是本机网络的问题，不是 Worker 的问题。
用本地镜像代替 CDN，就能把 Worker 本身测干净。

1. 起一个**多线程**静态服务器，根目录是仓库的 `data/`：

```python
# save as serve_mirror.py, then: python serve_mirror.py
import functools, http.server, socketserver

class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

Server(("127.0.0.1", 8899), functools.partial(Handler, directory="data")).serve_forever()
```

> 单线程的 `python -m http.server` 会被 Worker 的并发请求打断，报 `Network connection lost`。

2. 把 `SNAPSHOT_BASE` 指向它，然后逐个路由验证：

```bash
cd gateway
npx wrangler dev --local --port 8787 --var SNAPSHOT_BASE:http://127.0.0.1:8899/snapshot

curl -s 127.0.0.1:8787/health
curl -s "127.0.0.1:8787/vod/<key>?ac=class"
curl -s "127.0.0.1:8787/vod/<key>?ac=list&limit=2"
curl -s "127.0.0.1:8787/vod/<key>?ac=detail&ids=<vod_id>"
curl -s "127.0.0.1:8787/vod/<key>?ac=list&wd=<关键词>"
```

本机实测（2026-10-17，`validation-lziapi`）：`ac=class` 44 个分类；`ac=list` 293 部 / 147 页；
`ac=detail&ids=154717` 返回 `vod_play_from` 与 12 集 `vod_play_url`；`ac=list&wd=伦敦` 命中 1 部。

### `SNAPSHOT_BASE` 决定一切

`/` 与刷新路由曾经把 `data/tvbox/` 的地址写死成 jsDelivr，覆盖 `SNAPSHOT_BASE` 之后一访问 `/`
就 500。现在配置地址由 `SNAPSHOT_BASE` 推导（去掉结尾 `/snapshot`、换成 `/tvbox`），所以自建镜像
（S3 / R2 / 本地目录）能同时服务快照与配置。**这就是本地验证能成立的原因。**

---

## 触发刷新

```bash
BODY='{"site":"4kvm"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$DISPATCH_TOKEN" | awk '{print $2}')
curl -X POST "https://<your-worker>.workers.dev/admin/refresh" \
     -H "x-signature: $SIG" -H 'content-type: application/json' -d "$BODY"
```

**为什么用 HMAC 而不是 Bearer Token**：这个端点能消耗仓库的 Actions 配额。日志里意外泄露一行 URL 不该等于把触发权交出去，所以签名覆盖请求体、且校验是常数时间的。

---

## 设计要点

**只读投影。** Worker 不发起到源站的请求。它读 `data/snapshot/<key>/*.json`（经 jsDelivr），按需分片，按 `manifest.json` 里的 `counts` 知道有几片。

**isolate 内记忆化。** 快照一天最多更新几次，而播放器可能发上百次请求。解析后的分片缓存在 isolate 内存里（`CACHE_TTL` 秒，默认 600），常见路径变成纯内存读取。

**stale-while-error。** CDN 抖动时返回上一次成功解析的数据，而不是把订阅打成空的。一个空订阅会让用户以为源挂了。

**备用宿主展开为线路。** 4kvm 的 `window._pdf` 里有备用域名，播放器原本在 HLS 失败时自己换域名重试。快照生成阶段已经把它们展开成额外线路（`主线路-alt`），所以 TVBox 的「切换线路」天然实现了同一件事——而且是**可见的**，用户能自己选。

**错误也保持 MacCMS 形状。** 出错时返回 `{code: <非1>, msg, list: []}`，而不是 HTTP 500 加一段 HTML——否则客户端容易把一次瞬时故障当成一个空源缓存起来。

---

## 大陆网络下的两个硬约束（实测，2026-10-17）

这两条不是偏好问题，是「订阅地址在播放设备上打不开」的程度问题。

### 1. `*.workers.dev` 必须换成自定义域名

`wrangler deploy` 默认给你的 `<name>.<子域>.workers.dev` 在大陆网络上被 DNS 污染：

```
DNS: ['2a03:2880:f10c:283:face:b00c:0:25de', '50.117.117.42']
TCP 2a03:...:25de: FAIL TimeoutError
TCP 50.117.117.42: FAIL TimeoutError
```

第一条是 **Facebook 的地址段**（`face:b00c` 是它的标志），第二条是常见的污染地址；两个都连不上。
换成已托管在 Cloudflare 的域名下的子域，解析到真实 anycast（`104.21.82.39` / `172.67.152.170`），
直连 200：

```toml
# 必须是顶层键，放在 [vars] 之前
routes = [
  { pattern = "tvbox.<你的域名>", custom_domain = true }
]
```

> **TOML 表的作用域陷阱**：`[vars]` 会一直延续到下一个表头，所以 `routes` 写在 `[vars]` 下面
> 会变成一个名叫 `routes` 的**环境变量**，自定义域名根本不会被创建。`wrangler deploy` 的输出
> 会把它列在 `- Vars:` 下面——看到这个就是踩坑了，正确时输出的是
> `tvbox.<你的域名> (custom domain)`。

### 2. 客户端侧不要用 `cdn.jsdelivr.net`

网关自己读快照与配置都是从 Cloudflare 边缘发起的，不受这条限制；它们默认走
`raw.githubusercontent.com`，原因见下一条。但**订阅配置文件本身是播放设备去拉的**，
如果用 jsDelivr，那一步得挑可达的边缘域名：

| 域名 | 直连结果 |
|---|---|
| `cdn.jsdelivr.net` | 三次全部 `000` |
| `fastly.jsdelivr.net` | 200 |
| `gcore.jsdelivr.net` | 200 |
| `testingcf.jsdelivr.net` | 200 |

同一个 jsDelivr，换边缘域名而已。**部署完必须用播放的那台设备实测一次订阅地址**，不要用构建机
的结果代替。

### 3. 网关自己不要走 jsDelivr 的分支缓存

一次 514 部重采之后的实测：`cdn.jsdelivr.net` 还在返回上一版（16 部）的 manifest，
而 `fastly` / `gcore` / `testingcf` 已经是新的，`purge` 也没能修好 `cdn`。
jsDelivr 把分支引用解析成 commit 之后按边缘缓存最长 12 小时，各边缘的解析时刻不同，
所以在「刚提交完」这个窗口里，它给出的东西是不可预期的——而网关恰恰是那个必须和
最近一次提交一致的使用者。因此 `snapshotBase` 与 `configBase` 的默认值都是
`raw.githubusercontent.com`（没有分支解析缓存，push 后分钟级可见），
`SNAPSHOT_BASE` / `CONFIG_BASE` 仍然优先，本地镜像和自建存储照旧可用。

---

## 故障排查：域名突然解析不到

症状：`dig`/`curl` 报 `getaddrinfo failed`，但**同一个域下的其它子域正常**，Worker 的
自定义域名绑定在 API 里也显示 `enabled: true`。实测就这样丢过一次记录：

```
myeven.ccwu.cc            -> 104.21.82.39   ✓
yvigojknjnn.myeven.ccwu.cc -> 104.21.82.39   ✓
tvbox.myeven.ccwu.cc       -> NXDOMAIN       ✗   ← 绑定存在，记录没了
```

原因是**绑定记录与 DNS 记录会脱钩**：`wrangler deploy` 看到绑定已存在就不再碰 DNS，
于是重复部署也修不好。恢复步骤（不需要 DNS 权限，`workers:write` 就够）：

```bash
# 1. 删掉这条失效的绑定（换成你的 domain id，从下一条命令里找）
curl -X DELETE "https://api.cloudflare.com/client/v4/accounts/$ACCOUNT/workers/domains/$DOMAIN_ID" \
     -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"

# 2. 重新部署，wrangler 会重建绑定并顺带重建 DNS 记录
npx wrangler deploy
```

查 binding id：

```bash
curl "https://api.cloudflare.com/client/v4/accounts/$ACCOUNT/workers/domains" \
     -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"
```

注意 `POST /workers/domains` 用 API token 鉴权会返回 `405 Method not allowed for this
authentication scheme`——重建这条路只能走 wrangler，不能直接调 API。

诊断顺序也很重要：先看同域其它子域解析是否正常。都正常就说明是这条记录的个别问题，
不必怀疑 NS、代理或本地网络；`ipconfig /flushdns` 只在「所有域名都解析不了」时才有意义。

---

## 不部署网关会怎样

完全可以用。`data/tvbox/index.json` 是**直连版**，只包含能直连的 T1/T2 站，零依赖零成本。不部署网关时：

- T3/T4 站的快照仍然会生成在 `data/snapshot/`，只是没有运行时消费者
- 它们的条目只出现在 `data/tvbox/index.experimental.json` 里，且 `api` 为空，不会污染主订阅

这是刻意的默认：**宁可少给一个源，也不给一个打不开的源。**
