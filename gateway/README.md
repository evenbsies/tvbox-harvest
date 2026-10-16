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
| `GET /` | 自查页：列出所有站点、片量、快照生成时间 |
| `GET /health` | 存活与当前快照基址 |
| `POST /admin/refresh` | HMAC 签名，触发仓库的 `repository_dispatch` 重新采集 |

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

# 指向你的仓库（public 仓库才能走 jsDelivr）
# 编辑 wrangler.toml 里的 REPO

# 只在需要 /admin/refresh 时才配；它同时是 HMAC 校验密钥
wrangler secret put DISPATCH_TOKEN

wrangler deploy
```

`DISPATCH_TOKEN` 需要一个 GitHub **fine-grained PAT**，权限只要两项：

- `Contents: Read`
- `Actions: Read and write`

把它填进 `wrangler secret` 后，同一个值也用作 `/admin/refresh` 的 HMAC 密钥。

### 修改 config 后回写 config 变量

```bash
# 让 Actions 生成时自动带上网关地址，T3/T4 站才会进 index.gateway.json 的 api 字段
gh variable set HARVEST_GATEWAY --body "https://<your-worker>.workers.dev"
```

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

## 不部署网关会怎样

完全可以用。`data/tvbox/index.json` 是**直连版**，只包含能直连的 T1/T2 站，零依赖零成本。不部署网关时：

- T3/T4 站的快照仍然会生成在 `data/snapshot/`，只是没有运行时消费者
- 它们的条目只出现在 `data/tvbox/index.experimental.json` 里，且 `api` 为空，不会污染主订阅

这是刻意的默认：**宁可少给一个源，也不给一个打不开的源。**
