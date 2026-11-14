# 消息简报 wxbrief

懒猫微服应用：只读读取「云微信」（`cloud.lazycat.catlink.wechat`）的聊天记录，原文全部保留，给 agent 提供 JSON 接口，手机上也能翻看和搜索。

A Lazycat app that reads the cloud WeChat's history read-only and serves it to agents as JSON, plus a phone-sized page.

## 能做什么

- 每 30 秒同步一次新消息，存进自己的 SQLite（`/lzcapp/var/brief.db`），不改动微信的任何数据。
- 原件：图片（解密微信 4.x 的 `.dat`，wxgf 格式用 ffmpeg 转成 jpg）、视频、文件、语音（SILK 转 WAV）。
- 语音如果在微信里点过“转文字”，转写会写进消息文本。
- 多个 API key，可命名、吊销；每个 key 附一行给 agent 的说明，复制即用。
- 网页（需懒猫登录）：会话列表、搜索、聊天记录、内嵌图片和语音、key 管理。

## 接口

除 `/api/help` 外都需要 `Authorization: Bearer <key>`。完整说明见应用里的 `/api/help`。

```
GET /api/chats?q=&kind=group|private&limit=
GET /api/messages?chat=&kind=&since=&until=&after_id=&limit=
GET /api/search?q=&chat=&limit=
GET /api/media/<id>[?thumb=1][&format=silk]
GET /api/status
GET /api/help
```

## 构建与安装

```sh
lzc-cli project build          # 会先跑 fetch-vendor.sh 下载并校验 SILK 解码器
lzc-cli app install dist/*.lpk # 或者在盒子上：lpk-manager install <lpk>
```

## 实现要点

- 数据库：微信 4.x 的 SQLCipher 4 库，口令在微信登录时用 gdb 断点从 WCDB 取得（方法参考 [TANGandXUE/wcdb-key-tool](https://github.com/TANGandXUE/wcdb-key-tool)，MIT），每个库的密钥由 PBKDF2 派生；只解密到容器内 tmpfs 的临时副本。
- 图片 `.dat` V2：前 1 KB 为 AES-128-ECB，尾部单字节异或；AES 密钥为 `md5(uin + wxid)[:16]`，启动时用缩略图的 PKCS7 填充块穷举 uin 的高 24 位得到（数秒）。
- 语音：`media_*.db` 的 `VoiceInfo` 表，SILK v3，用 [silk-python](https://pypi.org/project/silk-python/)（BSD）解码。
- worker 需要 `pid: host` 和 `SYS_PTRACE` 才能在微信重新登录后重新取得口令；对云微信目录只读挂载。

只用于读取你自己的微信数据。

## License

MIT
