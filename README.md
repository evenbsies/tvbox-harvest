# tvbox-harvest

把"随手收集的视频网站 URL"变成一份 TVBox 可直接订阅的配置：在手机上通过 Issue 表单提交站点，GitHub Actions 自动探测站点类型、采集点播源、清洗去重，产出 JSON 并推送回仓库。

**只生成地址索引，不托管任何媒体内容。** 使用前请阅读 [COMPLIANCE.md](COMPLIANCE.md)。

---

## 它是怎么工作的

```
你提交 Issue ──> Actions 探测站点层级 ──> 采集 ──> 清洗/去重/过滤 ──> 生成 TVBox JSON ──> 提交回仓库 + 刷新 CDN
                                                │
                                        TVBox 订阅 index.json
```

三个平面各司其职：

| 平面 | 职责 | 实现 |
|---|---|---|
| **采集平面** | 探测、抓取、清洗、生成 | GitHub Actions（Python） |
| **数据平面** | 存放产物与站点台账 | Git 仓库 + jsDelivr CDN |
| **运行时平面** | 客户端读订阅、发起播放 | TVBox / 影视仓 等 |

关键设计：**GitHub Actions 是批处理，TVBox 是运行时。** 两者之间必须有桥。本项目对三种桥的取舍是：

- **Tier 1 / 2 站直连上游接口** —— 零依赖、零成本，这是 MVP 的主体
- **Tier 3 / 4 站经边缘网关**（可选，见 `gateway/`）—— 网关照常返回 MacCMS 格式 JSON，客户端无需任何插件
- **自研 jar 不采用** —— 能力上限与网关重叠，却要额外维护一个二进制

---

## 四级适配器

最贵的探测放最后，最便宜的放最前。绝大多数站点在第 1~2 级就被解决。

| 层级 | 类型 | 探测依据 | 单站成本 | 验证状态 |
|---|---|---|---|---|
| **T1** | 苹果CMS V10 标准接口 | 响应即 MacCMS JSON（`code`/`list`/`total`/`class`） | 20~100 请求，<2 分钟 | **真实站点**（2 个，159394 / 98604 条目录） |
| **T2** | 自定义 JSON/XML 接口 | 结构化数据但字段非标准，按字段词典打分 | 10~50 请求 | fixture 级 |
| **T3** | SSR HTML 站 | 详情页静态 HTML 中直接含 m3u8（或经一层播放页可达） | 每片 1 请求；经播放页则每集 +1 | **fixture 级**（真实站点待验证，见下） |
| **T4** | 运行时生成地址 | 有播放器容器但静态 m3u8 数为 0 | 每片 15~40 秒（无头浏览器） | **真实站点**（`4kvm`，含付费墙实测） |

这张表只写实际做到的程度。fixture 级意味着解析逻辑有回归测试、但选择器尚未对真实站点调优；
把它读成"已完成"会在首次接入时浪费时间排查一个本来就在预期内的现象。

探测结论连同**证据**一起写入 `sites/<key>.yaml` 的 `detect` 段，30 天有效。判定可解释、可回归测试——这也是为什么 `tests/fixtures/` 里存的是真实响应样本。

---

## T3 实测：详情页不给你地址，只给播放页

T3 承载的是**「普通 HTML 站」**——四类站点里数量最多、样貌最杂的一类。写它的测试时才暴露出一个真实缺口：绝大多数这类站点的详情页**根本不链接媒体文件**，播放列表指向的是**每集一个中间播放页**：

```html
<!-- 详情页 /voddetail/12345.html —— 三个链接，没有一个是 .m3u8 -->
<a href="/vodplay/12345-1-1.html">第01集</a>
<a href="/vodplay/12345-1-2.html">第02集</a>
<a href="/vodplay/12345-1-3.html">第03集</a>

<!-- 播放页 /vodplay/12345-1-1.html —— 地址在这里 -->
<script>var player_aaaa={"url":"https:\/\/cdn.example\/hls\/12345\/index.m3u8"}</script>
```

