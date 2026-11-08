# dsh-web-auth-fix

DeepSeek Harness（DSH）**0.2.2-rc.1** 桌面端「本地 Web 认证失败」的**本地热补丁 v2**。

补丁只改动 DSH 安装目录中 `app.asar` 里的 **`lib/main.js`** 一处：让 `welcomeBackend` 保留 **shell 侧 Host session** 的原生 RPC 路径，不再用渲染进程的 cookie 去冒充 Host 身份。

- 目标版本：**仅 0.2.2-rc.1**（脚本内置 sha256 校验，版本不符直接拒绝执行）
- 许可证：[MIT](LICENSE)
- 作者：yanamianna0501
- 状态：非官方补丁，仅供自用

---

## 一、问题背景

DSH 桌面端启动时，shell（Electron 主进程）里的 `lib/main.js` 做了两件看起来相关、实际上互相矛盾的事：

1. **shell 侧**通过 `authenticateWebHost(ready.url)` 与本地 Desktop Host 完成一次握手：Host 返回 `303`，`set-cookie` 里带着 Host 签发的会话 cookie。shell 把它保存在主进程变量 `hostCookie` 里——**这份凭证由 shell 独占，不进入 Chromium 的 cookie jar**。
2. **渲染侧**在构造 `welcomeBackend` 时，却把「请求发送器」和「cookie 提供者」都指向了 Chromium 默认 session：

```js
// 原始实现（lib/main.js，被本补丁替换的那一行）
welcomeBackend = await connectDesktopWelcome(
    ready.url,
    (input, init) => net.fetch(input, init),
    async () => (await session.defaultSession.cookies.get({ url: ready.url }))
        .map((cookie) => `${cookie.name}=${cookie.value}`).join("; ")
);
```

问题就出在**渲染进程 cookie 与 shell 侧 Host session 不一致**：

- `session.defaultSession.cookies.get()` 在该 URL 下返回的是 Chromium 默认 session 里堆积的 **71 个 cookie**，拼出来的 `Cookie` 头合计 **16186 字节**，而 Host 侧 Node HTTP 的 `httpMaxHeaderSize` 只有 **16384** 字节——留给其它请求头的余量已经被吃光。
- 实测探针（`original_probe_v2.json`）在未打补丁的版本上打断点抓到的认证响应状态是 **431**（Request Header Fields Too Large），`connectDesktopWelcome` 随即抛出 `desktop welcome: Web authentication failed`。
- Host 侧对**匿名请求**和**伪造 cookie 的请求**一律返回 **401**（`integration_result_v2.json`：`anonymousStatus: 401`、`forgedCookieStatus: 401`），说明真正有效的凭证只有 shell 手上那份原生 Host session cookie（单条 **226 字节**）。

也就是说：欢迎后端（welcome backend）那条 RPC 通道根本没有拿到 Host 认可的会话身份——症状表现为本地 Web 认证 401 / 启动即失败。根本原因是**认证凭证被从 shell 侧换成了渲染侧的 cookie jar**。

此外，原始实现把 `ready.url`（可能带 launch token）直接当作 welcome 入口 URL，入口不干净。

## 二、v2 的修复方案

**原则：不动 RPC 协议，只改「用哪份凭证、从哪儿发请求」。** `connectDesktopWelcome` 的原生调用路径保持不变——它仍然是：

1. 用带凭证的请求 GET 一次 welcome 入口完成认证；
2. 之后所有调用走 `POST /api/{namespace}/{method}` 的标准 RPC 报文（`type: client-request` / `server-response` + `rpcId` 配对）。

v2 只替换了传入 `connectDesktopWelcome` 的那两段：

```js
// 补丁后（lib/main.js）
// Local hotfix v2: keep native welcome RPC on the shell-owned Host session.
const welcomeCookie = hostCookie;
const welcomeOrigin = new URL(ready.url).origin;
welcomeBackend = await connectDesktopWelcome(`${welcomeOrigin}/`, (input, init) => {
    if (new URL(input).origin !== welcomeOrigin) throw new Error("desktop welcome: cross-origin Host request blocked");
    const headers = new Headers(init?.headers);
    headers.set("cookie", welcomeCookie);
    return fetch(input, { ...init, headers, redirect: "error" });
}, async () => welcomeCookie);
```

逐点说明：

