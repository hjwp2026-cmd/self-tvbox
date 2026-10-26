# self-tvbox · 自维护 TVBox 接口

自己维护的点播 + 直播接口,每天全自动体检,坏源自动剔除,不依赖任何个人维护者的单点。

## 📺 影视仓配置地址(电视填这个)

```
https://down.nigx.cn/raw.githubusercontent.com/hjwp2026-cmd/self-tvbox/main/tvbox.json
```

配置路径:影视仓 → 设置 → 配置地址。

- 点播:聚合精选源,探测存活后自动纳入/剔除
- 直播:每频道保留实测最快的 3 条线路,连续 2 次体检失败才剔除(防误杀)
- 自动更新:每日 09:00(北京时间)GitHub Actions 体检并推送;可在仓库 Actions 页手动触发

## 🗂 仓库内容

| 文件 | 说明 |
|---|---|
| `tvbox.json` | 影视仓配置入口(点播源 + 自维护直播) |
| `live.txt` | 直播列表(实测存活线路,标准 TVBox 格式) |
| `status.md` | 最近一次体检报告 |
| `scripts/check.py` | 体检脚本(拉上游 → 逐线路测活 → 生成) |

## 🔧 自己动手

本地跑一次体检(需要 python3):

```bash
python3 scripts/check.py
```

新增点播源:编辑 `scripts/check.py` 里的 `VOD_SOURCES`。
上游更换:改 `UPSTREAM_URL`(维护者挂了就换一个类似的 tvbox 直播仓库)。

## ⚠️ 说明

- 访问链路用 nigx 镜像(电视直连可用;GitHub raw / jsDelivr 直连均不通)
- 直播源为公开收集线路,仅供家庭自用;播放失败时到 `status.md` 看线路存活情况