原实现要求链接必须"长得像媒体"，于是这类站点**整站解析出 0 集、被判为不可采集**——但它其实只差一跳。现在适配器识别"这些链接是页面而不是媒体"并逐集跟随，两种形态共存：

| 形态 | 详情页给什么 | 成本 | 识别方式 |
|---|---|---|---|
| A 直链 | 直接是 `.m3u8` 链接 | 每片 1 请求 | 链接匹配媒体后缀 |
| B 播放页 | 每集一个 `/vodplay/...` 页面 | 每片 1 + 每集 1 请求 | 链接不匹配媒体后缀，按序跟随 |

形态 B 的成本是**每集一次额外请求**，一部 200 集的剧会把请求数翻 200 倍，所以它的上限是独立的、且**截断必然上报**而不是静默发生：

```yaml
selectors:
  detail:
    episodes:
      - { type: css_list, selector: ".playlist a", name: "::text", url: "::attr(href)" }
    # 播放页若不是 player_aaaa 变量，用这条覆盖（MacPlayer / player_data / sources 都常见）
    # play_page_episodes:
    #   - { type: json_regex, pattern: 'MacPlayer\.playinfo\s*=\s*(\{.*?\})', path: "$.url" }
    max_play_pages: 60      # 超出会被截断，并在报告里写明 available / walked
```

集号与集名来自**详情页的链接文本**（第01集、正片……），而不是回头去问播放页"你是第几集"——播放页上的信息通常更不可靠。

同源校验在这条路径上格外重要：只跟随与详情页同一主机的链接，否则一个站点就能把采集器变成任意 URL 的抓取器。

> **验证状态：fixture 级，尚未对真实站点验证。**
>
> 上面两种形态都有回归测试（`tests/test_tier3.py`，6 项），夹具是我们自己构造的标准模板页面，
> 覆盖了直链、播放页跟随、每集上限截断、集名取自链接文本、跨站链接不跟随、无媒体时不臆造地址
> 这六个行为。两种形态的成本核算与策略上报也有测试。
>
> 但**没有对任何真实 SSR 站点跑过完整 probe + collect**。差别是实质性的：真实站点的选择器
> （标题、播放列表容器）需要按站调，`cluster_pattern` 也要靠 probe 推断。所以首次接入真实 T3 站
> 时，预期需要一到两轮 `harvest probe` 迭代来对齐选择器——这是设计内的流程，不是缺陷，但不应
> 该被"T3 已完成"这种说法盖过去。
>
> 相比之下 T1 已对两个真实站点验证（159394 / 98604 条目录），T4 已对 `4kvm` 完整验证。

---

## T4 实测：一个站点的完整拆解

`sites/4kvm.yaml` 是本项目对 T4 的参考实现，全部结论都来自实测而非推测。它值得单独说明，因为**只有当地址由运行时生成时，前面的三级才会全部失效**。

**判定链（每一步都有证据）**

| 阶段 | 观察 | 结论 |
|---|---|---|
| 前置 | `/robots.txt` 返回 `404 page not found`（Go 的 `net/http` 默认文案） | 不是 PHP/MacCMS，排除 T1 |
| T1 | 四条标准路径全 404 | 无标准接口 |
| T2 | `/api/vod/?ac=list` 返回 `{"code":400,"message":"请求路径格式错误"}` | 是自研 REST，非 MacCMS 语义 |
| T3 | 详情页 **0 个 `.m3u8`**，但有 5 个播放器标记（`data-episode`/`episodeManager`/`artplayer`…） | 结构静态、地址运行时而生成 → **T4** |

**地址从哪来**

`GET /video/play?p=<每集id>&v=<短id>&q=1080&s=<32位签名>&t=<时间戳>&k=<密钥>` 的响应体。`s` 是**签名，无法构造，只能被观察**——这一条就决定了必须用浏览器，也决定了采集只能是离线的批处理而不是按需查询。