| 改动 | 原来 | 现在 | 目的 |
| --- | --- | --- | --- |
| 凭证来源 | `session.defaultSession.cookies.get(...)` 抓 71 个 cookie（16186 字节） | `hostCookie`——shell 侧 Host session 的原生单条 cookie（226 字节） | 与 Host 认可的会话身份对齐，避免超长 Cookie 头 |
| 入口 URL | `ready.url` | `${welcomeOrigin}/` | 干净的根 URL，不带 launch token |
| 请求发送 | `net.fetch(input, init)` | `fetch(input, { ...init, headers, redirect: "error" })`，并显式 `headers.set("cookie", welcomeCookie)` | 明确只带 Host cookie；`redirect: "error"` 禁止 30x 把凭证带去别处 |
| 跨源防护 | 无 | `new URL(input).origin !== welcomeOrigin` 直接抛错 | Host cookie 不会发往其它源 |
| cookie 提供者 | 渲染侧 cookie jar | 同一个 `welcomeCookie` | 账号 WebSocket 回调拿到同一份 Host cookie 与 origin |

完整改动见 [`web_auth_v2.patch`](web_auth_v2.patch)（`lib/main.js` 的 unified diff，作用域 `lib/main.js.before` → `lib/main.js.web-auth_v2`）。

## 三、适用版本

**只适用于 DSH 0.2.2-rc.1。** 脚本有两道闸：

1. 读取 `app.asar` 内 `package.json`，`version != "0.2.2-rc.1"` → 抛 `This hotfix is restricted to DSH 0.2.2-rc.1`；
2. 校验 `lib/main.js` 的 sha256 必须等于 `f42ed25b0b844a63e515a3aff93dee89b781dae7fcbbbbfc93e04d95ad399ec1`，不匹配 → 抛 `Installed main.js differs from the diagnosed build; refusing to overwrite it`。

**任何一道不通过都会拒绝执行，不会写入任何文件。** 换版本请勿强行改 sha256。

## 四、文件清单

| 文件 | 作用 |
| --- | --- |
| `repair_web_auth_v2.py` | 主脚本。解析并改写 `app.asar` 内的 `lib/main.js`，支持 `--install` / `--restore` / `--asar`；自带版本与 sha256 校验，安装前自动备份原 `app.asar` |
| `web_auth_v2.patch` | 本次改动的 unified diff（`lib/main.js.before` → `lib/main.js.web-auth_v2`），供人工审阅 |
| `repair_result_v2.json` | 一次「准备 + 安装」的最终报告：目标路径、各级 sha256、字节数、条目校验数、变更条目、备份路径、安装方式 |
| `original_probe_v2.json` | 对**未打补丁**版本做的断点探针记录：认证响应状态、cookie 元数据（数量与字节数）、捕获的进程输出字节数——只记录计数，不含任何 cookie 值 |
| `regression_tests_v2.json` | Node 环境下的回归测试结果（6 项全过）与测试局限说明（使用 mock Host，未做真实账号登录/退出） |
| `integration_result_v2.json` | 真实 Electron Host 上的集成测试结果：认证/匿名/伪造 cookie 的状态码、设置与 locale 读取、账号状态、API Key 存在性、analytics RPC、账号订阅、WebSocket 侧窗口状态 |
| `io_tests_v2.json` | 安装与还原的 I/O 路径测试（4 项全过）：原子替换、校验过的就地拷贝、中断还原重试、版本不符拒绝 |
| `.gitignore` | 排除分发 zip、`__pycache__`、`.pyc` 以及脚本运行时生成的 `work/` 中间产物 |
| `LICENSE` | MIT 许可证全文 |
| `README.md` | 本文件 |

> `DSH_Web认证修复_v2.zip` 是打包好的分发件（内容与目录内文件重复），已由 `.gitignore` 排除，**不进入仓库**；磁盘上保留。

## 五、使用方法

需要 **Python 3.8+**（仅用标准库，无第三方依赖），全程离线，不联网。

### 1. 安装补丁

```powershell
# 关闭 DSH 桌面端后执行
python repair_web_auth_v2.py --install
```

脚本会依次做：校验版本与 `lib/main.js` 哈希 → 生成补丁后的 `app.asar` → 备份原文件 → 原子替换 → 校验安装结果。

### 2. 还原

```powershell
python repair_web_auth_v2.py --restore
```

还原会使用 `repair_result_v2.json` 中记录的备份路径与哈希；**备份文件不会被删除**，且重复执行还原是安全的（已还原时会提示并直接返回）。

### 3. 自定义 `app.asar` 路径

脚本内置的默认路径是本机的 DSH 安装位置；装在别处或存在多份安装时，用 `--asar` 覆盖：

```powershell
python repair_web_auth_v2.py --asar "D:\Program Files\DeepSeek Harness\resources\app.asar" --install
python repair_web_auth_v2.py --asar "D:\Program Files\DeepSeek Harness\resources\app.asar" --restore
```

### 4. 只做准备、不安装

```powershell
python repair_web_auth_v2.py
```

不带 `--install` 时只做「准备」：生成补丁后的 `app.asar` 与原始/补丁后的 `lib/main.js` 到 `work/`，刷新 `web_auth_v2.patch` 与 `repair_result_v2.json`，并打印报告；**不会改动已安装的 `app.asar`**。

### 参数说明

