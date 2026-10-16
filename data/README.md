# data/ — 全部由采集流程生成

**这个目录下的内容由 `harvest` 在工作流运行时确定性重建。** 其中一部分会提交进仓库，一部分不会——这个划分不是随意的，它对应 [COMPLIANCE.md](../COMPLIANCE.md) 里"只发布地址索引"的承诺。

## 提交什么，不提交什么

| 路径 | 是否提交 | 理由 |
|---|---|---|
| `tvbox/` | **提交** | 订阅入口本身。`https://cdn.jsdelivr.net/gh/<owner>/<repo>@main/data/tvbox/index.json` 只有在文件确实存在于仓库里时才可用——这是本项目的核心交付物 |
| `snapshot/` | **提交** | 边缘网关（`index.gateway.json`）按分片读取目录，分片不在仓库里网关就无数据可服务 |
| `raw/` | 不提交 | 第三方页面的逐行回放，是**唯一属于"复制内容"而非"派生索引"的一类** |
| `normalized/` | 不提交 | 纯调试中间态 |
| `reports/` | 不提交 | 运行遥测，没必要进 git 历史 |

提交出去的是**地址索引**：站点 key、分类名、片名、播放地址 URL。不含页面正文、不含媒体、不含原始响应。所有产物每轮**从零重建**，因此仓库里不会积累某个后来被停用站点的历史残留。

所有站点配方默认 `enabled: false`，所以刚克隆的仓库跑完第一轮产出的是**空 `sites` 数组**——`index.json` 是有效的，只是没有站点。这是预期行为。

## 目录结构

```
raw/<key>/<date>.json.gz          原始响应归档，保留最近 7 轮（prune 自动清理）
normalized/<key>.json             站点无关的统一中间模型，调试用
snapshot/<key>/manifest.json      清单：分类表、计数、搜索索引、sid 反查表
snapshot/<key>/list-part-NNN.json   列表分片（每片 ≤5MB）
snapshot/<key>/detail-part-NNN.json 详情分片，含 vod_play_from / vod_play_url
tvbox/index.json                  订阅入口（直连版：只含 T1/T2）
tvbox/index.gateway.json          含 T3/T4（需要部署边缘网关）
tvbox/index.experimental.json     全部站点，含实验性条目
tvbox/sites/<key>.json            单站订阅
reports/<date>.json               结构化运行报告（过滤统计、博彩命中、验活结果）
reports/<date>.md                 同一份报告的可读版本
```

## 本地查看

```bash
harvest collect --site <key> --mode full      # 生成
cat data/reports/$(date +%F).md               # 本次运行做了什么
harvest verify --targets data/tvbox/index.json # 模拟 TVBox 的三类请求
```

报告里的 `filtered` 字段按理由分类计数（成人、博彩站点级、博彩条目级、语法剔除）。过滤必须是可审计的——一个静默删除内容的过滤器比没有过滤器更危险。