**四个只有实测才会发现的坑**

1. **响应体在导航后会被回收。** 第一集能读到，后续集全部读不到——因为延迟 `await response.text()` 时 body 已被浏览器丢弃。改为在路由拦截时**即时**读取（`route.fetch()` → 读体 → `route.fulfill()` 原样回放）。
2. **无头模式下播放器不拉流。** 实测 35 秒内**没有任何 `.m3u8` 请求**——没有用户手势，hls.js 不会启动。所以「等流请求」这条常规思路在这个站上产出为零，必须读接口响应体。
3. **点击会被贴片广告层吞掉。** 12 秒的 preroll 遮罩拦截真实指针事件，`page.click` 必然超时。而渲染后的 `[data-episode]` 是 `<a href>`，**直接导航**到每集自己的页面即可完全绕开——既更可靠，也不依赖播放器内部逻辑。
4. **VIP 条目会返回占位 URL。** `quality_urls` 里锁定的画质 `url` 字段是字面量 `"1"`。必须按 `isvip`/`locked` 标志挑选，靠正则从响应体里"捞" `.m3u8` 迟早会捞出垃圾。

**一个必须诚实说明的限制：付费墙**

实测结果是——**这个站只免费放送第 1 集，第 2 集起需要登录 + VIP**：

```
第1集 → {"code":200, ... "isvip":false,"locked":false,"url":"<真实m3u8>"}
第2集 → {"code":401, "message":"请先登录并开通VIP会员", "quality_urls":null}
第3集 → 连 /video/play 请求都没有
```

所以针对该站的可采集产出是**每部影片 1 集**。这不是抓取缺陷，是站点运营者刻意的访问控制。绕过它属于破解付费墙，[COMPLIANCE.md](COMPLIANCE.md) 明确排除在外。

流程对此的响应是**识别并如实报告**，而不是产出一个"看起来完整、实际只能播一集"的源：命中拒绝后立即停止该剧的后续集（否则 40 集 × 11 秒 = 白烧 7 分钟），并把 `paywalled_episodes` 计入报告。

**成本模型（据此设预算）**

| 项 | 实测 |
|---|---|
| 每集浏览器耗时 | ≈ 5 秒（页面加载 + 接口往返） |
| 单片（电影） | ≈ 10 秒 |
| 付费墙站每部影片 | ≈ 12~20 秒（第 2 集即停） |

因此预算是**两个独立维度**——只限片数不够，一部 72 集的全免费剧比 20 部电影还贵：

```yaml
budget:
  max_titles_per_run: 50
  max_episodes_per_run: 250    # 真正的硬上限
  concurrency: 1               # 绝不针对同一站开两个浏览器会话
```

---

## 快速开始

### 1. 用 Issue 表单提交站点

打开仓库的 **Issues → New issue → 提交视频站点**，填写站点地址即可。表单只要求一个必填项。

提交后你会收到机器人评论，包含层级、片量、线路数、过滤条目数和订阅地址。

> 注意：所有站点默认 `enabled: false`，**空订阅是默认状态**。启用任何站点都需要你自己决定（见 [COMPLIANCE.md](COMPLIANCE.md)）。

### 2. 在 TVBox 里订阅

```
https://cdn.jsdelivr.net/gh/<你的用户名>/tvbox-harvest@main/data/tvbox/index.json
```

三份产物，按需选择：

| 文件 | 内容 | 适用 |
|---|---|---|
| `index.json` | 可直连的站点（T1/T2） | 默认订阅 |
| `index.gateway.json` | 配了网关照的站点（含 T3/T4） | 已部署网关照 |
| `index.experimental.json` | 全部站点，含实验性 | 排查用 |

单站订阅：`data/tvbox/sites/<key>.json`。

---

## 本地运行