| 参数 | 说明 |
| --- | --- |
| `--asar PATH` | 目标 `app.asar` 路径（默认：本机 DSH 安装位置） |
| `--install` | 执行安装（与 `--restore` 互斥） |
| `--restore` | 执行还原（与 `--install` 互斥） |
| `-h, --help` | 查看帮助 |

> 安装或还原前请先**完全退出 DeepSeek Harness**（脚本会检测 `DeepSeek Harness.exe` 进程仍在运行并直接报错退出）。

## 六、安全性说明

- **只改一处**：补丁只替换 `lib/main.js` 中的唯一一处 welcome 认证调用；`changed_entries` 记录为 `["lib/main.js"]`。
- **全量校验**：改写后脚本会逐条遍历 `app.asar` 内的 **22102** 个条目，除 `lib/main.js` 外要求元数据与内容**逐字节一致**，否则安装前就中止。
- **先备份再动手**：安装前把原始 `app.asar` 复制到 `<备份目录>\web-auth_20261107_v2\app.asar`，并对备份做 sha256 校验；若该位置已存在内容不同的备份，脚本拒绝覆盖。
- **哈希闭环**：原 `app.asar`、原 `lib/main.js`、补丁后 `app.asar`、补丁后 `lib/main.js`、磁盘上的当前文件、备份文件，全部做 sha256 校验；安装采用原子替换（`os.replace`），失败时回退为「校验过的就地拷贝」，再失败则用备份自动回滚。
- **可逆**：一条命令即可还原：

  ```powershell
  python repair_web_auth_v2.py --restore
  ```

- **不联网、不收集任何凭证**：脚本只读写本地文件，不发送任何数据；本仓库内所有 JSON 证据都只有状态码、计数、字节数和哈希，**不含任何 cookie、token、密钥或用户名**。

## 七、证据文件说明

四个 JSON 分别是四组独立记录，都只有技术数据（状态码、计数、字节数、哈希），不含任何真实凭证：

- **`repair_result_v2.json`** — 一次完整「准备 + 安装」的结果报告：补丁版本、应用版本、目标路径、原始/补丁后 `app.asar` 的字节数与 sha256、原始/补丁后 `lib/main.js` 的 sha256、校验过的条目数（22102）、变更条目列表、备份路径、是否已安装、替换方式。
- **`original_probe_v2.json`** — 在**未修改**的桌面端上打断点做的探针：探测断点行号、认证响应的状态与 `ok`/`redirected` 标志、cookie 元数据（cookie 数量 71、Cookie 头字节数 16186、单条最大 cookie 字节数 226）、捕获到的进程输出字节数。用于证明「渲染侧 cookie 数量与体积异常」这一前提。
- **`regression_tests_v2.json`** — Node 运行时下的回归测试：6 项全过、0 失败，逐项列出验证内容（无 cookie 的独立传输可复现原始失败；真实 welcome backend 的认证根路径、设置/账号状态、analytics 与 RPC 报文保持不变；账号 WebSocket 收到相同的 Host cookie 与 origin；启动使用不带 launch token 的干净根 URL 并返回 200；跨源目标与重定向零请求零 cookie；AbortSignal 与超时行为保持不变）。同时如实记录测试局限：使用 mock HTTP Host，未做真实账号登录/登出与设置写入。
- **`integration_result_v2.json`** — 在**真实 Electron Host** 上的集成测试：`passed: true`，含认证请求 200 / 匿名请求 401 / 伪造 cookie 401 的对照、原生 Cookie 头字节数 226、HTTP 最大请求头 16384、设置与 locale 可读、账号状态可读、API Key 存在、analytics RPC 可读、账号订阅状态，以及渲染窗口的 scheme/path/readyState 与「无认证失败标记」的检查。
- **`io_tests_v2.json`** — 安装与还原流程的 4 项 I/O 路径测试（原子替换、校验过的就地拷贝、中断还原重试、版本不符拒绝），全部通过；测试仅使用隔离的合成夹具，**没有停止或还原过真实 DSH**。

## 八、风险提示与免责声明

- **本项目是非官方补丁**，与 DeepSeek / DSH 官方无关，仅供个人自用与研究参考。
- 补丁会**修改 DSH 安装目录下的 `app.asar`**。虽然脚本有备份、哈希校验与还原命令，但仍请自行评估风险，**重要环境请先完整备份安装目录**。
- 脚本会拒绝在 DSH 进程仍运行时操作；请务必先完全退出客户端。
- **版本不符会直接拒绝执行**——这意味着你只能把它用在 0.2.2-rc.1 上。
- 官方后续版本若已修复该问题，请优先升级官方版本并删除本补丁。
- 使用本仓库内容造成的一切后果由使用者自行承担，作者不承担任何责任。

## 九、License

[MIT](LICENSE) — Copyright (c) 2026 yanamianna0501
