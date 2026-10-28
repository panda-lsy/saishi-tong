# 赛事通 · saishi-tong

用腾讯元宝 SearchPro 发现开发、计算机类赛事，以来源证据和审核流程把线索整理成可查询的中文技术赛历。仅收录技术比赛与技术实践征文，不接收普通学生作文或文学文化征文；题目提到AI不能替代技术参赛要求。

演示：https://competition.shengxia.me/ · 版本：0.2.2 · 许可证：[MIT](LICENSE)

## 功能

- 游客直接浏览公开目录。普通关键词、状态、线上/线下/混合、国内/国外筛选只查本地SQLite，不调用搜索或模型。
- 发现与公开目录只展示已校对确认且发布、仍在报名/征集的赛事；结束赛事可恢复地移入回收站。每条附来源与原检索时间，正式赛名和报道标题分开保存。
- 浏览器显式在线查找支持类型意图、CSDN/魔搭站点和网页时间。Web Crypto计算TC3签名，SecretKey不离开浏览器；服务端仅转发至固定腾讯SearchPro地址。
- 解释、范文、列表、模型资源和部分技术教程不直接成为赛事；排除原因可查看。证据不足线索保留待核，不能保证所有结果都相关。
- 管理员新线索进入未核验草稿；OpenClaw worker用SearchPro、官网与真实模型核验，之后人工待核。显式校对确认并发布才公开。
- 管理端支持日期选择、编辑抽屉、批量选择、任务进度、重复合并预览、保留独立决定及可恢复回收站。

截止、奖励、主办不明则留空。完整年份截止按北京时间更新展示状态，缺年和模糊范围不猜。报名资格与开放情况以官方为准。

## 快速开始

要求Python 3.12（已验证）。网站无需Node.js、腾讯密钥或OpenClaw即可启动。

```bash
git clone https://github.com/panda-lsy/saishi-tong.git
cd saishi-tong
python -m venv .venv
# Linux/macOS
. .venv/bin/activate
# Windows PowerShell改用：.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m saishi_tong.web.app
```

打开 http://127.0.0.1:8787 。首次运行创建空库，不填虚构赛事；`/healthz`可检查健康。数据库默认`data/contests.db`，可通过进程变量`SAISHI_DB_PATH`改路径；保留此文件即可保留赛事。

### 管理员

网站读取进程变量`SAISHI_ADMIN_TOKEN`，或当前用户`~/.config/saishi-tong/tencent.env`中的同名字段；worker还可读取`SAISHI_WORKER_TOKEN`。配置文件按字段白名单读取，不读取文件中的腾讯CAM。

用密码生成器产生至少32字节熵的独立随机令牌，保存到受保护的本机配置，限制文件权限，不提交仓库。参考`examples/web.env.example`。重启后在`/admin`输入管理员令牌。未配置时管理功能关闭，公开浏览仍可用。

登录后改用HttpOnly会话与CSRF保护，原令牌不是Cookie。刷新同一网站进程可保留会话；网站重启后需重新登录。退出可撤销访问。

### 可选：浏览器在线查找

在`/settings`展开本地密钥，填写自己有SearchPro权限的SecretId/SecretKey。使用HTTPS或本机地址，在发现页确认实际查询词后点击在线查找。每次最多10个网页、一次请求，无自动付费重试。

按账号已开通的版本选择“标准版 / 旗舰版”或“极速版”；极速版请求不包含官方不支持的Mode和Cnt。此选项只调整请求参数，不能切换账号的服务版本。单站结果会再次核对URL域名；网页时间中的m3为本月及前两个月，y1为本自然年。

密钥明文保存在localStorage，并非加密保险箱；共享设备请勿保存，可随时一键清除。短期签名请求临时包含SecretId；服务端不保存签名或SecretId，不接收SecretKey。游客结果仅当前页；管理员有效新来源进入私有未核验草稿。

站点、网页时效和分值不能证明报名开放。在线查找使用用户自己的腾讯额度；无密钥仍可查本地目录。

### 可选：核验worker

worker当前支持Linux，需要独立安装和配置OpenClaw及可用模型。安装或启动网站不会自动运行模型。

1. 将`examples/worker.env.example`复制到worker主机`~/.config/saishi-tong/worker.env`，填写自己的站点URL、worker令牌（与网站配置一致）。
2. 在worker主机`~/.config/saishi-tong/tencent.env`配置自己的SearchPro CAM；不要发给网站或浏览器。
3. 在OpenClaw配置工作区指向项目根目录的`saishi`智能体，确认模型可用。提示规范见`skills/saishi-curate/SKILL.md`和`skills/saishi-sources/SKILL.md`。
4. 执行`python -m saishi_tong.openclaw_worker --once`。`deploy/`含systemd参考单元，需按自己路径和模型调整再启用。

仓库不赠送模型或搜索额度。失败和缺证据不会伪造成功；AI通过不能直接公开。受托校对可通过管理员API的`review_note`记录授权与依据，不冒充用户本人复审。

## 测试

```bash
python -m unittest discover -s tests -q
# 可选Node.js 22+，仅前端测试需要
node tests/frontend-dom.mjs
```

测试使用临时数据库、合成凭据。公开来源摘录只用于回归，不是生产种子。模拟回放不等于真实模型或搜索调用。

CLI是独立联网入口，与网站普通搜索不同。执行`python -m saishi_tong --help`查看用法；worker工具为`bin/saishi-searchpro`、`bin/saishi-fetch`。

架构与边界见[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，复现记录见[docs/VALIDATION.md](docs/VALIDATION.md)。

## 边界与来源

普通用户提交收录/本人回执、云端搜索历史尚未开放。来源采集由管理员手动触发；不提供爬虫集群、自动报名、组队社交或完整日历。

代码采用MIT。外部网站摘录、赛事名称和平台标识保留原权利，见[NOTICE.md](NOTICE.md)。仓库与发布包不含生产库、私人草稿、真实密钥、Cookie、内部运维归档或个人智能体记忆。