```bash
# 1) 隔离 venv（Windows 示例；Linux/macOS 换成对应的 python 路径）
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m pip install -e .               # 让 harvest 可导入
.venv/Scripts/python -m playwright install chromium   # 仅 T4 站需要

# 2) 探测一个站点：输出层级 + 判定证据
harvest probe https://www.example.com/ --key mysite

# 3) 采集 + 生成（显式指定站点会覆盖 enabled: false）
harvest collect --site mysite --mode full --no-browser

# 4) 自检：模拟 TVBox 的三类请求
harvest verify --targets data/tvbox/index.json
```

> **为什么要 `pip install -e .` 而不是设 `PYTHONPATH=src`？**
>
> 因为不装包时，`pytest` 依然会全绿——`tests/conftest.py` 会自己把 `src/` 塞进 `sys.path`——但
> 每一个 `harvest ...` / `python -m harvest.cli ...` 都会报 `ModuleNotFoundError`。CI 最初就是这样
> 挂的：测试通过，命令行全废。装成包能让本地和 CI 走同一条路径。

### 改依赖时必须用全新 venv 验证

`requirements.txt` 里声明的版本，必须是**真的验证过**的那些。这两者一旦分家，本地就永远看不见 CI 的失败。

这不是假想问题。`requirements.txt` 曾经声明 `typer~=0.12.5`（一个依赖 click 的旧版），而开发机上是
typer 0.27.3（已经完全不依赖 click）。CI 严格按照文件装了 typer 0.12.5 + 最新 click 8.5.0，两者不兼容：

```
TypeError: Secondary flag is not valid for non-boolean flag.
```

本地全绿，CI 全红，因为跑的根本不是同一套依赖。所以：

```bash
python -m venv .venv-fresh
.venv-fresh/bin/pip install -r requirements.txt
.venv-fresh/bin/pip install -e .
.venv-fresh/bin/pytest -q          # 这才是 CI 会看到的结果
```

`tests/test_cli_surface.py` 会在本地就抓住上面那类错误：它强制构造整个命令树，而选项声明一旦与
安装的 Typer/Click 不匹配，构造本身就会抛异常——不需要等到推上去跑一轮 CI。

其他命令：

| 命令 | 用途 |
|---|---|
| `harvest plan --tiers 1,2` | 输出 GitHub Actions matrix |
| `harvest generate --flavors direct,gateway` | 不重抓，仅用已有数据重生成 |
| `harvest doctor` | 体检：配方 schema、过期探测、缺失黑名单；`--strict` 把警告也当失败 |
| `harvest prune --keep 7` | 清理 raw 归档 |
| `harvest purge-cdn` | 刷新 jsDelivr 缓存 |

**离线模式**：设 `HARVEST_OFFLINE=1` 后所有出网被拒。`pytest` 全程不联网——回归测试跑的是 `tests/fixtures/` 里的录制样本。

---

## 加一个新站点

**你不需要写代码，只需要写 YAML。** 这是整个架构的核心承诺：**为"站点类型"写代码，为"每个站点"写配置。**

```bash
python -m harvest.cli probe https://www.example.com/ --key mysite
```

探测通过后 `sites/mysite.yaml` 会自动生成并写入 `detect` 证据段。如果自动判定失败（Tier 2 需要字段映射、Tier 3 需要选择器），命令会告诉你缺什么。

以 T1 站为例，最简配方只需要：

```yaml
key: mysite
name: 示例站
base_url: https://www.example.com
adapter: maccms_v10
enabled: true
compliance:
  policy: block_adult
  bc_filter: true
```

仓库内可直接抄的模板：`sites/example-maccms.yaml`（T1）、`sites/validation-lziapi.yaml`（T1 实测）、`sites/4kvm.yaml`（T4 完整注释）。

> `enabled` 在配方和台账（`sites/registry.yaml`）里各有一份，**采集要求两处都是 `true`**。
> `harvest probe --save` 与 Issue 表单会自动同步两处；手工改的时候只改一处会让站点停用
> （停用只需要一处，这是刻意的），但**启用**得改两处，`harvest doctor` 会在不一致时提醒。

---

## 评估一个候选站点

决定要不要收录某个源之前，先量它，而不是凭印象。这个命令不写任何文件，只出数字：

```bash
python -m harvest.cli scorecard https://example.com/ --sample 60 --streams 5
```

四个维度，每个都标注了**测法**：

| 维度 | 关键指标 | 怎么来的 |
|---|---|---|
| **速度** | `ms_per_title`、`requests_per_title`、`kb_per_title`、`errors`、`statuses` | 抽样采集的墙钟时间与 `HttpClient` 的请求计数 |
| **画质** | `max_resolution`、`resolutions_seen`；另有 `claims_*` 自述值 | `max_resolution` **实际读取 m3u8 主播放列表的 `RESOLUTION=`**；`claims_*` 只是站点在 `vod_remarks` 里的说法 |
| **广告** | `gambling_domains_hit`、`site_ad_signals`、`promo_tail_urls`、`adult_entries_dropped` | 本底黑名单 + 首页广告信号扫描 + 播放地址里的推广参数 |
| **内容质量** | `playable_ratio`、`lines_per_title`、`categories`、`updated_within_7d` | 清洗后的统计 |

设计上有三条刻意的取舍，值得知道：

1. **没有总分。** 一个综合分数会把权重藏起来，而权重是「你愿意拿什么换什么」的判断——4 万条挂在满是广告的站上，并不比 3 千条干净的更「好」，除非那是你的偏好。命令只给数字，排序由你决定。
2. **「未测量」显示为 `—`，与 `0` 不同。** `0` 是量到了零，`—` 是没量到。画质尤其明显：`claims_resolution` 为 0 只说明站点没在简介里写分辨率，不说明它不高清——所以还有 `streams_*` 那一组真读清单的测量。
3. **探测未完成时显示 `tier ?`。** 站点无响应、超时或被拦截，是**这次没量到**，不是「站点不可用」。把前者报成后者会让人直接放弃一个其实正常的源。

一个实测例子（对已收录站点的抽样）：`HD` 这两个字出现在 298 条里的 289 条备注中——只认「1080/4K/蓝光」的正则会把它全部算作「无高清」，于是报出一个自信但错误的 0.0。所以画质被拆成「自述分辨率」与「泛 HD 标签」两组，且另有清单实测兜底。

### 「4K」到底怎么证伪

标签不可信是常态：`claims_generic_hd_ratio` 可以到 0.85 而 `claims_resolution` 是 0。清单也常常帮不上忙——大量站点给的是**媒体列表**（直接列 `.ts` 分片），根本没有 `RESOLUTION=`。这种情况下 `--streams` 会退一步去读流本身：

1. 取媒体列表里第一个 `.ts` 分片（`Range: bytes=0-524287`，一次请求）；
2. 在 Annex-B 里找 H.264 的 SPS（NAL type 7）；
3. 解析 `pic_width_in_mbs` / `pic_height_in_map_units` 与 `frame_cropping`，得到真实像素尺寸。

结果落在 `streams_measured_by_sps`、`sps_resolutions`，以及 **`measured_is_4k`**（阈值 3800×2100）。这条路径给出的是编码器自己写的数字，不是页面上的词。局限写死在 `probe/h264.py` 的文档里：只解 Annex-B（HLS/FLV），`.mp4` 的 `avcC` 与 HEVC 不处理，读不到就返回 `None`——**`None` 永远不等于「不是 4K」**。

实测：某站标题自称「蓝光高清」，SPS 读出来是 `1920x1080`；同一站点的 `claims_resolution` 为 0。两个数字都对，只是回答的不是同一个问题。

---

## 只保留 4K 线路

同一个片子的详情页常挂着好几条**播放线路**——闪电、非凡、量子，有时其中一条直接就叫 `4K高清`。它们不是几个片子，是同一部片子的几条流。想要「只要 4K 那条」，先看清站上到底怎么给线路起名：

```bash
python -m harvest.cli lines https://example.com/ --sample 6 --match 4K
```

输出是「线路名 → 出现在几部」，加 `--match 4K` 会标出命中的那条。然后再采集：

```bash
python -m harvest.cli collect --site example --lines-keep 4K
```

也可以写进配方，跟着站点走：

```yaml
lines:
  keep: ["4K", "2160"]   # 正则，大小写不敏感；空表示不过滤
  drop: ["广告"]
```

**判定口径只有一条：线路名。** 名字来自源站（Tier 1 的 `vod_play_from`，其他层级是页面上的线路标签），也就是用户在播放器里看到的那几个字。

- 片子标题写着「4K」不算——那是片名上的词，不是某条流的属性；`streams_measured_by_sps` 才是流的实测。
- 线路没有名字的站点（例如有些模板只给线路编号）**不算匹配**。对这类站跑 `--lines-keep 4K` 会得到 0 部，这是正确结果：无法证明它是 4K，就不假装它是。
- 一条都不剩的片子会被**整条丢弃**而不是留个空壳，并在报告里记 `line_not_matched`，附带站上实际出现过的线路名——短多少、为什么短，都在报告里。

### 第三种线路：解析线路（`parses`）

有些站的「4K」不在采集线路里，而在**播放器**里。洛雪TV 就是例子：它对外公布 8 个采集源
（`官采蓝光 / 4k60帧🔥 / 自营 …`）和 6 条解析线路（`默认解析 / 洛雪4K / TXNP / 闪电 …`），
其中「4k60帧」指向的不是采集接口而是「母鸡播放器」（`https://mujizybf.com/m3u8/?url=`）。

这类线路在 TVBox 里属于 `parses[]`，而不是 `sites[].lines`。按名字筛选写在 `config/parses.yaml`：

```yaml
parses:
  - name: 洛雪TV
    config: https://tv.lxyy.club/api/proxy.php?action=config
    routes_key: official_routes
    keep: ["4K"]          # 6 条里只留「洛雪4K」
```

实测输出（生成的 `index.json` 里）：

```json
{"name": "洛雪4K", "type": 1, "url": "https://tv.lxyy.club/json-player.php?route=official_musbpyqz&url=",
 "ext": {"player_codes": ["qq", "bilibili", "rrmj", "hmdj"], "official_only": true}}
```

**这一条与「只索引」不同**：`parses` 意味着播放请求经过对方服务器。所以它按源显式开启，
`keep` 不写就等于「不启用」，理由和边界写在 COMPLIANCE.md 第 1.1 节。单站订阅不带全局 `parses`。

---

## 仓库结构

```
src/harvest/
├── fetch/         http.py（唯一出网点：限速/重试/熔断）、browser.py（T4 嗅探）
├── probe/         detector.py（四级判定状态机）、signals.py（纯函数信号库）
├── adapters/      maccms_v10 / json_custom / html_ssr / playwright_sniff
├── clean/         normalize / dedupe / classify / blacklist / gambling
└── generate/      tvbox.py、snapshot.py、verify.py、report.py

sites/             站点台账。唯一"人可读可改"的决策中心
config/            分类映射、黑名单、TVBox 默认值、JSON Schema
data/              全部机器生成（raw / normalized / snapshot / tvbox / reports）
gateway/           可选边缘网关（Cloudflare Worker）
tests/fixtures/    录制的真实响应，离线回归的基准
```

`src/` 内**不出现任何站点硬编码**。站点知识只存在于 `sites/*.yaml` 与 `tests/fixtures/`。

---

## 自动化

| 触发 | 用途 |
|---|---|
| `schedule` 每日 03:17 UTC | 全量采集 |
| `schedule` 每 6 小时 | T1/T2 增量（`h=24`，一次请求拿全部更新） |
| `issues: opened` | 用户提交站点 → 登记 → 采集 → 回写评论 |
| `workflow_dispatch` | 手动指定站点/层级/重探 |
| `repository_dispatch` | 网关发现快照陈旧时回爬 |

有两个容易踩的坑，本项目已经处理好：

- **jsDelivr 分支缓存默认 12 小时**。push 之后必须调用 `purge.jsdelivr.net`，否则用户最长 12 小时看不到新源——pipeline 看起来"坏了"，其实一直在正常工作。所以 purge 是必需步骤，不是优化项。
- **Issue body 是不可信输入**。它以环境变量传入 Python，绝不做 shell 插值；只有通过 `validate_public_url`（拒绝内网/回环/链路本地地址）的 URL 才会被请求。相关防护有专门的单测。

### 首次部署：创建 label

Label 是**仓库状态**而不是文件，所以 fork 不会带上它们。工作流依赖四个 label：

```bash
gh label create site-request    --color 0e8a16 --description "站点提交，由受理工作流处理"
gh label create removal-request --color b60205 --description "权利人移除请求，由人工处理"
gh label create collected       --color 1d76db --description "已成功采集并加入订阅"
gh label create needs-manual    --color fbca04 --description "自动采集失败，需人工补充配置"
```

这是踩过的坑，值得说明白：仓库最初只有 GitHub 默认的那批 label，四个依赖项一个都没有。其中 `site-request` 是**整条 Issue 受理路径的门槛**——缺了它，工作流根本不会触发，而且不会有任何报错，只是悄悄什么都不发生。

现在有两层防护：`collected` / `needs-manual` 由 finalise 步骤按需创建；受理门槛除了看 label，还接受表单自身字段（`### 站点地址`）——label 是仓库状态而模板字段是文件，fork 一定有后者。所以即使忘了建 label，受理仍然可用。

---

## 常见问题

**为什么我的源没有出现在 index.json？**

按可能性排序：

1. **站点没启用。** `enabled` 同时写在配方（`sites/<key>.yaml`）和台账（`sites/registry.yaml`）里，**两处都必须是 `true`** 才会采集。默认全是 `false`，空订阅是默认状态。只要有一处不一致，`harvest doctor` 会告诉你。
2. **该站是 T3/T4 但没配网关**，所以只进 `index.experimental.json` 而不进 `index.json`。
3. **它这一轮没被采集，并且此前也从未发布过。** 配置由「本轮抓到的数据 + 上一版里已启用站点的条目」合成（carry-over），所以单站运行不会把其他站挤掉；但一个从未成功发布过的站点，本轮又没抓到数据，自然就无处可沿承。

**注意：`harvest verify` 失败不会把站点从订阅里摘掉。** 验证是**告知性**的——一次超时或限流就摘掉一个可用源，比暂时服务一个不可达条目更糟。失败会进入运行报告、运行摘要和 Issue 讨论，让人来判断。要摘掉一个站点，请显式地把它的 `enabled` 改成 `false`。

**为什么 JSON 里 `spider` 是空字符串？**

因为不需要。`type: 1` 的 MacCMS JSON 接口本身就够用；带 jar 会让配置依赖一个不由你控制的二进制。

**源站换了域名/改版了怎么办？**

探测结论 30 天过期，且采集失败率超过阈值会自动标记 `detect.stale`，下次先重探。降级链是 T1 → 试 XML 端点 → 降到 T3 解析。同时上一版产物会保留（last-good），不会因为一次失败就把订阅弄空。

**我看到某些条目被过滤掉了，合理吗？**

看 `data/reports/<date>.json` 里的 `filtered` 字段，按理由分类计数，可审计。过滤必须是透明的——一个静默删内容的过滤器比没有过滤器更危险。

---

## 免责声明

本项目是技术工具，不提供、不推荐、不背书任何具体资源站。

- 仓库中不含任何资源站列表；所有站点配方默认停用
- 使用者对"采集哪些站点、如何使用产物"承担全部责任
- 请自行确认你的行为符合所在司法辖区的法律法规

详见 [COMPLIANCE.md](COMPLIANCE.md)，其中也包含权利人/站点运营者的移除流程（24 小时内响应）。
