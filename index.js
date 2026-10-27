// x-media-finder 入口：注册工具 + 调 Python worker 搜索 + 发合并转发卡片。
//
// ── 为什么搜索在子进程里 ──────────────────────────────────────────────────
// twscrape 是 Python 库（带账号池、限频、登录态），重写没有意义。
// 这里沿用本项目 jm-suomingor 的做法：spawn 一个 Python 进程，
// stdin 传 JSON 任务、stdout 逐行收 JSON 进度与结果（见 pdf.js / jm_worker.py）。
// 好处：worker 崩了不会拖垮机器人主进程；坏处：多一跳 IPC，日志要看两处。
//
// ── 发送路径 ──────────────────────────────────────────────────────────────
// 群聊：ctx.onebot.call('send_group_forward_msg', { group_id, messages })
// 私聊：ctx.onebot.call('send_private_msg', { user_id, message: [段数组] })
// 一律走 ctx.onebot.call —— 与核心共用同一套 HTTP/令牌/超时逻辑，
// 不自己 fetch，避免绕开令牌或超时配置。

import fs from 'node:fs';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

import {
  NODE_SYNTAX,
  buildForwardCard,
  groupByMedia,
  mediaCountOf,
  isSendableImageRef,
  isSendableVideoRef,
  renderTemplate,
  DEFAULT_HEADER_TEMPLATE,
  toFileUri
} from './sender.js';
import { SeenStore } from './dedupe.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP_ROOT = path.resolve(HERE, '..', '..');
const WORKER_PATH = path.join(HERE, 'x_worker.py');

// 站点抓取 worker（cosplaytele 等）。与 x_worker 分开：
// 那边打 X 的 API，这边是 Playwright 驱动浏览器，混在一起难维护。
const GRAB_PATH = path.join(HERE, 'grab_worker.py');
const GRAB_TIMEOUT_MS = 30 * 60 * 1000;   // 大视频下载+压缩可能很久
// 搜索类任务（含抓封面）单独设上限：抓封面每条约 6-12 秒，
// 不能让它跑到几十分钟把整轮对话拖死（实测出现过"调用不返回、表现为没回复"）。
const GRAB_SEARCH_TIMEOUT_MS = 240 * 1000;   // 4 分钟

// 技能 id 从清单读，不硬编码 —— 目录改名/复制一份就能并存两个实例
const SKILL_ID = (() => {
  try {
    const raw = fs.readFileSync(path.join(HERE, 'skill.json'), 'utf8').replace(/^\uFEFF/, '');
    return String(JSON.parse(raw).id || 'x-media-finder');
  } catch {
    return 'x-media-finder';
  }
})();
const toolId = (name) => `${SKILL_ID}__${name}`;

// ── 配置 ────────────────────────────────────────────────────────────────────

let cfg = () => ({});
let logFns = { log: console.log, warn: console.warn, error: console.error };

function num(v, dflt, lo, hi) {
  const n = Number(v);
  if (!Number.isFinite(n)) return dflt;
  return Math.min(hi, Math.max(lo, n));
}

function csv(v) {
  return String(v ?? '')
    .split(/[,，\s]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/**
 * tag 勾选框 → tag 值列表。
 *
 * 为什么做成"每 tag 一个布尔项"而不是一个多选控件：
 * 这台机器人的技能设置界面只支持 boolean / number / enum / string 四种
 * （见 ui/app/06-settings-render.js 的 fieldHtml），没有多选控件。
 * 用一组勾选框既满足"可选"，也不会踩到 UI 限制。
 *
 * 映射表写死在这里而不是从 schema 里读，是为了让"哪个布尔项对应哪个 tag"
 * 一目了然，排障时不用两处对着看。新增 tag 时两边都要改（集成测试会校验）。
 */
export const TAG_FIELDS = [
  ['tagNsfw', 'nsfw'],
  ['tagR18', 'r18'],
  ['tagEro', 'ero'],
  ['tagWata', 'wata'],
  ['tagUra', '裏垢'],
  ['tagJisori', '自撮り'],
  ['tagSelfie', 'selfie'],
  ['tagEcchi', 'えっち'],
  ['tagNude', 'naked'],
  ['tagLewd', 'lewd'],
  // 中文高频成人 tag
  ['tagZiwei', '自慰'],
  ['tagLuchu', '露出'],
  ['tagBaisi', '白丝'],
  ['tagHeisi', '黑丝'],
  ['tagFuli', '福利'],
  ['tagSifang', '私房'],
  ['tagSheQing', '涩情'],
  ['tagTiaoDan', '跳蛋'],
  ['tagJuRu', '巨乳'],
  ['tagNaiZi', '奶子'],
  ['tagZaXie', '榨精'],
  ['tagHuLi', '狐狸']
];

/**
 * 「一键启用中文成人 tag」预置组。
 * 与 TAG_FIELDS 里的条目对应，勾选组开关等于批量勾选这些 tag。
 */
export const CHINESE_ADULT_TAGS = [
  '自慰', '露出', '白丝', '黑丝', '福利', '私房', '涩情', '巨乳'
];

/**
 * 汇总最终参与搜索的 tag 列表。
 *
 * 优先级：
 *   1. 勾选框选中的（主入口，界面上可点）
 *   2. extraTags 文本框里手填的（给"想加个冷门 tag 但不想改代码"留的口子）
 * 两者合并去重。这样既有点选，也不失去灵活性 ——
 * 只有勾选框的话，用户遇到没列出的 tag 就得等改代码。
 *
 * 去重按小写比较（#NSFW 与 #nsfw 视为同一个），
 * 但保留用户原始写法（#wata 的 # 会被 worker 保留）。
 */
function collectTags(c) {
  const out = [];
  const seen = new Set();
  const push = (v) => {
    const t = String(v ?? '').trim().replace(/^#+/, '');
    if (!t) return;
    const key = t.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    out.push(t);
  };
  for (const [field, tag] of TAG_FIELDS) {
    if (c[field] === true) push(tag);
  }
  // 一键启用中文成人 tag 组
  if (c.tagChineseAdult === true) {
    for (const t of CHINESE_ADULT_TAGS) push(t);
  }
  for (const t of csv(c.extraTags)) push(t);
  return out;
}

/**
 * 解析「群号:模式」配置串。
 *
 * 输入："123456789:file, 987654321:link , private:file"
 * 输出：{ "123456789": "file", "987654321": "link", "private": "file" }
 *
 * 容错：大小写、中英文冒号、多余空格都能处理；非法项静默跳过
 * （配置写错了不该让整个技能崩掉，顶多那个群用默认值）。
 */
function parseModeMap(raw) {
  const out = {};
  const str = String(raw || '').trim();
  if (!str) return out;
  for (const part of str.split(/[,，;；\n]+/)) {
    const item = part.trim();
    if (!item) continue;
    // 支持 123:file / 123：file / private:file
    const m = item.match(/^(private|\d+)\s*[:：]\s*(link|file)$/i);
    if (!m) continue;
    out[m[1].toLowerCase()] = m[2].toLowerCase();
  }
  return out;
}

/**
 * 判断某个会话应该用哪种视频发送方式。
 *
 * 优先级：
 *   1. videoModeByGroup 里精确匹配的群号 / 'private'
 *   2. videoSendMode 默认值
 *
 * @returns {{mode:'link'|'file', reason:string}}
 */
function resolveVideoMode(ctx) {
  const s = settings();
  const [kind, id] = String(ctx.chatKey).split(':');
  const key = kind === 'private' ? 'private' : String(id);

  const map = s.videoModeByGroup || {};
  if (map[key]) {
    return { mode: map[key], reason: `按群配置（${key}）` };
  }
  return { mode: s.videoSendMode, reason: '默认配置' };
}

/** 把原始设置规整成内部用的形状（每个字段都夹到合法区间）。 */
function settings() {
  const c = cfg() || {};
  const depth = Number(c.nestDepth) === 3 ? 3 : 2;
  return {
    maxResults: num(c.maxResults, 12, 1, 40),
    mediaPerCard: num(c.mediaPerCard, 2, 1, 6),
    groupForward: c.groupForward !== false,
    // 私聊是否也合并成卡片：默认 true —— 与群聊同一套浏览体验。
    privateForward: c.privateForward !== false,
    nestDepth: depth,
    nodeSyntax: String(c.nodeSyntax || NODE_SYNTAX.TYPED).toLowerCase() === NODE_SYNTAX.BARE
      ? NODE_SYNTAX.BARE
      : NODE_SYNTAX.TYPED,
    expandTags: c.expandTags !== false,
    extraTags: collectTags(c),
    // 多个 tag 的组合方式：any（每个单独搜）/ or（一轮 OR）/ and（必须都有）
    tagMode: ['any', 'or', 'and'].includes(String(c.tagMode || '').toLowerCase())
      ? String(c.tagMode).toLowerCase()
      : 'any',
    denyKeywords: csv(c.denyKeywords).map((s) => s.toLowerCase()),
    minLikes: num(c.minLikes, 0, 0, 100000),
    // 媒体类型：image / video / mix（默认一半一半）
    mediaType: ['image', 'video', 'mix'].includes(String(c.mediaType || '').toLowerCase())
      ? String(c.mediaType).toLowerCase()
      : 'mix',
    // true = 所有结果塞进一张卡片（用 depth3 的内层节点承载）；
    // false = 按 mediaPerCard 拆多张
    singleCard: c.singleCard !== false,
    singleCardMax: num(c.singleCardMax, 20, 1, 40),
    // 跨次去重：同一会话不重发同一内容
    dedupe: c.dedupe !== false,
    dedupeTtlDays: num(c.dedupeTtlDays, 7, 1, 90),
    dedupeMax: num(c.dedupeMax, 3000, 100, 50000),
    dedupeRefill: c.dedupeRefill !== false,
    qualityBoost: c.qualityBoost !== false,
    // X 搜索总开关：默认关。涩涩内容走站点（cosplaytele/游戏巴士/每日大赛/
    // FC2/MissAV/Pornhub），不消耗 X 账号额度。识图找人不受此开关影响。
    xSearchEnabled: c.xSearchEnabled === true,
    // 同会话两次搜索的最小间隔（秒）。0 = 关闭限流。
    // 多人同时发「找涩涩」时不限流会同时起 N 个 worker + N 个 Chromium，
    // 4GB 内存的机器直接 OOM。
    searchCooldownSeconds: num(c.searchCooldownSeconds, 30, 0, 600),
    // 视频发送方式：link = 只发地址，file = 下载文件后发
    videoSendMode: String(c.videoSendMode || 'link').toLowerCase() === 'file' ? 'file' : 'link',
    // 按群覆盖：{ '123456789': 'file', 'private': 'link' }
    videoModeByGroup: parseModeMap(c.videoModeByGroup),
    videoMaxSendMB: num(c.videoMaxSendMB, 150, 10, 2000),
    // 检索模式：auto（自动判断是不是在问某个博主）/ search / author
    searchMode: ['auto','search','author'].includes(String(c.searchMode||'').toLowerCase())
      ? String(c.searchMode).toLowerCase()
      : 'auto',
    // 同一作者在一次结果里最多几条（0 = 不限）
    maxPerAuthor: num(c.maxPerAuthor, 2, 0, 20),
    // 只保留成人向内容（硬过滤，不是排序）
    adultOnly: c.adultOnly === true,
    // 自定义屏蔽/偏好（逗号分隔）。AI 内容作为默认预置。
    blockTags: csv(c.blockTags),
    blockWords: csv(c.blockWords),
    preferTags: csv(c.preferTags),
    preferWords: csv(c.preferWords),
    blockAi: c.blockAi === true,
    // 相关性过滤：滤掉与关键词无关的内容（默认开）
    relevanceFilter: c.relevanceFilter !== false,
    // 只要硬核 18+（默认关：擦边内容通常也要）
    adultStrict: c.adultStrict === true,
    // 站点抓取开关与体积上限
    siteGrabEnabled: c.siteGrabEnabled !== false,
    siteMaxSendMB: num(c.siteMaxSendMB, 150, 20, 500),
    siteCoverMax: num(c.siteCoverMax, 6, 0, 20),
    magnetMaxMinutes: num(c.magnetMaxMinutes, 25, 5, 180),
    // 视频复用缓存
    videoCacheDays: num(c.videoCacheDays, 14, 1, 90),
    videoCacheMaxGB: num(c.videoCacheMaxGB, 8, 1, 100),
    // 地区偏好（按文字系统判定，不是按人种）：asia/japan/china/western/any
    region: ['asia','japan','china','western','any'].includes(String(c.region || ''))
      ? String(c.region)
      : 'any',
    // 模型二次识别：先返回候选清单，模型挑完再发
    modelSecondPass: c.modelSecondPass === true,
    groupAllowlist: csv(c.groupAllowlist),
    showAuthor: c.showAuthor !== false,
    inlineImages: c.inlineImages !== false,
    // 卡片首条文案模板；空串 = 用 sender.js 里的 DEFAULT_HEADER_TEMPLATE
    headerTemplate: String(c.headerTemplate ?? ''),
    pythonPath: String(c.pythonPath ?? '').trim(),
    accountsDb: String(c.accountsDb ?? '').trim(),
    proxy: String(c.proxy ?? '').trim(),
    timeoutMs: num(c.searchTimeoutSeconds, 150, 10, 900) * 1000,
    retries: num(c.retries, 3, 1, 8),
    backoffSeconds: num(c.backoffSeconds, 8, 1, 120)
  };
}

/** 账号库默认位置：resources/app/data/<技能 id>/accounts.db。 */
function defaultAccountsDb() {
  return path.join(APP_ROOT, 'data', SKILL_ID, 'accounts.db');
}

function accountsDbPath() {
  return settings().accountsDb || defaultAccountsDb();
}

// ── Python 解释器探测 ───────────────────────────────────────────────────────
//
// 顺序：配置路径 → py 启动器 → PATH 里的 python/python3 → 常见安装位置。
// 与 jm-suomingor/pdf.js 同一套策略，行为保持一致（用户换技能不用重新理解）。

const EXTRA_CANDIDATES = [
  'C:\\Python313\\python.exe',
  'C:\\Python312\\python.exe',
  'C:\\Python311\\python.exe',
  `${process.env.LOCALAPPDATA || ''}\\Programs\\Python\\Python313\\python.exe`,
  `${process.env.LOCALAPPDATA || ''}\\Programs\\Python\\Python312\\python.exe`,
  `${process.env.LOCALAPPDATA || ''}\\Programs\\Python\\Python311\\python.exe`,
  '/usr/bin/python3',
  '/usr/local/bin/python3'
].filter((p) => p && !p.startsWith('\\') && !p.startsWith('/Program'));

let cachedPython = null;

/** 跑一次 `python -c "pass"` 确认这个解释器真的能执行。 */
function probe(pythonExe, args = []) {
  try {
    const r = spawnSync(pythonExe, [...args, '-c', 'import sys;print(sys.version_info[0])'], {
      encoding: 'utf8',
      timeout: 15000,
      windowsHide: true
    });
    if (r.status === 0) return true;
  } catch { /* 探测失败继续下一个候选 */ }
  return false;
}

/**
 * 找可用的 Python。
 * @returns {{exe: string, args: string[], configuredMissing: boolean}|null}
 */
function resolvePython() {
  const want = settings().pythonPath;
  if (want) {
    if (probe(want)) return { exe: want, args: [], configuredMissing: false };
    // 配置了路径但不可用：继续往下探测，但返回值里标出来，
    // 否则会出现"设置里填了错路径、程序悄悄用了另一个解释器"这种怪事。
    logFns.warn(`配置的 Python 不可用：${want}，尝试自动探测`);
  }
  if (process.platform === 'win32' && probe('py', ['-3'])) {
    return { exe: 'py', args: ['-3'], configuredMissing: Boolean(want) };
  }
  for (const exe of ['python', 'python3', ...EXTRA_CANDIDATES]) {
    if (probe(exe)) return { exe, args: [], configuredMissing: Boolean(want) };
  }
  return null;
}

function pythonOrWarn() {
  if (cachedPython === undefined) cachedPython = null;
  if (cachedPython) return cachedPython;
  const found = resolvePython();
  cachedPython = found;
  return found;
}

// ── 调 worker ───────────────────────────────────────────────────────────────

/**
 * 跑 x_worker.py，逐行解析 JSON。最后一个 {"t":"result"} 行作为返回值。
 *
 * @param {object} task 传给 worker 的任务对象
 * @param {(ev: object) => void} [onProgress] 每收到一行进度就回调
 * @returns {Promise<object>} result 行（ok=false 时也 resolve，由调用方判断）
 */
function runWorker(task, onProgress) {
  return new Promise((resolve) => {
    const py = pythonOrWarn();
    if (!py) {
      resolve({
        ok: false,
        code: 'NO_PYTHON',
        error: '找不到可用的 Python。请在技能设置里填「Python 解释器路径」，或装 Python 3.10+ 并加入 PATH。'
      });
      return;
    }

    const child = spawn(py.exe, [...py.args, WORKER_PATH], {
      cwd: HERE,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' }
    });

    let settled = false;
    let result = null;
    let stderrBuf = '';
    let stdoutBuf = '';

    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { child.kill(); } catch { /* 已退出 */ }
      resolve(value);
    };

    const timer = setTimeout(() => {
      finish({
        ok: false,
        code: 'TIMEOUT',
        error: `搜索超时（${Math.round(task.__timeoutMs / 1000)}s）。twscrape 首次运行、限频退避都可能很慢，可在设置里调大「单次搜索超时」。`
      });
    }, Math.max(10000, task.__timeoutMs || 150000));

    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      stdoutBuf += chunk;
      // 按行切；最后一段可能不完整，留在缓冲里等下一块
      let idx;
      while ((idx = stdoutBuf.indexOf('\n')) >= 0) {
        const line = stdoutBuf.slice(0, idx).trim();
        stdoutBuf = stdoutBuf.slice(idx + 1);
        if (!line) continue;
        let ev;
        try {
          ev = JSON.parse(line);
        } catch {
          // 非 JSON 行（比如第三方库的 print）直接当日志，不要让它毁掉解析
          logFns.log('[worker]', line);
          continue;
        }
        if (ev.t === 'result') result = ev;
        else if (onProgress) {
          try { onProgress(ev); } catch { /* 进度回调失败不影响主流程 */ }
        }
      }
    });

    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (chunk) => {
      stderrBuf += chunk;
      if (stderrBuf.length > 8000) stderrBuf = stderrBuf.slice(-8000);
    });

    child.on('error', (err) => {
      finish({ ok: false, code: 'SPAWN_FAILED', error: `启动 Python 失败：${err?.message ?? err}` });
    });

    child.on('close', (code) => {
      if (settled) return;
      if (result) {
        finish(result);
        return;
      }
      // 没有 result 行就退出了：把 stderr 尾巴交出去，这是唯一能定位问题的线索
      const tail = stderrBuf.trim().split('\n').slice(-8).join('\n');
      finish({
        ok: false,
        code: 'NO_RESULT',
        error: `worker 退出（code=${code}）但没返回结果。${tail ? `\n${tail}` : ''}`
      });
    });

    // 把超时时间也塞进任务里（worker 自己不用，纯给上面 finish 用）
    const payload = { ...task };
    delete payload.__timeoutMs;
    try {
      child.stdin.write(JSON.stringify(payload), 'utf8');
      child.stdin.end();
    } catch (err) {
      finish({ ok: false, code: 'STDIN_FAILED', error: `写入 worker 失败：${err?.message ?? err}` });
    }
  });
}

/** 一次性的环境自检（不联网）。 */
/**
 * 跑站点抓取 worker，收集 JSON 行事件。
 * 与 runWorker 的区别：超时更长（下载+压缩大视频），且允许中途进度回调。
 */
function runGrab(task, onProgress) {
  return new Promise((resolve) => {
    // 站点抓取总开关（配置里能关掉，省资源）
    if (settings().siteGrabEnabled === false) {
      resolve({ ok: false, code: 'DISABLED', error: '站点抓取已在技能设置里关闭' });
      return;
    }
    const py = pythonOrWarn();
    if (!py) {
      resolve({ ok: false, code: 'NO_PYTHON', error: '找不到可用的 Python' });
      return;
    }
    let err = '';
    const child = spawn(py.exe, [...py.args, GRAB_PATH], {
      cwd: HERE,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' }
    });
    const timeoutMs = Number(task.__timeoutMs) > 0 ? Number(task.__timeoutMs) : GRAB_TIMEOUT_MS;
    const timer = setTimeout(() => {
      try { child.kill(); } catch { /* ignore */ }
      resolve({ ok: false, code: 'TIMEOUT', error: '抓取超时（可减少 limit 或关掉封面试试）' });
    }, timeoutMs);

    // ⚠️ 不要在 'data' 里边读边丢已经解析过的行：
    // Windows 上 'close' 可能早于最后一次 stdout 数据到达（管道尚未 flush），
    // 早期版本在 close 里只看 buf 剩余部分，导致 result 行被漏掉、
    // 表现为"明明搜到了却返回没搜到"。这里改成**累积完整输出**，
    // 等 'close' 后统一扫描，慢一点但不会丢。
    let all = '';
    child.stdout.setEncoding('utf-8');
    child.stdout.on('data', (d) => {
      all += d;
      // 进度事件可以即时回调（顺带解析，失败了也不影响最终结果）
      for (const line of String(d).split('\n')) {
        const t = line.trim();
        if (!t) continue;
        try {
          const ev = JSON.parse(t);
          if (ev.t === 'progress') onProgress?.(ev);
        } catch { /* 半行，忽略 */ }
      }
    });
    child.stderr.on('data', (d) => { err += d; });
    child.on('error', (e) => {
      clearTimeout(timer);
      resolve({ ok: false, code: 'SPAWN_FAILED', error: `启动失败：${e.message}` });
    });
    child.on('close', () => {
      clearTimeout(timer);
      let res = null;
      for (const l of all.split('\n')) {
        const t = l.trim();
        if (!t) continue;
        try {
          const ev = JSON.parse(t);
          if (ev.t === 'result') res = ev;
        } catch { /* 忽略非 JSON 行 */ }
      }
      if (!res) {
        resolve({ ok: false, code: 'NO_RESULT', error: (err || all || '无输出').slice(-300) });
        return;
      }
      resolve(res);
    });
    child.stdin.write(JSON.stringify(task));
    child.stdin.end();
  });
}

/**
 * 「正在找货」提示语。
 *
 * 为什么要单独抽出来：原来只有 searchAndSend（推特）里内联发这一句，
 * 后来新增的 find_video / album_search / site_search 三条路径**都漏了**，
 * 表现为"私聊搜索和每日大赛搜索没有提示，干等"。
 * 收成一个函数，新增搜索入口只要调它，就不会再漏。
 *
 * 失败不影响主流程（提示语发不出去不该导致搜索失败）。
 */
async function sendSearchHint(ctx, extra) {
  const text = '🔍 小鲸鱼正在全网找货，请稍候...' + (extra ? `\n${extra}` : '');
  try {
    await sendText(ctx, text);
    return true;
  } catch (err) {
    logFns.warn?.('发送搜索提示语失败：', err?.message ?? err);
    return false;
  }
}

async function runWorkerCheck() {
  return new Promise((resolve) => {
    const py = pythonOrWarn();
    if (!py) {
      resolve({ ok: false, error: '找不到可用的 Python（见技能设置「Python 解释器路径」）' });
      return;
    }
    let out = '';
    let err = '';
    const child = spawn(py.exe, [...py.args, WORKER_PATH, '--check', accountsDbPath()], {
      cwd: HERE,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' }
    });
    const timer = setTimeout(() => {
      try { child.kill(); } catch { /* ignore */ }
      resolve({ ok: false, error: '自检超时（20s）' });
    }, 20000);
    child.stdout.on('data', (d) => { out += d; });
    child.stderr.on('data', (d) => { err += d; });
    child.on('close', () => {
      clearTimeout(timer);
      const line = out.trim().split('\n').filter(Boolean).pop() || '';
      try {
        resolve(JSON.parse(line));
      } catch {
        resolve({ ok: false, error: `自检输出无法解析：${(line || err).slice(0, 400)}` });
      }
    });
    child.on('error', (e) => {
      clearTimeout(timer);
      resolve({ ok: false, error: `启动失败：${e?.message ?? e}` });
    });
  });
}

// ── 环境预检（带缓存）──────────────────────────────────────────────────────
//
// 为什么需要它：搜索是慢操作，"🔍 正在找货"必须立刻发出去缓和等待感。
// 但如果环境根本没配好（没装 twscrape / 账号库空），先喊"正在找货"、
// 两秒后再说"还没配好"，是最糟的体验 —— 群友白等一场。
//
// 所以判断必须在提示语**之前**做。而完整判断要问 twscrape（账号数），
// 就得起 Python 进程（约 0.5~1s）。为了不把这 1s 加到每次请求上，
// 结果缓存一段时间：首次付一次代价，之后白拿。
//
// 缓存 TTL 取 2 分钟：够挡住连续刷关键词时的重复探测，
// 又能在管理员补好账号后很快自愈。

const PREFLIGHT_TTL_MS = 120000;
let preflightCache = { at: 0, ok: false, reason: '', key: '' };

/**
 * 判断环境是否就绪。返回 {ok, reason}。
 *
 * @param {string} dbPath 当前账号库路径（路径变了要重新探测）
 * @param {boolean} force 跳过缓存（自检工具用）
 */
async function preflight(dbPath, force = false) {
  const key = `${dbPath}|${pythonOrWarn()?.exe ?? ''}|${settings().proxy}`;
  const now = Date.now();
  if (!force && preflightCache.key === key && now - preflightCache.at < PREFLIGHT_TTL_MS) {
    return { ok: preflightCache.ok, reason: preflightCache.reason };
  }

  const r = await runWorkerCheck();
  let ok = false;
  let reason = '';
  if (!r.ok) {
    reason = r.error || '自检失败';
  } else if (!r.report || !(Number(r.report.accounts) > 0)) {
    reason = '账号库里没有账号';
  } else {
    ok = true;
  }
  preflightCache = { at: now, ok, reason, key };
  return { ok, reason };
}

/** 环境就绪后清缓存（配置改动、登录完成后立刻生效）。 */
function invalidatePreflight() {
  preflightCache = { at: 0, ok: false, reason: '', key: '' };
}

// ── 关键词安全闸 ────────────────────────────────────────────────────────────
//
// 拦截未成年指向词。这一层在 JS 侧做（而不是只靠模型自觉），
// 因为它是硬底线：不依赖提示词是否被遵守。
//
// 判定方式：关键词按空格/标点切词，任一切片命中黑名单即拒绝。
// 用切片而不是子串匹配，是为了避免"jacket"命中"jk"这种误伤。

function hitsDenyKeyword(keyword, denyList) {
  if (!denyList.length) return null;
  const lower = String(keyword || '').toLowerCase();
  // 先做整词/子串双重判定：
  //   中日文黑名单词（女子高生/萝莉）在中文里没有空格分隔，必须走子串；
  //   拉丁词（jk/jc/loli）走切片，避免误伤 jacket/lolita-legit 之类。
  const tokens = lower.split(/[^a-z0-9\u3040-\u30ff\u4e00-\u9fff]+/).filter(Boolean);
  for (const bad of denyList) {
    if (!bad) continue;
    const isLatin = /^[a-z0-9]+$/.test(bad);
    if (isLatin) {
      if (tokens.includes(bad)) return bad;
    } else if (lower.includes(bad)) {
      return bad;
    }
  }
  return null;
}

// ── 发消息 ──────────────────────────────────────────────────────────────────

/**
 * 发一条普通文本。
 * @returns {Promise<object>} OneBot 返回的 data
 */
async function sendText(ctx, text) {
  const [kind, id] = String(ctx.chatKey).split(':');
  if (kind === 'private') {
    return ctx.onebot.call('send_private_msg', { user_id: Number(id), message: [{ type: 'text', data: { text } }] });
  }
  return ctx.onebot.call('send_group_msg', { group_id: Number(id), message: [{ type: 'text', data: { text } }] });
}

// ── 候选缓存（模型二次识别用）──────────────────────────────────────────────
//
// 流程：search_media(previewOnly=true) 搜完**不发**，把结果暂存这里，
// 并把"编号 + 作者 + 原文 + 标签"以纯文本返回给模型；
// 模型判断哪些相关后调 send_picked(indices="1,3,5") 真正发出。
//
// 为什么需要：模型看不到图片，但能读文字和标签。tag 匹配本身有噪音
// （#cosplay 到处都是），让模型按语义再筛一遍能明显减少"内容不相干"。
//
// 缓存按会话隔离（不同群的候选不能混），10 分钟过期。
const PENDING_TTL_MS = 30 * 60 * 1000;   // 30 分钟：群友看完列表再决定常常超过 10 分钟
const pendingPicks = new Map();   // chatKey -> { items, at, keyword, mediaType }

// 站点候选缓存：find_video / album_search 搜完后把"编号 -> 条目"存这里，
// 群友回一个数字就能直接下那个视频。按会话隔离，10 分钟过期。
// 为什么需要：站点视频下载要十几分钟，让群友先看标题+封面再决定下哪个，
// 比"直接下第一个"靠谱得多。
const pendingSites = new Map();

// ── 搜索快照 ────────────────────────────────────────────────────────────────
//
// 问题：早期每个会话只存"最后一次搜索"的候选。如果群友看到编号后，
// 中间又搜了一次，缓存被覆盖，再说「下 3」下到的就是**新搜索的 3 号**，
// 而不是他刚才看到的那条 —— 这就是"下载到没出现的链接"。
//
// 方案：每次搜索生成一个短 ID（A/B/C…），保留最近 5 个快照。
//   · 「下 3」  → 默认取**最新**快照的第 3 条（保留简单说法）
//   · 「下 A3」 → 明确指定 A 快照第 3 条（要翻旧列表时用）
// 快照在卡片标题里就带着（如「A · 找到 30 条」），群友能看到代号。
const SNAPSHOT_MAX = 5;
const snapshotSeq = new Map();   // chatKey -> next letter index

function nextSnapId(chatKey) {
  const key = String(chatKey);
  const n = snapshotSeq.get(key) || 0;
  snapshotSeq.set(key, n + 1);
  // A..Z 循环，够用（同时最多 5 个快照）
  return String.fromCharCode(65 + (n % 26));
}

// 快照落盘：内存版在"技能重载 / 抓封面超时后重载"时会全丢，
// 表现为"列表过期了，要重搜"。存到 data/site_snapshots.json 里就稳了。
const SNAP_FILE = path.join(HERE, 'data', 'site_snapshots.json');

function loadSnapshots() {
  try {
    if (!fs.existsSync(SNAP_FILE)) return;
    const raw = JSON.parse(fs.readFileSync(SNAP_FILE, 'utf8'));
    const now = Date.now();
    for (const [k, v] of Object.entries(raw || {})) {
      if (!Array.isArray(v)) continue;
      const fresh = v.filter((x) => now - (x.at || 0) <= PENDING_TTL_MS);
      if (fresh.length) pendingSites.set(k, fresh);
    }
  } catch (err) {
    logFns.warn?.('读取候选快照失败：', err?.message ?? err);
  }
}

function saveSnapshots() {
  try {
    const dir = path.dirname(SNAP_FILE);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    const obj = {};
    for (const [k, v] of pendingSites) obj[k] = v;
    const tmp = SNAP_FILE + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(obj), 'utf8');
    fs.renameSync(tmp, SNAP_FILE);
  } catch (err) {
    logFns.warn?.('保存候选快照失败：', err?.message ?? err);
  }
}

// 启动时读一次
loadSnapshots();

function putSiteCandidates(chatKey, payload) {
  const key = String(chatKey);
  // 快照序号也要能从磁盘恢复：取已有最大序号 + 1
  const list = pendingSites.get(key) || [];
  let snapId = payload.snapId;
  if (!snapId) {
    const used = new Set(list.map((x) => String(x.snapId)));
    let n = 0;
    while (used.has(String.fromCharCode(65 + (n % 26)))) n += 1;
    snapId = String.fromCharCode(65 + (n % 26));
  }
  const at = Date.now();
  // 同 snapId 覆盖（同一轮搜索多次写），否则追加
  const filtered = list.filter((x) => x.snapId !== snapId);
  filtered.push({ ...payload, snapId, at });
  // 只留最近 N 个
  filtered.sort((a, b) => b.at - a.at);
  pendingSites.set(key, filtered.slice(0, SNAPSHOT_MAX));
  saveSnapshots();
  return snapId;
}

/**
 * 取快照。
 * @param {string} chatKey
 * @param {string} [which] 快照 ID（如 'A'）；不传 = 最新
 */
function takeSiteCandidates(chatKey, which) {
  const key = String(chatKey);
  // 惰性加载：内存里没有就先去磁盘捞一次。
  // 为什么需要：loadSnapshots() 只在模块加载时跑一次，而技能热重载
  // （实测 21:59 有过一次）会清空内存 → 磁盘上有快照但内存是空的，
  // 表现为"找不到快照（可用：无）"，用户看到的就是"下载失败"。
  if (!pendingSites.has(key)) loadSnapshots();
  const list = pendingSites.get(key);
  if (!Array.isArray(list) || !list.length) return null;
  const now = Date.now();
  const fresh = list.filter((x) => now - x.at <= PENDING_TTL_MS);
  if (!fresh.length) { pendingSites.delete(key); return null; }
  if (fresh.length !== list.length) { pendingSites.set(key, fresh); saveSnapshots(); }
  if (which) {
    return fresh.find((x) => String(x.snapId).toUpperCase() === String(which).toUpperCase()) || null;
  }
  return fresh[0];   // 最新的
}

function putPending(chatKey, payload) {
  pendingPicks.set(String(chatKey), { ...payload, at: Date.now() });
  // 顺手清掉过期项，避免长期占用内存
  const now = Date.now();
  for (const [k, v] of pendingPicks) if (now - v.at > PENDING_TTL_MS) pendingPicks.delete(k);
}

function takePending(chatKey) {
  const key = String(chatKey);
  const v = pendingPicks.get(key);
  if (!v) return null;
  if (Date.now() - v.at > PENDING_TTL_MS) { pendingPicks.delete(key); return null; }
  return v;
}

/**
 * 把一条候选渲染成一行文本，供模型判断相关性。
 * 只给模型看**元数据**（编号/作者/原文/标签/媒体类型），不给图片本身。
 */
function renderCandidate(idx, it) {
  const tags = Array.isArray(it._tags) ? it._tags.slice(0, 8).join(' ') : '';
  const kind = [it.images?.length ? `${it.images.length}图` : '', it.videos?.length ? '视频' : '']
    .filter(Boolean).join('+') || '媒体';
  const text = String(it.text || '').replace(/\s+/g, ' ').slice(0, 90);
  return `#${idx} @${it.author || '?'} [${kind}]${it.sensitive ? ' [敏感]' : ''} ${text}${tags ? ' | ' + tags : ''}`;
}

/**
 * 把图片包进**一层**合并转发卡片发出去。
 *
 * 和 sendVideosAsOneCard 同构：每条图片一个节点 + 一个说明节点。
 * 用户明确要求：所有图片/视频只能出现在卡片里，绝不裸发。
 * 卡片失败就报错，不做任何降级。
 *
 * @param {object} ctx
 * @param {Array<{localPath:string, title?:string, note?:string}>} imgs
 * @returns {Promise<{ok:number, failed:number, viaCard:boolean, error?:string}>}
 */
async function sendImagesAsOneCard(ctx, imgs) {
  const list = (imgs || []).filter((v) => v && v.localPath);
  if (!list.length) return { ok: 0, failed: 0, viaCard: false, error: '没有可发的图片' };

  const nodes = list.map((v, i) => ({
    type: 'node',
    data: {
      user_id: Number(ctx.selfId) || 0,
      nickname: `🖼 图片 ${i + 1}`.slice(0, 40),
      content: [
        { type: 'image', data: { file: toFileUri(v.localPath) } },
        { type: 'text', data: { text: [
          `【${i + 1}】${String(v.title || '').slice(0, 120)}`,
          v.note || ''
        ].filter(Boolean).join('\n') } }
      ]
    }
  }));

  const label = `🖼 ${list.length} 张图片`;
  const sent = await sendForwardWithRetry(ctx, nodes, { source: label, summary: label, prompt: label });
  if (sent.ok) return { ok: list.length, failed: 0, viaCard: true };
  logFns.error?.('图片卡片发送失败，按配置不再降级为裸发：', sent.error);
  return { ok: 0, failed: list.length, viaCard: false, error: sent.error || '卡片发送失败' };
}

// ── 图片通道状态缓存 ────────────────────────────────────────────────────────
//
// 背景（实测 2026-10-23）：
//   · QQ 对**群消息里的图片上传**有独立限流，私聊不受影响；
//   · 失败了会返回 retcode=100 "group message delivery was not confirmed"；
//   · 文字卡片走另一条通道，同一时刻依然能发。
//   · 没有只读 API 能查询这个状态（get_status 只给 online/good），
//     空内容卡片会被协议端拒绝（rc=1400），所以**无法零痕迹探测图片通道**。
//
// 因此这里的做法是：记录最近一次图片发送结果，短期内失败的会话
// 直接提前告知"图片通道受限"，省掉一次 20 秒的搜索 + 一轮必败的重试。
// 这不是探测，是"记账"，所以不会往群里发任何东西。
const imgChannelState = new Map();   // chatKey -> { ok:boolean, at:number, err:string }
const IMG_BLOCK_TTL_MS = 60 * 1000;  // 1 分钟内复用这个判断

function markImgChannel(chatKey, ok, err) {
  imgChannelState.set(String(chatKey), { ok, at: Date.now(), err: String(err || '') });
}

/**
 * 这个会话的图片通道是否"最近刚失败过"。
 * @returns {{blocked:boolean, agoSec:number, err:string}}
 */
function imgChannelBlocked(chatKey) {
  const st = imgChannelState.get(String(chatKey));
  if (!st || st.ok) return { blocked: false, agoSec: 0, err: '' };
  const age = Date.now() - st.at;
  if (age > IMG_BLOCK_TTL_MS) return { blocked: false, agoSec: 0, err: '' };
  return { blocked: true, agoSec: Math.round(age / 1000), err: st.err };
}

/**
 * 自检：判断"现在能不能往这个会话发带图的卡片"。
 *
 * 为什么值得做：一次搜索要 20+ 秒（抓封面），搜完才发现发不出去很浪费。
 * 但限制很硬 —— **不能真往群里发东西**：
 *   · 空内容卡片 → 协议端拒绝（rc=1400）
 *   · 只读 API → 不反映图片上传状态
 * 所以自检只能回答"最近这个会话的图片通道是否刚失败"，
 * 不能凭空探测。这是诚实的边界，不做假装能测的事。
 *
 * @returns {{ok:boolean, reason:string, canProbe:boolean}}
 */
function selfcheckImageChannel(ctx) {
  const st = imgChannelBlocked(ctx.chatKey);
  if (st.blocked) {
    return {
      ok: false,
      canProbe: false,
      reason: `${st.agoSec} 秒前这个会话的图片通道刚失败过（${st.err.slice(0, 80)}），`
        + `建议等 30-60 秒再试。文字能发，只有图片会被拦。`
    };
  }
  return { ok: true, canProbe: false, reason: '最近没有失败记录' };
}

/**
 * 把识图结果渲染成一张一层卡片发出去。
 *
 * 抽成独立函数是因为：识图要跑 60 秒以上，必须放后台
 * （实测 16:41 那次同步等的时候技能热重载，工具调用被中断 → 返回空 →
 *  模型以为成功回了"卡片在上边"，群里却什么都没有）。
 * 后台任务完成后由它负责"发"，模型只负责接一句很短的话。
 *
 * @param {object} ctx
 * @param {object} r worker 返回的 {ok, accounts, posts, summary, mediaIds}
 */
async function renderImageSourceResult(ctx, r) {
  try {
    if (!r || !r.ok) {
      await sendText(ctx, `❌ 识图失败：${String(r?.error || r?.code || '未知原因').slice(0, 120)}。`
        + `这是技术问题（网络/超时），不是图片内容被拒。`);
      return;
    }

    const accts = r.accounts || [];
    const posts = (r.posts || []).filter((p) => p.platform === 'x');

    if (!accts.length && !posts.length) {
      await sendText(ctx, `Lens 跑完了，但没找到任何来源链接。`
        + (r.summary ? `\n它给的图片描述：${String(r.summary).slice(0, 200)}` : '')
        + `\n\n这不是"被拒绝"也不是"涉及违规"，就是单纯没匹配到。`);
      return;
    }

    const nodes = [];
    const postByUser = {};
    for (const p of posts) if (p.user) postByUser[p.user] = p.url;
    const cands = [];
    let ci = 0;

    for (const a of accts) {
      ci += 1;
      const segs = [];
      if (a.avatarLocal && isSendableImageRef(a.avatarLocal)) {
        segs.push({ type: 'image', data: { file: toFileUri(a.avatarLocal) } });
      }
      const kindTag = a.tweetIsVideo ? '🎬 视频' : '🖼 图片';
      const lines = [
        `【${ci}】@${a.user}　${String(a.name || '').slice(0, 34)}　${kindTag}`,
        `${Number(a.followers || 0).toLocaleString('en-US')} 粉丝 · ${Number(a.tweets || 0)} 推文 · 建号 ${a.created || '?'}`,
      ];
      if (a.desc) lines.push(String(a.desc).slice(0, 160));
      const pu = postByUser[a.user];
      if (pu) {
        lines.push(`原帖：${pu}`);
        if (a.tweetDate) lines.push(`发布于 ${a.tweetDate}`);
      }
      if (a.tweetText) lines.push(`「${String(a.tweetText).slice(0, 100)}」`);
      if (a.tweetIsVideo) lines.push('原帖是视频，下面是封面帧');
      lines.push(`主页：https://x.com/${a.user}`);
      segs.push({ type: 'text', data: { text: lines.join('\n') } });

      for (const sp of (a.tweetShotsLocal || []).slice(0, 2)) {
        if (isSendableImageRef(sp)) {
          segs.push({ type: 'image', data: { file: toFileUri(sp) } });
        }
      }

      nodes.push({ type: 'node', data: {
        user_id: Number(ctx.selfId) || 0,
        nickname: `${ci}. @${a.user}`.slice(0, 40),
        content: segs
      } });

      cands.push({
        site: 'twitter_post',
        url: postByUser[a.user] || '',
        title: String(a.tweetText || a.name || a.user).slice(0, 120),
        id: postByUser[a.user] ? String(postByUser[a.user]).split('/status/')[1] : '',
        tweetId: postByUser[a.user] ? String(postByUser[a.user]).split('/status/')[1] : '',
        isVideo: !!a.tweetIsVideo
      });
    }

    if (!nodes.length && posts.length) {
      const txt = posts.slice(0, 10).map((p, i) => `${i + 1}. ${p.url}`).join('\n');
      nodes.push({ type: 'node', data: {
        user_id: Number(ctx.selfId) || 0,
        nickname: '原帖链接',
        content: [{ type: 'text', data: { text: txt } }]
      } });
    }

    if (cands.length) {
      putSiteCandidates(ctx.chatKey, {
        items: cands, keyword: '识图结果', site: 'imgsearch'
      });
    }

    const src = `识图结果：${accts.length || posts.length} 个来源`;
    const sent = await sendForwardWithRetry(ctx, nodes,
      { source: src, summary: src, prompt: src });
    if (!sent.ok) {
      await sendText(ctx, `识图成功但卡片发送失败：${String(sent.error || '').slice(0, 100)}。稍等重试。`);
    }
  } catch (err) {
    logFns.error?.('渲染识图结果失败：', err?.stack ?? err);
    try { await sendText(ctx, `❌ 识图结果渲染出错：${String(err?.message ?? err).slice(0, 100)}`); } catch { /* ignore */ }
  }
}

// ── 卡片发送重试 ────────────────────────────────────────────────────────────
//
// 为什么需要：实测 QQ 对"图片/视频上传"有瞬时限流，返回
//   { status:"failed", retcode:100,
//     wording:"group message delivery was not confirmed: QQ returned no
//              message receipt; check the conversation before retrying" }
// 特征是**很快就失败**（1 秒内），而且过几秒重试就成功 —— 说明不是内容坏了，
// 而是 QQ 那边这次没确认。早期代码一失败就放弃，用户看到的就是"卡片通道炸了"。
//
// 策略：只对"可重试"的错误做指数退避重试（2s → 5s → 12s）。
// 注意：仍然**不做裸发兜底**（用户明确要求内容只能出现在卡片里），
// 重试还是走同一条卡片通道，失败就把真实原因返回给上层。
const RETRY_DELAYS_MS = [2000, 5000, 12000];

// ── 转发写法自动切换 ────────────────────────────────────────────────────
//
// 两种 node 写法：
//   typed : { type:'node', data:{ user_id, nickname, content } }
//   bare  : { user_id, nickname, content }
// SnowLuma / 其他 OneBot 实现可能只认其中一种。判断"是不是写法问题"靠这些关键词。
const autoSyntax = new Map();   // chatKey -> 'typed' | 'bare'（记住本次进程里成功的写法）

// 「换一批」用：每个会话已经展示过的 URL。超过 500 条自动裁剪。
const shownUrls = new Map();

// ── 搜索冷却 ────────────────────────────────────────────────────────────
//
// 为什么需要：多人同时发「找涩涩」时，每次搜索会 spawn 一个 Python worker，
// 里面可能再起 Chromium（426MB/实例）。3 个人同时搜 → 1.3GB 直接吃掉，
// 4GB 的服务器就 OOM 了。而且 QQ 侧图片上传也有频率限制。
//
// 做法：同会话两次搜索之间至少间隔 searchCooldownSeconds（默认 30 秒）。
// 冷却期内直接返回"还要等 N 秒"，不起任何子进程。
//
// 为什么按会话而不是全局：A 群在搜不应该挡住 B 群。
// 全局限流由底层并发数控制（见 SEARCH_CONCURRENCY）。
const searchCooldown = new Map();   // chatKey -> 上次搜索的结束时间戳

// 全局并发闸：同时最多跑几个搜索。
// 4 核 4G 的经验值 —— 每个搜索峰值约 1GB（Chromium）+ 0.3GB（Python）。
const SEARCH_CONCURRENCY = 2;
let searchRunning = 0;
const searchQueue = [];

async function acquireSearchSlot() {
  if (searchRunning < SEARCH_CONCURRENCY) {
    searchRunning += 1;
    return;
  }
  // 排队：返回一个 Promise，等别人释放时 resolve
  await new Promise((resolve) => searchQueue.push(resolve));
  searchRunning += 1;
}

function releaseSearchSlot() {
  searchRunning = Math.max(0, searchRunning - 1);
  const next = searchQueue.shift();
  if (next) next();
}

/**
 * 检查并占用冷却。返回 {ok:true} 或 {ok:false, waitSec, message}。
 * 调用方在搜索**开始前**调用，结束时调 markSearchDone 记录时间。
 */
function checkCooldown(ctx, label) {
  const s = settings();
  const cd = Number(s.searchCooldownSeconds) || 0;
  if (cd <= 0) return { ok: true };

  const key = String(ctx.chatKey);
  const last = searchCooldown.get(key);
  const now = Date.now();
  if (last) {
    const elapsed = (now - last) / 1000;
    if (elapsed < cd) {
      const wait = Math.ceil(cd - elapsed);
      return {
        ok: false,
        waitSec: wait,
        message: `这个会话刚搜过，还要等 ${wait} 秒（冷却 ${cd} 秒）。`
          + `\n为什么有冷却：多人同时搜会让服务器同时起多个浏览器实例，`
          + `机器扛不住就卡死。`
          + `\n⚠️ 如实告诉群友"等一下再搜"，不要说"没货"，也不要偷偷换关键词重试。`,
      };
    }
  }
  return { ok: true };
}

function markSearchDone(ctx) {
  searchCooldown.set(String(ctx.chatKey), Date.now());
  // 顺手清理超过 10 分钟的旧记录，避免 Map 无限增长
  const now = Date.now();
  for (const [k, t] of searchCooldown) {
    if (now - t > 10 * 60 * 1000) searchCooldown.delete(k);
  }
}

function isSyntaxError(err) {
  const m = String(err?.message ?? err ?? '').toLowerCase();
  // 实测的几种报法
  if (m.includes('invalid_field')) return true;
  if (m.includes('invalid field')) return true;
  if (m.includes('node')) return true;              // "node 段格式不对"
  if (m.includes('segment must have type')) return true;
  if (m.includes('写法')) return true;
  if (m.includes('unknown segment')) return true;
  // 注意：'not confirmed' / timeout 不是写法问题，不能误判
  if (m.includes('not confirmed') || m.includes('no message receipt')) return false;
  if (m.includes('timeout') || m.includes('timed out')) return false;
  if (m.includes('fetch failed')) return false;
  return false;
}

/**
 * 按指定写法重建节点数组。
 *
 * 只处理**最外层**：content 里的 node 段永远保持 typed
 * （SnowLuma 的 isNestedNodeArray 只认 type:'node'，裸对象会被判成普通段）。
 */
function rebuildForSyntax(nodes, syntax) {
  return (nodes || []).map(function (n) {
    const isTyped = n && n.type === 'node' && n.data;
    const d = isTyped ? n.data : n;
    if (!d) return n;
    const content = d.content || [];
    // 嵌套层：只要有 node 段，里面一律保持 typed
    const fixedContent = content.map(function (sg) {
      if (sg && (sg.user_id !== undefined || sg.nickname !== undefined) && sg.type !== 'node' && !sg.data) {
        // 裸 node 段 → 转 typed
        const { user_id, nickname, content: inner } = sg;
        const data = { content: inner };
        if (user_id !== undefined) data.user_id = user_id;
        if (nickname !== undefined) data.nickname = nickname;
        return { type: 'node', data };
      }
      return sg;
    });
    if (syntax === 'bare') {
      const out = { content: fixedContent };
      if (d.user_id !== undefined) out.user_id = d.user_id;
      if (d.nickname !== undefined) out.nickname = d.nickname;
      return out;
    }
    const data = { content: fixedContent };
    if (d.user_id !== undefined) data.user_id = d.user_id;
    if (d.nickname !== undefined) data.nickname = d.nickname;
    return { type: 'node', data };
  });
}

// ── 卡片去重 ────────────────────────────────────────────────────────────
//
// 为什么需要：QQ 返回 "group message delivery was not confirmed" 时，
// 消息**可能其实已经送达**（只是回执没拿到）。我们的重试机制会再发一遍
// → 群友看到同一个视频卡片刷屏好几次。
//
// 所以发之前先算内容指纹，短时间内重复的同一张卡片直接跳过。
// 这不是"降级"也不是"吞掉内容"，只是避免同一份东西重复刷屏。
const sentCardFingerprints = new Map();   // fp -> { at, chatKey }
const sentVideoFiles = new Map();          // chatKey::path -> 时间戳
const VIDEO_DEDUP_MS = 10 * 60 * 1000;     // 同一个视频 10 分钟内不重复发
const CARD_DEDUP_MS = 3 * 60 * 1000;      // 3 分钟内同样的卡片只发一次

// ⚠️ 去重记录必须落盘。
// 实测：技能热重载（文件监听触发，一次改代码就重载好几遍）会重建模块，
// 内存里的 Map 全空 → 同一视频又被发一遍，群友看到刷屏。
const DEDUP_FILE = path.join(HERE, 'data', 'sent_dedup.json');

function loadDedup() {
  try {
    if (!fs.existsSync(DEDUP_FILE)) return;
    const raw = JSON.parse(fs.readFileSync(DEDUP_FILE, 'utf8'));
    const now = Date.now();
    for (const [k, at] of Object.entries(raw.cards || {})) {
      if (now - at <= CARD_DEDUP_MS) sentCardFingerprints.set(k, { at });
    }
    for (const [k, at] of Object.entries(raw.videos || {})) {
      if (now - at <= VIDEO_DEDUP_MS) sentVideoFiles.set(k, at);
    }
  } catch (err) {
    logFns.warn?.('读取去重记录失败：', err?.message ?? err);
  }
}

function saveDedup() {
  try {
    const dir = path.dirname(DEDUP_FILE);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    const cards = {};
    const now = Date.now();
    for (const [k, v] of sentCardFingerprints) {
      if (now - v.at <= CARD_DEDUP_MS) cards[k] = v.at;
    }
    const videos = {};
    for (const [k, at] of sentVideoFiles) {
      if (now - at <= VIDEO_DEDUP_MS) videos[k] = at;
    }
    fs.writeFileSync(DEDUP_FILE, JSON.stringify({ cards, videos }), 'utf8');
  } catch (err) {
    logFns.warn?.('保存去重记录失败：', err?.message ?? err);
  }
}

loadDedup();

function cardFingerprint(nodes) {
  // 用节点数量 + 每个节点的类型和文本/文件路径前若干字符算指纹。
  // 不追求密码学强度，只要能区分"是不是同一张卡片"就够了。
  const parts = [];
  for (const n of nodes || []) {
    const d = n && n.type === 'node' ? n.data : n;
    if (!d) continue;
    const segs = d.content || [];
    const segDesc = segs.map(function (sg) {
      const t = sg && sg.type;
      if (t === 'text') return 't:' + String(sg.data?.text || '').slice(0, 80);
      if (t === 'image') return 'i:' + String(sg.data?.file || '').slice(-40);
      if (t === 'video') return 'v:' + String(sg.data?.file || '').slice(-40);
      return t;
    }).join('|');
    parts.push(segDesc);
  }
  return parts.join('||').slice(0, 4000);
}

function cardAlreadySent(ctx, fp) {
  const key = String(ctx.chatKey) + '::' + fp;
  const hit = sentCardFingerprints.get(key);
  if (!hit) return false;
  if (Date.now() - hit.at > CARD_DEDUP_MS) {
    sentCardFingerprints.delete(key);
    return false;
  }
  return true;
}

function markCardSent(ctx, fp) {
  const key = String(ctx.chatKey) + '::' + fp;
  sentCardFingerprints.set(key, { at: Date.now() });
  const now = Date.now();
  for (const [k, v] of sentCardFingerprints) {
    if (now - v.at > CARD_DEDUP_MS) sentCardFingerprints.delete(k);
  }
  saveDedup();
}

// "未确认"类错误：QQ 可能已经送达，只是回执没拿到。
// 这类只允许重试 1 次（再多就是刷屏），网络类错误才可以多试几次。
function isAmbiguousSendError(err) {
  const m = String(err?.message ?? err ?? '').toLowerCase();
  return m.includes('not confirmed') || m.includes('no message receipt');
}

function isRetryableSendError(err) {
  const m = String(err?.message ?? err ?? '').toLowerCase();
  // 瞬时限流 / 未确认 / 超时 才重试
  if (m.includes('not confirmed')) return true;
  if (m.includes('no message receipt')) return true;
  if (m.includes('timeout') || m.includes('timed out')) return true;
  if (m.includes('fetch failed')) return true;
  if (m.includes('econnreset') || m.includes('socket hang up')) return true;
  // 明确的"不在群/被踢/被禁言"不重试（重试也没用）
  if (m.includes('不在该群') || m.includes('移出') || m.includes('禁言')) return false;
  return false;
}

/**
 * 带重试地发一张合并转发卡片。
 * @param {object} ctx
 * @param {Array} nodes
 * @param {object} extra { source, summary, prompt }
 * @returns {Promise<{ok:boolean, error?:string, attempts:number}>}
 */
async function sendForwardWithRetry(ctx, nodes, extra) {
  const [kind, id] = String(ctx.chatKey).split(':');
  const action = kind === 'private' ? 'send_private_forward_msg' : 'send_group_forward_msg';
  const base = kind === 'private' ? { user_id: Number(id) } : { group_id: Number(id) };


  // 同一张卡片 3 分钟内不重复发（QQ 回执丢失时重试会造成刷屏）
  const fp = cardFingerprint(nodes);
  if (cardAlreadySent(ctx, fp)) {
    logFns.log?.('同一张卡片 3 分钟内已发过，跳过重复发送');
    return { ok: true, attempts: 0, deduped: true };
  }

  // ── 转发写法自动切换 ────────────────────────────────────────────────
  //
  // 为什么需要：SnowLuma 和不同 OneBot 实现对 node 段有两种写法
  //   typed : { type:'node', data:{ user_id, nickname, content } }
  //   bare  : { user_id, nickname, content }
  // 换实现 / 升级版本时可能只认其中一种，报 "INVALID_FIELD" 或
  // "转发节点写法不对"。以前只能让管理员手动去设置里切，很痛苦。
  //
  // 现在：先按配置的写法发；如果报的是"写法类"错误，**自动换另一种重发**，
  // 并把本次成功的写法记住（同一进程后续都用它），下次不用再试错。
  // 注意：全程只走卡片通道，不会退化成裸发。
  const syntaxTried = new Set();
  let useSyntax = autoSyntax.get(ctx.chatKey) || settings().nodeSyntax || 'typed';

  // 首次发送就按目标写法重建一次（调用方可能传的是别的写法）
  let payload = { ...base, messages: rebuildForSyntax(nodes, useSyntax), ...(extra || {}) };

  let lastErr = '';
  for (let attempt = 0; attempt <= RETRY_DELAYS_MS.length; attempt += 1) {
    try {
      await ctx.onebot.call(action, payload);
      if (attempt > 0) logFns.log?.(`卡片第 ${attempt + 1} 次尝试成功`);
      markImgChannel(ctx.chatKey, true, '');
      markCardSent(ctx, fp);
      return { ok: true, attempts: attempt + 1 };
    } catch (err) {
      lastErr = String(err?.message ?? err);
      // 写法类错误：自动换另一种 node 写法，立刻重试（不占重试额度）
      if (isSyntaxError(err) && !syntaxTried.has(attempt)) {
        syntaxTried.add(attempt);
        const nextSyntax = String(useSyntax).toLowerCase() === 'typed' ? 'bare' : 'typed';
        logFns.warn?.(`转发写法 ${useSyntax || settings().nodeSyntax} 失败，自动切到 ${nextSyntax} 重试`);
        try {
          const alt = rebuildForSyntax(nodes, nextSyntax);
          await ctx.onebot.call(action, { ...base, messages: alt, ...(extra || {}) });
          payload = { ...base, messages: alt, ...(extra || {}) };
          autoSyntax.set(ctx.chatKey, nextSyntax);
          markImgChannel(ctx.chatKey, true, '');
          markCardSent(ctx, fp);
          return { ok: true, attempts: attempt + 1, syntax: nextSyntax };
        } catch (err2) {
          logFns.warn?.(`另一种写法 ${nextSyntax} 也失败：${String(err2?.message ?? err2).slice(0, 100)}`);
          useSyntax = nextSyntax;
          lastErr = String(err2?.message ?? err2);
        }
      }
      const retryable = isRetryableSendError(err);
      const ambiguous = isAmbiguousSendError(err);
      // 模糊错误（可能已送达）最多重试 1 次，避免同一张卡片刷屏
      const maxAttempts = ambiguous ? 1 : RETRY_DELAYS_MS.length;
      const hasNext = attempt < maxAttempts;
      if (!retryable || !hasNext) {
        logFns.warn?.(`卡片发送失败（尝试 ${attempt + 1} 次，不再重试）：${lastErr.slice(0, 120)}`);
        if (retryable) markImgChannel(ctx.chatKey, false, lastErr);
        return { ok: false, error: lastErr, attempts: attempt + 1 };
      }
      const wait = RETRY_DELAYS_MS[attempt];
      logFns.warn?.(`卡片发送未确认（第 ${attempt + 1} 次），${wait / 1000} 秒后重试：${lastErr.slice(0, 90)}`);
      await new Promise((r) => setTimeout(r, wait));
    }
  }
  return { ok: false, error: lastErr, attempts: RETRY_DELAYS_MS.length + 1 };
}

/**
 * 把视频包进**一层**合并转发卡片发出去。
 *
 * 约束：SnowLuma 要求 video 段在**同一个节点内**独占
 * （assertVideoSendPolicy: effectiveSegmentCount 必须为 1），
 * 所以每个视频单独占一个节点，节点内容只有 [{type:'video'}]。
 * 文字说明再单独一个节点（text 节点里不能混 video）。
 *
 * 节点与节点之间没有这个限制，全部挂在 messages 数组下就是"一层"
 * （树深 0，点开一次全展开）。实测 136MB 视频节点发送成功。
 *
 * @param {object} ctx
 * @param {Array<{localPath:string, title?:string, note?:string}>} vids
 * @returns {Promise<{ok:number, failed:number, viaCard:boolean, error?:string}>}
 */
/**
 * 发「视频地址卡片」—— 只给链接，不下载、不发送视频文件。
 *
 * 为什么改：
 *   2026-10-27 需求「不用下载视频文件发送，给出视频地址即可」。
 *   原来是下载完整 mp4 再作为 video 段发出去 —— 等于在服务器上留存并
 *   分发完整内容，磁盘、带宽、CPU 三样全吃，风险也最高。
 *   现在只把地址给出去，用户自己去打开。
 *
 * 卡片结构（一层）：
 *   · 每个视频一个节点：标题 + 时长 + 页面链接 + 播放地址
 *   · 末尾一个说明节点：告诉群友怎么用
 *
 * 为什么不直接把 m3u8 当链接发：
 *   MissAV 的 m3u8 需要 Referer、Pornhub 的直链有时效和 IP 绑定。
 *   所以优先给**页面链接**（永远有效），播放地址作为附加信息。
 *
 * @param {object} ctx
 * @param {Array} items [{ title, pageUrl, hls, duration, site }]
 */
async function sendVideoLinksCard(ctx, items) {
  const list = (items || []).filter((v) => v && (v.pageUrl || v.hls));
  if (!list.length) {
    return { ok: 0, error: '没有可发的地址' };
  }

  const nodes = [];
  list.forEach((v, i) => {
    const segs = [];
    const dur = Number(v.duration) || 0;
    const dt = dur > 0
      ? `${Math.floor(dur / 60)}:${String(dur % 60).padStart(2, '0')}`
      : '';
    const head = [`【${i + 1}】`, v.site || '', dt].filter(Boolean).join(' · ');

    const lines = [head];
    if (v.title) lines.push(String(v.title).slice(0, 140));
    if (v.pageUrl) lines.push(`页面：${v.pageUrl}`);
    // 播放地址单独一行，标注它可能需要 Referer
    if (v.hls) {
      lines.push(`播放地址：${String(v.hls).slice(0, 300)}`);
      if (v.referer) lines.push(`（这个地址要带 Referer: ${v.referer}）`);
    }
    if (v.note) lines.push(String(v.note).slice(0, 120));

    segs.push({ type: 'text', data: { text: lines.join('\n') } });

    // 封面能拿到就带上（纯图，不涉及视频文件）
    if (v.cover && isSendableImageRef(v.cover)) {
      segs.unshift({ type: 'image', data: { file: toFileUri(v.cover) } });
    }

    nodes.push({
      type: 'node',
      data: {
        user_id: Number(ctx.selfId) || 0,
        nickname: `${i + 1}. ${String(v.title || v.site || '视频').slice(0, 26)}`,
        content: segs
      }
    });
  });

  nodes.push({
    type: 'node',
    data: {
      user_id: Number(ctx.selfId) || 0,
      nickname: '📋 说明',
      content: [{ type: 'text', data: {
        text: '以上是视频地址，点开页面即可播放。\n'
          + '需要走代理的站点，请自行配置。\n'
          + '（本技能不再下载视频文件，只提供地址）'
      } }]
    }
  });

  const label = `🎬 ${list.length} 个视频地址`;
  const sent = await sendForwardWithRetry(ctx, nodes,
    { source: label, summary: label, prompt: label });
  if (!sent.ok) {
    logFns.error?.('视频地址卡片发送失败：', sent.error);
    return { ok: 0, error: String(sent.error || '卡片发送失败') };
  }
  return { ok: list.length };
}

async function sendVideosAsOneCard(ctx, vids, opts) {
  const [kind, id] = String(ctx.chatKey).split(':');
  let list = (vids || []).filter((v) => v && v.localPath);
  if (!list.length) return { ok: 0, failed: 0, viaCard: false, error: '没有可发的视频' };

  // 同一个视频文件 10 分钟内不给同一个会话重复发。
  // 触发场景：群友说「下 1」两次、或模型自己重试、或后台任务重复启动。
  const now0 = Date.now();
  const force = !!(opts && opts.force);
  const before = list.length;
  list = force ? list : list.filter((v) => {
    const key = String(ctx.chatKey) + '::vid::' + String(v.localPath);
    const hit = sentVideoFiles.get(key);
    if (hit && now0 - hit < VIDEO_DEDUP_MS) {
      logFns.log?.(`视频 ${String(v.localPath).split(/[\\/]/).pop()} 10 分钟内已发过，跳过`);
      return false;
    }
    return true;
  });
  if (!list.length) {
    // 告诉群友为什么不发（否则他刚看到"已开始下载"却什么都没收到，很困惑）
    try {
      await sendText(ctx, `这个视频 ${Math.round(VIDEO_DEDUP_MS / 60000)} 分钟内已经发过了，不重复发。`
        + `要再发一次就说「重发」。`);
    } catch { /* ignore */ }
    return { ok: 0, failed: 0, viaCard: true, deduped: true,
             error: '这个视频刚发过，跳过重复发送' };
  }

  // ① 每个视频一个节点（内容只有 video 段）
  const nodes = list.map((v, i) => ({
    type: 'node',
    data: {
      user_id: Number(ctx.selfId) || 0,
      nickname: `🎬 视频 ${i + 1}`.slice(0, 40),
      content: [{ type: 'video', data: { file: toFileUri(v.localPath) } }]
    }
  }));

  // ② 说明节点：一条文字汇总所有视频信息（不能塞进 video 节点）
  const lines = [];
  list.forEach((v, i) => {
    const mb = v.bytes ? Math.round(v.bytes / 1048576) : 0;
    const dur = Number(v.duration) || 0;
    const dt = dur > 0 ? `${Math.floor(dur / 60)}:${String(dur % 60).padStart(2, '0')}` : '';
    const head = [`【${i + 1}】`, dt, mb ? `${mb}MB` : '', v.compressed ? '已压缩' : '', v.cached ? '缓存' : '']
      .filter(Boolean).join(' · ');
    lines.push(head);
    if (v.title) lines.push(String(v.title).slice(0, 140));
    if (v.note) lines.push(String(v.note).slice(0, 120));
  });
  nodes.push({
    type: 'node',
    data: {
      user_id: Number(ctx.selfId) || 0,
      nickname: '📋 说明',
      content: [{ type: 'text', data: { text: lines.join('\n').slice(0, 1800) } }]
    }
  });

  const label = `🎬 ${list.length} 个视频`;
  const sent = await sendForwardWithRetry(ctx, nodes, { source: label, summary: label, prompt: label });
  const viaCard = sent.ok;
  const cardError = sent.ok ? '' : String(sent.error || '');
  if (sent.ok) {
    for (const v of list) {
      sentVideoFiles.set(String(ctx.chatKey) + '::vid::' + String(v.localPath), Date.now());
    }
    saveDedup();
  }

  if (viaCard) return { ok: list.length, failed: 0, viaCard: true };

  // ③ 卡片失败：**不退回单独发视频**。
  //
  // 用户明确要求视频必须在卡片里，所以这里宁可报错也不裸发。
  // 早期版本把"卡片失败就逐条单独发 video"当兜底 —— 那正是
  // "视频直接发出来、没进卡片"的来源（卡片一失败就降级成裸视频）。
  // 现在改成如实返回失败原因，由上层提示群友重试。
  logFns.error?.('视频卡片发送失败，按配置不再降级为裸视频发送：', cardError);
  return { ok: 0, failed: list.length, viaCard: false, error: cardError || '卡片发送失败' };
}

// ── 后台下载任务 ────────────────────────────────────────────────────────────
//
// 问题：ffmpeg 下载一个大视频要 10-20 分钟，而 runGrab 是**同步等待**的。
// 工具调用不返回 → 这一轮对话不结束 → 群友再 @ 也没反应（看起来像卡死）。
// 实测：19:08 的 ffmpeg 跑了 4 分钟，期间所有消息都在排队。
//
// 方案：下载任务丢到后台跑，工具**立刻返回**"已开始，下好自动发"。
// 完成后由后台自己调 sendVideosAsOneCard 发出。
// 正在跑的任务按会话记录，避免同一个人重复触发。

const bgJobs = new Map();   // chatKey -> { startedAt, label }

function bgKey(chatKey, tag) { return `${chatKey}::${tag}`; }

/**
 * 在后台跑一个下载任务。
 * @param {object} ctx
 * @param {string} tag 任务标识（用于去重/日志）
 * @param {string} label 给群友看的名字
 * @param {Function} runner 返回 Promise<{ok, localPath, ...}> 的任务函数
 * @param {Function} [onDone] 完成回调（拿到结果后做什么，默认发视频卡片）
 * @returns {{started:boolean, reason?:string}}
 */
function runInBackground(ctx, tag, label, runner, onDone, opts) {
  const k = bgKey(ctx.chatKey, tag);
  if (bgJobs.has(k)) {
    return { started: false, reason: '这个视频已经在下载了，等它下完' };
  }
  bgJobs.set(k, { startedAt: Date.now(), label });

  // 不 await：让工具立刻返回
  (async () => {
    const rep = makeDownloadReporter(ctx, label);
    try {
      await rep.start();
      const r = await runner((ev) => { rep.onProgress(ev); });
      // 识图/磁力这类"处理型"任务：只要 worker 说 ok 就算完成，
      // 哪怕结果是空（比如识图没找到来源），由 onDone 负责怎么呈现。
      // ⚠️ 这里不能靠"没有 localPath"去推断 —— 识图返回的 accounts/posts
      // 是**空数组**，空数组也是 Array，之前那种推断条件恒为 false。
      if (opts && opts.acceptEmpty && r && r.ok === true) {
        r.acceptEmpty = true;
      }
      // ⚠️ 两种返回形态都要认：
      //   · 单视频任务（gamebus_video / album_video / site_video）→ { ok, localPath }
      //   · 批量任务（pick_download）→ { ok, results: [{localPath, ...}] }
      // 之前只判断 r.localPath，而 pick_download 返回的是 results 数组，
      // 于是 localPath 永远 undefined → 每次报"下载失败：未知原因"，
      // 其实视频早就下好了（实测 452MB 已落盘）。
      // 三种返回形态都要认：
      //   · 单视频  { ok, localPath }
      //   · 批量    { ok, results:[{localPath}] }
      //   · 识图/磁力 { ok, accounts/posts/files }   ← 没有 localPath
      // 早期只认前两种，导致识图后台任务被判失败（"下载失败：未知原因"）。
      const anyOk = !!(r && r.ok === true && (
        r.localPath
        || (Array.isArray(r.results) && r.results.length)
        || (Array.isArray(r.accounts) && r.accounts.length)
        || (Array.isArray(r.posts) && r.posts.length)
        || (Array.isArray(r.files) && r.files.length)
        || r.acceptEmpty === true       // 处理型任务：空结果也算完成
      ));
      if (!anyOk) {
        await sendText(ctx, `❌ ${label} 下载失败：${String(r?.error || r?.code || '未知原因').slice(0, 100)}`);
        return;
      }
      if (onDone) {
        await onDone(r);
      }
    } catch (err) {
      logFns.error?.('后台下载异常：', err?.stack ?? err);
      try { await sendText(ctx, `❌ ${label} 出错：${String(err?.message ?? err).slice(0, 100)}`); } catch { /* ignore */ }
    } finally {
      bgJobs.delete(k);
    }
  })();

  return { started: true };
}

/** 后台任务是否还在跑（给工具返回文案用）。 */
function bgRunning(ctx, tag) { return bgJobs.has(bgKey(ctx.chatKey, tag)); }

/**
 * 下载进度播报器。
 *
 * 为什么需要：一个视频下载+压缩+上传要 10-20 分钟，中间没有任何反馈时
 * 群友会以为"没开始"或"卡死了"。实测用户反馈过「有时候会没有开始下载」——
 * 其实是开始了但静默太久。
 *
 * 做法：worker 会持续发 progress 事件（阶段名 + note），这里节流成
 * 最多每 20 秒发一条 QQ 消息，避免刷屏，同时保证"开头一定有反馈"。
 */
function makeDownloadReporter(ctx, label) {
  const startAt = Date.now();
  let lastSent = 0;
  let sentCount = 0;
  return {
    // 立刻发一条"已开始"
    async start() {
      lastSent = Date.now();
      sentCount += 1;
      try {
        // 文案按任务类型区分（识图/磁力不是"下载"）
        const isDownload = /下载|视频|番号/.test(String(label));
        await sendText(ctx, isDownload
          ? `⏳ ${label}：已开始下载，完成后直接发（可能要十几分钟）`
          : `⏳ ${label}：已开始处理，完成后直接发（约 1-2 分钟）`);
      } catch { /* ignore */ }
    },
    // worker 的 progress 回调
    async onProgress(ev) {
      const note = String(ev?.note || '').trim();
      if (!note) return;
      const now = Date.now();
      // 首条进度（刚开始的解析/取地址阶段）立即发，之后节流 20 秒
      const force = sentCount === 0;
      if (!force && now - lastSent < 20000) return;
      lastSent = now;
      sentCount += 1;
      const mins = Math.floor((now - startAt) / 60000);
      const tag = mins > 0 ? `[${mins}分]` : '';
      try { await sendText(ctx, `⏳ ${tag}${note}`); } catch { /* ignore */ }
    },
    elapsed() { return Math.round((Date.now() - startAt) / 1000); }
  };
}

/**
 * 把站点搜索结果渲染成"带封面预览的编号列表"。
 *
 * 为什么要预览图：光看标题不知道是哪个，封面是第一眼判断依据。
 * 每条发一条独立消息（文字里带封面 URL），因为：
 *   · 合并转发卡片里塞外链图要先下载内联，几十条会很慢
 *   · 单独发文字+封面链接，群友点开就能看，而且不影响下载决策
 *
 * @returns {Promise<number>} 发出的条目数
 */
async function sendSiteCandidates(ctx, items, opts) {
  const [kind, id] = String(ctx.chatKey).split(':');
  // 注意：这里**不保留** send() 裸发辅助函数。
  // 用户要求候选只能出现在卡片里；留一个"能裸发"的入口迟早被误用
  // （之前就是靠它做兜底，导致卡片失败时内容散着发出来）。
  const s = settings();

  // 每个候选一个节点，全部塞进**一张一层**的合并转发。
  //
  // 为什么是一层：用户明确要求"只有一层"。一层 = messages 里直接就是
  // 各条目节点，不再包一层"内容"壳。看的时候点开一次就全展开。
  //
  // 节点内容 = 封面图（本地文件）+ 编号/标题/时长/链接。
  // 封面必须用本地路径：pic.sugfew.cn 的 URL 直接发会报
  // highway upload error_code=921（协议端拉不到那个域）。
  const nodeOf = (it, i, withCover) => {
    const segs = [];
    // withCover=false 时**完全不放图**（连 URL 兜底也不放）——
    // 之前 URL 兜底写在 else 分支里绕过了这个开关，导致
    // siteCoverMax=6 却仍然发了 8 张图，卡片因图片过多而失败。
    let hasCover = false;
    if (withCover !== false) {
      const cover = String(it.coverLocal || '');
      if (cover && isSendableImageRef(cover)) {
        segs.push({ type: 'image', data: { file: toFileUri(cover) } });
        hasCover = true;
      }
      // 注意：图片段仍然**只用本地文件**。远程 URL 直发在 pic.sugfew.cn
      // 这类域上必定失败（highway upload error_code=921，内容加密），
      // 加了只会拖慢并拉高整张卡片的失败率。
      // 但封面抓不到时，把**图片地址当文字**给出去 —— 群友至少能点开看，
      // 比"什么都没有"强，而且文字段不会让卡片发送失败。
    }
    const dur = Number(it.duration) || 0;
    const durText = dur > 0 ? `${Math.floor(dur / 60)}:${String(dur % 60).padStart(2, '0')}` : '';
    const meta = [durText, it.views ? `${it.views} 次观看` : ''].filter(Boolean).join(' · ');
    const lines = [`【${i + 1}】${it.title || '(无标题)'}`];
    if (meta) lines.push(meta);
    if (it.url) lines.push(it.url);
    // 没有本地封面时，把远程封面地址也贴上（当文字，不做图片段）
    if (!hasCover && it.cover && /^https?:/i.test(String(it.cover))) {
      lines.push(`封面：${String(it.cover).slice(0, 160)}`);
    }
    segs.push({ type: 'text', data: { text: lines.join('\n') } });
    return {
      type: 'node',
      data: {
        user_id: Number(ctx.selfId) || 0,
        nickname: `${ctx.selfNickname || '机器人'} · ${i + 1}`.slice(0, 40),
        content: segs
      }
    };
  };

  // 图片上传很慢，且密集上传容易触发 QQ 限流
  // （实测「group message delivery was not confirmed」）。
  // 超过 MAX_COVER_NODES 条时，只给前 N 条带封面，其余用纯文字节点 ——
  // 保证卡片一定发得出去，只是后面的条目没预览图。
  const MAX_COVER_NODES = Number(s.siteCoverMax) || 6;
  const nodes = items.map((it, i) => nodeOf(it, i, i < MAX_COVER_NODES));
  // 一层：直接就是一个节点数组，不再包 "内容" 壳
  // 节点数也要限制：实测 11 节点 + 8 张图会 fetch failed。
  // 超出部分截断并在末尾说明（不静默丢，让群友知道可以再搜）。
  const MAX_NODES = 12;
  let messages = nodes;
  if (nodes.length > MAX_NODES) {
    messages = nodes.slice(0, MAX_NODES);
    logFns.log?.(`候选 ${nodes.length} 条，卡片只放前 ${MAX_NODES} 条（其余请再搜或调高 limit）`);
  }

  const snapId = String(opts?.snapId || '');
  const header = [
    `【${snapId}】找到 ${items.length} 条（编号 ${snapId}1 - ${snapId}${items.length}）`,
    `回复「下 ${snapId}3」或「下 ${snapId}1,${snapId}4」下载`,
    opts?.note || ''
  ].filter(Boolean).join('\n');

  // 走带重试的卡片通道（QQ 图片上传有瞬时限流，见 sendForwardWithRetry 注释）。
  // 仍然不做裸发兜底 —— 失败就把原因返回给上层。
  const src = header.slice(0, 60);
  const sent = await sendForwardWithRetry(ctx, messages, { source: src, summary: src, prompt: src });
  const viaCard = sent.ok;
  const cardError = sent.ok ? '' : String(sent.error || '');

  if (!viaCard) {
    // ⚠️ 卡片失败时**不退回逐条发送**。
    //
    // 用户明确要求：所有图片/视频只能出现在卡片里，不能裸发。
    // 早期这里会"卡片失败就逐条发图+文字"当兜底 —— 那正是
    // "编号没有卡片、内容散着发出来"的来源。
    // 现在改成如实返回失败，由上层提示重试。
    logFns.error?.('候选卡片发送失败，按配置不再降级为逐条发送：', cardError);
    return { sent: 0, viaCard: false, cardError: cardError || '卡片发送失败' };
  }
  // 卡片已经带了说明，不再单独发头部
  return { sent: items.length, viaCard: true, cardError: '' };
}

/**
 * 发合并转发卡片（群聊）。
 *
 * @param {object} ctx
 * @param {Array<object>} items 本张卡片的结果
 * @param {object} p { keyword, page, totalPages, depth, mediaTotal }
 */
async function sendGroupCard(ctx, items, p) {
  const s = settings();
  const messages = buildForwardCard({
    syntax: s.nodeSyntax,
    // 允许单张卡临时指定深度（单卡片模式用 3，承载全部条目）
    depth: p.depth || s.nestDepth,
    selfId: ctx.selfId,
    selfNickname: ctx.selfNickname || '机器人',
    keyword: p.keyword,
    page: p.page,
    totalPages: p.totalPages,
    items,
    headerTemplate: s.headerTemplate,
    opts: { showAuthor: s.showAuthor }
  });

  // ⚠️ 必须走 sendForwardWithRetry，不能直接 ctx.onebot.call。
  // 实测（2026-10-26）：search_media 走的是这个函数，直接 call 没有重试，
  // 一次 "retcode=100 fetch failed" 就整次失败 —— 而 find_video 走
  // sendForwardWithRetry 会自动重试 3 次。同一个 QQ、同一个群，
  // 一个能发一个不能发，就是因为两条路径不一致。
  const sent = await sendForwardWithRetry(ctx, messages, {
    source: p.keyword || '',
    summary: p.keyword || '',
    prompt: p.keyword || ''
  });
  if (!sent.ok) {
    const err = String(sent.error || '');
    // 区分错误类型给不同提示 —— fetch failed 是网络问题，不是写法问题，
    // 之前一律提示"切节点写法"会把人带到错的方向。
    const isNet = /fetch failed|timeout|econnreset|socket hang up/i.test(err);
    const hint = isNet
      ? '这是网络层错误（连不上协议端），不是写法问题。稍等重试即可。'
      : /not confirmed|no message receipt/i.test(err)
        ? 'QQ 回执没拿到，可能已发出也可能没发。稍等重试。'
        : '多半是转发节点写法不兼容，可以试切 nodeSyntax。';
    throw new Error(`OneBot send_group_forward_msg 失败: ${err}。${hint}`);
  }
  return { ok: true, deduped: sent.deduped, attempts: sent.attempts };
}

/**
 * 发合并转发卡片（私聊）。
 *
 * 与群聊的唯一区别是 action 名和 id 字段（send_private_forward_msg / user_id）。
 * 节点树完全复用 buildForwardCard —— 保持两条路径同构，
 * 避免"群里能看的结构、私聊发不出去"这种只在一边踩到的坑。
 *
 * @param {object} ctx
 * @param {Array<object>} items
 * @param {object} p { keyword, page, totalPages, depth, mediaTotal }
 */
async function sendPrivateCard(ctx, items, p) {
  const s = settings();
  const messages = buildForwardCard({
    syntax: s.nodeSyntax,
    depth: p.depth || s.nestDepth,
    selfId: ctx.selfId,
    selfNickname: ctx.selfNickname || '机器人',
    keyword: p.keyword,
    page: p.page,
    totalPages: p.totalPages,
    items,
    headerTemplate: s.headerTemplate,
    opts: { showAuthor: s.showAuthor }
  });

  const sent = await sendForwardWithRetry(ctx, messages, {
    source: p.keyword || '',
    summary: p.keyword || '',
    prompt: p.keyword || ''
  });
  if (!sent.ok) {
    throw new Error(`OneBot send_private_forward_msg 失败: ${String(sent.error || '')}`);
  }
  return { ok: true, deduped: sent.deduped, attempts: sent.attempts };
}

/**
 * 发私聊内容（不套卡片）：一条消息里放图 + 作者 + 视频链接。
 * 图多时按 mediaPerCard 拆成多条，避免单条消息过大。
 */
async function sendPrivateFlat(ctx, items, keyword) {
  const s = settings();
  const [kind, id] = String(ctx.chatKey).split(':');
  if (kind !== 'private') throw new Error('sendPrivateFlat 只能用于私聊');

  const cards = groupByMedia(items, s.mediaPerCard);
  // 私聊只有一条消息带标题（第一条），文案与卡片首条同一个模板，
  // 免得"群聊一套话、私聊另一套话"这种口径分裂。
  // 私聊没有分页概念（不折叠），但模板里可能引用 {page}/{total}，
  // 这里按"第1/1张、全部媒体数"填，让同一个模板在两处都说得通。
  const headerLines = renderTemplate(
    s.headerTemplate || DEFAULT_HEADER_TEMPLATE,
    {
      keyword,
      page: 1,
      total: 1,
      media: items.reduce((sum, it) => sum + mediaCountOf(it), 0),
      results: items.length,
      nickname: ctx.selfNickname || '机器人',
      depth_hint: ''
    }
  );

  const results = [];
  for (let i = 0; i < cards.length; i++) {
    const segs = [];
    if (i === 0) {
      for (const line of (headerLines.length ? headerLines : [`🔞 X 结果 · ${keyword}`])) {
        segs.push({ type: 'text', data: { text: line } });
      }
    }
    for (const item of cards[i]) segs.push(...mediaMessageSegments(item, s));
    if (!segs.length) continue;
    // 私聊图多时给个短间隔，避免协议端连发失败
    if (i > 0) await new Promise((r) => setTimeout(r, 600));
    results.push(await ctx.onebot.call('send_private_msg', { user_id: Number(id), message: segs }));
  }
  return results;
}

/** 私聊单条结果的消息段（图 + 视频预览/原文/链接 + 作者）。 */
function mediaMessageSegments(item, s) {
  const segs = [];
  if (s.showAuthor && item.author) {
    const likes = Number(item.likes) || 0;
    segs.push({ type: 'text', data: { text: `@${item.author}${likes ? ` · ♥${likes}` : ''}` } });
  }
  // 原文（视频推尤其需要：只有链接看不出内容）
  if (item.text) {
    const t = String(item.text).trim();
    if (t) segs.push({ type: 'text', data: { text: t } });
  }
  // 与卡片同一口径：优先内联数据（见 sender.js 的说明）
  const imgs = Array.isArray(item.imageData) && item.imageData.length
    ? item.imageData
    : (item.images || []);
  for (const url of imgs) {
    // 用与卡片同一个判定，避免"私聊能发、卡片丢图"这种口径分裂
    if (isSendableImageRef(url)) segs.push({ type: 'image', data: { file: url } });
    else logFns.warn(`跳过无法发送的图片引用：${String(url).slice(0, 80)}`);
  }
  // 视频：预览图 → 时长 → 链接（与 sender.js 的 mediaSegments 同构）
  const details = Array.isArray(item.videoDetails) && item.videoDetails.length
    ? item.videoDetails
    : (item.videos || []).map((u) => ({ url: u, thumbnail: '', duration: 0 }));
  let vi = 0;
  for (const vd of details) {
    vi += 1;
    const url = String(vd?.url || '');
    if (!isSendableVideoRef(url)) continue;
    const thumb = String(vd?.thumbData || vd?.thumbnail || '');
    if (thumb && isSendableImageRef(thumb)) segs.push({ type: 'image', data: { file: thumb } });
    const dur = Number(vd?.duration) || 0;
    const durText = dur > 0 ? `${Math.floor(dur / 60)}:${String(dur % 60).padStart(2, '0')}` : '';
    const parts = [details.length > 1 ? `🎬 视频 ${vi}` : '🎬 视频'];
    if (durText) parts.push(durText);
    if (vd?.kind === 'gif') parts.push('GIF');
    segs.push({ type: 'text', data: { text: parts.join(' · ') } });
    segs.push({ type: 'text', data: { text: url } });
  }
  if (!segs.length) segs.push({ type: 'text', data: { text: '（这条没有可显示的媒体）' } });
  return segs;
}

// ── 工具实现 ────────────────────────────────────────────────────────────────

/**
 * 完整流程：校验 → 提示语 → 搜索 → 发送。
 *
 * ⚠️「先回提示语、再跑搜索」的顺序是需求要求的（群友要知道机器人没卡死）。
 * 但工具本身是同步返回的，所以提示语在这里先 await 发出去，
 * 搜索在之后继续 —— 模型会看到工具还没返回，群友已经看到提示语了。
 */
/**
 * 把一批结果发出去（群聊/私聊、卡片/平铺、视频补发都在这里）。
 *
 * 抽成独立函数是为了让 searchAndSend 和 send_picked 共用同一套发送逻辑 ——
 * 否则"搜索后直接发"和"模型挑选后发"会出现两套行为，改一处忘一处就会不一致。
 *
 * @param {object} ctx
 * @param {Array<object>} items 已经筛好、要去发的结果
 * @param {string} keyword 关键词（卡片标题用）
 * @returns {Promise<{content: string, isError?: boolean}>}
 */
async function sendItems(ctx, items, keyword) {
  const s = settings();
  const [kind] = String(ctx.chatKey).split(':');
  const isGroup = kind === 'group';
  const shouldForward = isGroup ? s.groupForward : s.privateForward;
  try {
    if (shouldForward && isGroup) {
      // 单卡片模式：全部结果塞一张卡，靠 depth3 的内层节点承载所有条目。
      // 为什么默认开：拆成多张卡时，群友看到的是一串"第1/5张"，要点开五次，
      // 而且要判断哪张有什么；一张卡点一次就能顺着看下去。
      // 拆卡的唯一理由是单卡太重（SnowLuma 上传节点多会慢/失败），
      // 所以超过 singleCardMax 条时仍会自动拆。
      // 关于嵌套层数：早期这里写死过 depth=3（理由是"单卡要装很多条"），
      // 那个理由是错的 —— depth=2 的结构是 [标题节点, 内容节点(children=全部条目)]，
      // 本来就能把任意多条挂在第二层（实测 12 条：顶层 2、第二层 12）。
      // 强行加一层只是多套个空壳（第二层只有 1 个子节点），
      // 既违背用户设的 nestDepth，又让展开时多一次点击。
      const useSingle = s.singleCard && items.length <= s.singleCardMax;
      const cards = useSingle ? [items] : groupByMedia(items, s.mediaPerCard);
      // 层数只由 nestDepth 决定：singleCard 只管"要不要拆卡"，不管层数。
      const depth = s.nestDepth;

      let sent = 0;
      for (let i = 0; i < cards.length; i++) {
        const page = i + 1;
        // 卡片之间留间隔：SnowLuma 每张卡片都要上传节点，连发容易失败
        if (i > 0) await new Promise((r) => setTimeout(r, 1200));
        await sendGroupCard(ctx, cards[i], {
          keyword,
          page,
          totalPages: cards.length,
          depth,
          mediaTotal: cards[i].reduce((sum, it) => sum + mediaCountOf(it), 0)
        });
        sent += 1;
      }
      const media = items.reduce((sum, it) => sum + mediaCountOf(it), 0);
      // 视频已经作为独立节点放进卡片里了（见 sender.js 的 videoNodes），
      // 不再需要卡片后补发 —— 群友点开卡片就能直接播放。
      const vidTotal = items.reduce(
        (n, it) => n + (it.videoDetails || []).filter((v) => v?.localPath).length, 0);
      const vidNote = vidTotal > 0 ? `，含 ${vidTotal} 个视频` : '';
      return {
        content: `已发出 ${sent} 张合并转发卡片，共 ${media} 条媒体（${items.length} 条结果）${vidNote}。不要复述卡片内容。`
      };
    }

    // 私聊：默认也走合并转发卡片（与群聊同构，点开一次看完）。
    // privateForward=false 时才退回"平铺直发"。
    if (s.privateForward) {
      const useSingle = s.singleCard && items.length <= s.singleCardMax;
      const cards = useSingle ? [items] : groupByMedia(items, s.mediaPerCard);
      // 层数只由 nestDepth 决定：singleCard 只管"要不要拆卡"，不管层数。
      const depth = s.nestDepth;
      let sent = 0;
      for (let i = 0; i < cards.length; i++) {
        if (i > 0) await new Promise((r) => setTimeout(r, 1200));
        await sendPrivateCard(ctx, cards[i], {
          keyword,
          page: i + 1,
          totalPages: cards.length,
          depth
        });
        sent += 1;
      }
      const mediaC = items.reduce((sum, it) => sum + mediaCountOf(it), 0);
      const vidTotalC = items.reduce(
        (n, it) => n + (it.videoDetails || []).filter((v) => v?.localPath).length, 0);
      const noteC = vidTotalC > 0 ? `，含 ${vidTotalC} 个视频` : '';
      return {
        content: `已发出 ${sent} 张合并转发卡片，共 ${mediaC} 条媒体（${items.length} 条结果）${noteC}。不要复述卡片内容。`
      };
    }

    // 私聊平铺（仅当你显式关掉 privateForward）
    await sendPrivateFlat(ctx, items, keyword);
    const media = items.reduce((sum, it) => sum + mediaCountOf(it), 0);
    return { content: `已发送 ${items.length} 条结果、${media} 条媒体。不要复述图片内容。` };
  } catch (err) {
    logFns.error('发送失败：', err?.message ?? err);
    const msg = String(err?.message ?? err);

    // ── 先分流"机器人本身发不出去"的情况 ────────────────────────────────
    // 这些和卡片结构无关，硬说成"节点写法不兼容"会让管理员白改配置。
    // 实测踩过：机器人在群 957649606 被踢了，报的是
    //   "send group message rejected: result=110 err=发送失败，你已被移出该群"
    // 当时提示却写着"转发节点写法不兼容"，方向完全错了。
    if (/移出该群|不在该群|重新加群|result=110/i.test(msg)) {
      return {
        content: `发不出去：机器人已经不在这个群里了（被移出或群解散）。让管理员把机器人重新拉进群再试。原始信息：${msg.slice(0, 120)}`,
        isError: true
      };
    }
    if (/禁言|muted|shut.?up|result=1[0-9]{2}$/i.test(msg) && /err=/.test(msg)) {
      return {
        content: `发不出去：机器人可能被禁言了。原始信息：${msg.slice(0, 120)}`,
        isError: true
      };
    }
    if (/风控|risk|freq|频繁|too many/i.test(msg)) {
      return {
        content: `发不出去：QQ 风控/频率限制，过几分钟再试。原始信息：${msg.slice(0, 120)}`,
        isError: true
      };
    }

    // 节点写法不兼容：给出可操作的切换提示
    if (/INVALID_FIELD|node|forward/i.test(msg)) {
      return {
        content: `卡片发送失败（多半是转发节点写法不兼容）。请管理员到技能设置把「转发节点写法」从 ${s.nodeSyntax} 切成另一种再试。原始错误：${msg.slice(0, 200)}`,
        isError: true
      };
    }
    return { content: `卡片发送失败：${msg.slice(0, 200)}`, isError: true };
  }
}

async function searchAndSend(ctx, args) {
  // ── X 搜索准入检查 ────────────────────────────────────────────────
  //
  // 2026-10-27 需求：涩涩搜索全部不走 X，只有"识图找人"和"群友明确点名推特"
  // 这两种情况才允许访问 X。原因：
  //   · X 有严格的账号限频（一个号搜几十次就要等 10-15 分钟）
  //   · X 必须配登录态（cookie），换个人部署就废
  //   · 涩涩内容在 cosplaytele / 游戏巴士 / 每日大赛 / FC2 / MissAV / Pornhub
  //     上更全，而且不需要账号
  //
  // 这里做最后一道闸：如果配置里关掉了 X 搜索（默认关），
  // 就直接拒绝并告诉调用方应该用哪个工具，避免模型反复试。
  const _s = settings();
  if (!_s.xSearchEnabled) {
    return {
      content: 'X 搜索当前已关闭（配置里 xSearchEnabled=false）。'
        + '\n涩涩内容请改用：find_video（cosplaytele/游戏巴士/每日大赛）、'
        + 'av_search（番号：FC2/MissAV/Pornhub）、album_search（每日大赛）。'
        + '\n只有群友**明确说要去推特搜**时，才让管理员开启 xSearchEnabled。'
        + '\n⚠️ 不要把这条当成"没货"转告群友，如实说明 X 搜索没开。',
      isError: false
    };
  }

  // 冷却 + 并发闸
  const _cd = checkCooldown(ctx, 'X 搜索');
  if (!_cd.ok) return { content: _cd.message, isError: false };
  await acquireSearchSlot();
  try {
    return await _searchAndSendInner(ctx, args);
  } finally {
    releaseSearchSlot();
    markSearchDone(ctx);
  }
}

/** searchAndSend 的实际实现（外面包了冷却 + 并发闸） */
async function _searchAndSendInner(ctx, args) {

  const s = settings();

  // 开关只有一处：config.skills['x-media-finder'].enabled（由核心 tool-registry
  // 的 getToolAvailability 统一判定）。这里**不再**自己读一个 enabled 字段 ——
  // 两处开关必然出现"UI 显示开着、实际被这里挡掉"的口径分裂。
  // 所以本函数被调到，就说明技能已启用。

  // author 参数优先：模型可以用 {author:'alice', keyword:'自慰视频'} 明确表达
  // "看这个博主的这类作品"，不必把博主名拼进 keyword 里再让 worker 去猜。
  const authorArg = String(args?.author ?? '').trim().replace(/^@+/, '');
  let keyword = String(args?.keyword ?? '').trim();
  if (authorArg) {
    keyword = keyword ? `${authorArg}的${keyword}` : `${authorArg}的新推文`;
  }
  if (!keyword) return { content: '缺少 keyword：要搜什么？', isError: true };
  if (keyword.length > 60) return { content: '关键词太长了（最多 60 字）。', isError: true };

  // 「不要ai / 要亚洲的」这类说法**在 worker 里解析**，不在 JS 里再实现一份。
  // 理由：解析规则和过滤逻辑必须放在一起，否则两边改一处忘一处就会行为不一致
  // （本会话已经因为"两处定义"踩过 parse_custom_intent 被旧版本覆盖的坑）。
  // 所以这里原样把 keyword 传给 worker，由它拆出意图再搜索。

  // ── 安全闸：未成年指向词直接拒 ────────────────────────────────────────
  const bad = hitsDenyKeyword(keyword, s.denyKeywords);
  if (bad) {
    return {
      content: `「${keyword}」命中拦截词「${bad}」，这个不给搜。换个关键词。`,
      isError: true
    };
  }

  const [kind] = String(ctx.chatKey).split(':');
  const isGroup = kind === 'group';
  if (isGroup) {
    const [, gid] = String(ctx.chatKey).split(':');
    if (!s.groupAllowlist.length) {
      return {
        content: '本群未开通这个功能（群聊默认关闭）。需要的话让管理员在技能设置里把群号填进「允许使用的群号」。',
        isError: true
      };
    }
    if (!s.groupAllowlist.includes(String(gid))) {
      return { content: '本群未开通这个功能。', isError: true };
    }
  }

  if (!pythonOrWarn()) {
    return {
      content: '服务端没找到 Python，搜索跑不起来。请管理员在技能设置里填「Python 解释器路径」。',
      isError: true
    };
  }

  // ── 预检：环境没就绪就别先喊"正在找货"了 ──────────────────────────────
  // 体验问题：原来提示语写在搜索前，所以"没登录账号"这种必然失败的情况
  // 也会先发一句「正在全网找货」，然后才报错——群友白等一场。
  // 预检结果带缓存，健康环境下不会给每次请求都加一次 Python 启动开销。
  const dbPath = accountsDbPath();
  const pf = await preflight(dbPath);
  if (!pf.ok) {
    logFns.error(`环境预检未通过（${dbPath}）：${pf.reason}`);
    // 预检的 reason 自带句号（来自 worker 的完整句子），这里不要再硬加，
    // 否则会出现「内容」。。请管理员…」这种双句号。
    const reason = String(pf.reason || '').trim().replace(/[。.！!]+$/, '');
    return {
      content: `搜索还没配好：${reason}。管理员可跑「X 搜索自检」看详情。`,
      isError: true
    };
  }

  // ── 先给群友一句反馈 ─────────────────────────────────────────────────
  await sendSearchHint(ctx);

  // ── 搜索 ─────────────────────────────────────────────────────────────
  // 媒体类型：工具参数优先（群友当场说"找视频 cosplay"），否则用配置默认值。
  const mediaType = ['image', 'video', 'mix'].includes(String(args?.mediaType || '').toLowerCase())
    ? String(args.mediaType).toLowerCase()
    : s.mediaType;

  // 检索模式：工具参数优先（群友当场说"看看某人的新推文"），否则用配置默认。
  // 显式传了 author 参数时，等于宣布"这是按博主查"。
  let searchMode = ['auto', 'search', 'author'].includes(String(args?.searchMode || '').toLowerCase())
    ? String(args.searchMode).toLowerCase()
    : s.searchMode;
  if (authorArg) searchMode = 'author';

  const task = {
    keyword,
    extraTags: s.extraTags,
    expandTags: s.expandTags,
    tagMode: s.tagMode,
    maxResults: s.maxResults,
    minLikes: s.minLikes,
    accountsDb: dbPath,
    proxy: s.proxy,
    inlineImages: s.inlineImages,
    mediaType,
    qualityBoost: s.qualityBoost,
    maxPerAuthor: s.maxPerAuthor,
    adultOnly: s.adultOnly,
    adultStrict: s.adultStrict,
    region: (args?.region && String(args.region).trim()) || s.region,
    // 只传配置里的静态值；口头说的由 worker 从 keyword 里解析后自行合并
    blockTags: s.blockTags,
    blockWords: s.blockWords,
    preferTags: s.preferTags,
    preferWords: s.preferWords,
    blockAi: s.blockAi,
    relevanceFilter: s.relevanceFilter,
    // 模型给的别名（中文/日文/英文写法），worker 用它做相关性匹配
    includeTerms: Array.isArray(args?.includeTerms)
      ? args.includeTerms.map((x) => String(x || '').trim()).filter(Boolean).slice(0, 12)
      : [],
    searchMode,
    retries: s.retries,
    backoffSeconds: s.backoffSeconds,
    __timeoutMs: s.timeoutMs
  };

  const result = await runWorker(task, (ev) => {
    if (ev.t === 'progress' && ev.note) logFns.log(`[xsearch] ${ev.stage} ${ev.done}/${ev.total} ${ev.note}`);
  });

  // 搜索阶段暴露的环境问题（预检通过后才可能出现的，例如账号中途失效）
  // 要让下一次预检重新判断，所以清缓存。
  if (!result.ok && String(result.code || '').startsWith('NO_ACCOUNTS')) {
    invalidatePreflight();
  }

  if (!result.ok) {
    // 按错误码分流：环境/配置问题必须说清，不能糊成"没货"——
    // 否则群友会一直换关键词，而问题根本不在关键词上。
    const code = String(result.code || '');
    logFns.error(`搜索失败[${code || 'ERROR'}]：`, result.error);

    // 需要管理员动手的：直接给可操作指引
    if (code === 'NO_ACCOUNTS_DB' || code === 'NO_ACCOUNTS') {
      return {
        content: `搜索还没配好：${result.error}`,
        isError: true
      };
    }
    // 暂时性故障：告诉用户"等会儿再试"，而不是"没货"
    if (code === 'RATE_LIMIT') {
      // ⚠️ 措辞要写死"不是卡片问题"。
      // 实测：工具只回"搜索超时"，模型会自己编成"卡片发不出去，通道卡住了"，
      // 群友以为机器人坏了。这里把真实原因说清楚。
      return {
        content: 'X（推特）那边限频了：一个账号搜太多次，要等 10-15 分钟才恢复。'
          + '\n这不是卡片问题，也不是写法不兼容 —— 是推特暂时不给搜了。'
          + '\n等一会儿再试，或者让管理员多加几个搜索账号（能快 3-5 倍）。'
          + '\n⚠️ 如实转告群友，不要编"卡片坏了"这种理由。',
        isError: true
      };
    }
    if (code === 'TIMEOUT' || code === 'SPAWN_FAILED' || code === 'NO_PYTHON' || code === 'NO_RESULT') {
      return {
        content: `搜索暂时不可用：${result.error}`,
        isError: true
      };
    }
    // 真正的搜索失败/无结果
    return { content: '没货，换个关键词试试', isError: false };
  }

  // ── 跨次去重 ─────────────────────────────────────────────────────────
  // 同一个会话（私聊某人 / 某个群）不重复收到同一批内容。
  // 去重在发送**之前**做，所以要先把"这次发什么"定下来再发。
  let items = Array.isArray(result.items) ? result.items : [];

  if (s.dedupe && items.length) {
    const store = new SeenStore(ctx.chatKey, {
      ttlMs: s.dedupeTtlDays * 24 * 60 * 60 * 1000,
      maxPerChat: s.dedupeMax
    });

    const { fresh, skipped } = store.filterFresh(items);
    if (skipped) {
      logFns.log(`[xsearch] 去重：跳过 ${skipped} 条本会话已发过的内容（剩余 ${fresh.length} 条）`);
    }

    // 全被去重掉了 → 补搜一轮，别让用户空手而归。
    // 为什么要补：热门内容就那么十几条，连搜两次第二次可能全被挡掉，
    // 直接回"没货"是假象（其实有货，只是都发过）。
    if (!fresh.length && s.dedupeRefill) {
      logFns.log('[xsearch] 全部命中历史，补搜一轮…');
      const more = await runWorker({ ...task, maxResults: s.maxResults * 2 }, null);
      if (more.ok && Array.isArray(more.items)) {
        const second = store.filterFresh(more.items);
        fresh.push(...second.fresh);
        if (second.fresh.length) {
          logFns.log(`[xsearch] 补搜得到 ${second.fresh.length} 条新内容`);
        }
      }
    }

    items = fresh;
    // 先记账再发送：即使发送中途失败，也不会导致同一批内容被重复推送
    // （宁可少发，也不要同一个群刷两遍）。
    store.addAll(items);
    store.save();

    if (!items.length) {
      return {
        content: '这个会话里最近发过的内容还没到可重发的时候（默认 7 天）。换个关键词，或让管理员调小「去重保留天数」。',
        isError: false
      };
    }
  }

  if (!items.length) {
    return { content: '没货，换个关键词试试', isError: false };
  }

  // ── 预览模式：只返回候选清单，让模型二次识别 ──────────────────────────
  // 触发条件：工具参数 previewOnly=true，或配置开了 modelSecondPass。
  // 这样做的价值：tag 匹配有噪音（#cosplay 到处都是），模型读文字/标签
  // 再筛一遍能显著减少"内容不相干"。代价是多一轮对话。
  const wantPreview = args?.previewOnly === true
    || (s.modelSecondPass && args?.skipPreview !== true);
  if (wantPreview) {
    const MAX_PREVIEW = 20;
    const candidates = items.slice(0, MAX_PREVIEW);
    // 给模型看标签：从 _tweet.hashtags 拿不到（worker 侧对象），
    // 所以让 worker 另行回传；这里用 text 兜底
    putPending(ctx.chatKey, {
      items: candidates,
      keyword,
      mediaType,
      all: items
    });
    const lines = candidates.map((it, i) => renderCandidate(i + 1, it));
    return {
      content: [
        `找到 ${items.length} 条候选，**还没发送**。请判断哪些与「${keyword}」真正相关：`,
        '',
        ...lines,
        '',
        `然后调用 x-media-finder__send_picked，indices 填你要发的编号（例如 "1,3,5"）。`,
        `全部都要就填 "all"；都不合适就填 "none"（会回一句没货）。`,
        `判断依据：作者名、原文、标签是否真的在说「${keyword}」；小猫、新闻、无关 AI 图都该排除。`
      ].join('\n')
    };
  }

  // ── 发送 ─────────────────────────────────────────────────────────────
  return await sendItems(ctx, items, keyword);
}

// ── 生命周期 ────────────────────────────────────────────────────────────────

export function setup(api) {
  cfg = api.config;
  logFns = {
    log: (...a) => api.log(...a),
    warn: (...a) => api.warn(...a),
    error: (...a) => api.error(...a)
  };

  // ── 主工具：搜索 + 发送 ──────────────────────────────────────────────
  api.registerTool({
    id: 'search_media',
    name: 'X 媒体搜索',
    description: '按关键词搜 X/Twitter 上的图片与视频。'
      + '⚠️ **只在群友明确要求搜推特时才调用**（如「搜推特的」「X上找找」「去推特搜」）。'
      + '群友说「找涩涩的」「找片」「搜点图」但没有点名推特时，'
      + '**应该用 find_video / av_search / album_search**，不要用这个工具 —— '
      + '涩涩内容默认不走 X（X 有账号限频，而且需要登录态）。',
    category: 'media',
    icon: '🔞',
    parameters: {
      type: 'object',
      properties: {
        keyword: {
          type: 'string',
          description: '搜索关键词，例如 cosplay、自撮り、猫。不要带 filter: 之类的搜索语法，工具会自己加。'
        },
        mediaType: {
          type: 'string',
          enum: ['mix', 'image', 'video'],
          description: '要哪种媒体。群友说「找视频」「要视频」填 video；说「要图」「找图」「来点图」填 image；没特别说就留空（用默认 mix，图片视频各一半）。'
        },
        searchMode: {
          type: 'string',
          enum: ['auto', 'search', 'author'],
          description: '检索方式。群友说「看看某人的新推文」「想看某人的自慰视频」（在问某个具体博主的作品）时填 author；说「想看 xxx 这类内容」时填 search；拿不准就留空（auto 会自动判断）。'
        },
        author: {
          type: 'string',
          description: '仅 author 模式用：博主的 X 用户名（不带 @，例如 alice）。填了它就不用把博主名塞进 keyword。'
        },
        region: {
          type: 'string',
          enum: ['asia', 'japan', 'china', 'western', 'any'],
          description: '地区偏好，按文字系统判定（不是按人种）：asia=日文+中文、japan=日文、china=中文、western=拉丁字母、any=不限。群友说「不要欧美」「要亚洲的」「只要日本的」时按此填；没提就留空用配置值。'
        },
        includeTerms: {
          type: 'array',
          items: { type: 'string' },
          description: '关键词的**其他写法**。如果 keyword 是人名/角色名/作品名，把你想到的中文、日文、英文别名都填进来（最多 12 个），工具用它们判断相关性，避免混进无关内容。例：keyword=奥黛塔 时填 ["オデット","Odette","odette"]。普通词汇（如 cosplay）不用填。'
        }
      },
      required: ['keyword']
    },
    async execute(ctx, args) {
      try {
        return await searchAndSend(ctx, args);
      } catch (err) {
        logFns.error('search_media 未捕获异常：', err?.stack ?? err);
        return { content: `搜索出错：${err?.message ?? err}`, isError: true };
      }
    }
  });

  // ── 模型二次识别：发送挑选后的候选 ──────────────────────────────────
  api.registerTool({
    id: 'send_picked',
    name: '发送挑选的结果',
    description: '配合 search_media(previewOnly=true) 使用。把候选清单里你认为相关的那几条真正发出去。indices 填编号，如 "1,3,5"；全都要填 "all"；都不要填 "none"。',
    category: 'media',
    icon: '📤',
    parameters: {
      type: 'object',
      properties: {
        indices: {
          type: 'string',
          description: '要发送的候选编号，逗号分隔（如 "1,3,5"），或 "all" / "none"。'
        }
      },
      required: ['indices']
    },
    async execute(ctx, args) {
      try {
        const pend = takePending(ctx.chatKey);
        if (!pend) {
          return { content: '没有待发送的候选（可能超时了）。重新搜一次吧。', isError: true };
        }
        const raw = String(args?.indices ?? '').trim().toLowerCase();
        if (!raw || raw === 'none') {
          pendingPicks.delete(String(ctx.chatKey));
          return { content: '没货，换个关键词试试', isError: false };
        }
        let picked;
        if (raw === 'all') {
          picked = pend.items;
        } else {
          const want = new Set(
            raw.split(/[,，\s]+/).map((x) => Number.parseInt(x, 10)).filter((n) => Number.isFinite(n) && n > 0)
          );
          picked = pend.items.filter((_, i) => want.has(i + 1));
        }
        if (!picked.length) {
          return { content: `你给的编号（${raw}）对不上任何候选。可用范围 1-${pend.items.length}。`, isError: true };
        }
        pendingPicks.delete(String(ctx.chatKey));
        // 复用正常发送流程
        return await sendItems(ctx, picked, pend.keyword);
      } catch (err) {
        logFns.error('send_picked 失败：', err?.stack ?? err);
        return { content: `发送失败：${err?.message ?? err}`, isError: true };
      }
    }
  });

  // ── 站点搜索（cosplaytele 等）────────────────────────────────────────
  api.registerTool({
    id: 'site_search',
    name: '站点图集搜索',
    description: '在 cosplaytele.com 按关键词搜 cosplay 图集/视频。返回标题和链接（标题里会写明 "N photos and M videos"）。要下载视频再用 site_video。',
    category: 'media',
    icon: '🔎',
    parameters: {
      type: 'object',
      properties: {
        keyword: { type: 'string', description: '关键词，如 furina、genshin、raiden' },
        limit: { type: 'number', description: '返回几条，默认 8，最多 20' }
      },
      required: ['keyword']
    },
    async execute(ctx, args) {
      const kw = String(args?.keyword ?? '').trim();
      if (!kw) return { content: '要搜什么？给个关键词。', isError: true };
      const limit = Math.min(20, Math.max(1, Number(args?.limit) || 8));
      const cd = checkCooldown(ctx, 'cosplaytele 搜索');
      if (!cd.ok) return { content: cd.message, isError: false };
      await sendSearchHint(ctx);
      await acquireSearchSlot();
      let r;
      try {
        r = await runGrab({ mode: 'search', keyword: kw, limit,
          __timeoutMs: GRAB_SEARCH_TIMEOUT_MS }, null);
      } finally {
        releaseSearchSlot();
        markSearchDone(ctx);
      }
      if (!r.ok) {
        if (r.code === 'NO_RESULT') return { content: '没搜到，换个词试试', isError: false };
        return { content: `搜索失败：${r.error}`, isError: true };
      }
      const lines = (r.items || []).map((it, i) =>
        `${i + 1}. ${it.title}\n   ${it.url}`);
      return {
        content: [
          `cosplaytele 搜到 ${r.count} 条：`,
          '',
          ...lines,
          '',
          '要下载哪个的视频，用 site_video 并把 url 填进去。'
        ].join('\n')
      };
    }
  });

  // ── 统一搜索（自动路由：按说法决定搜哪个站）──────────────────────────
  api.registerTool({
    id: 'find_video',
    name: '找视频（自动选站）',
    description: '按群友的说法自动选站点搜视频。提到「每日大赛」→ 搜每日大赛；关键词含 cos → cosplaytele + 推特（cosplaytele 优先）；其他中文词 → 每日大赛。中文角色名会自动转成英文（刻晴→keqing、雷电将军→raiden shogun），因为 cosplaytele 是英文站。',
    category: 'media',
    icon: '🎯',
    parameters: {
      type: 'object',
      properties: {
        keyword: { type: 'string', description: '群友原话里的关键词，中文也可以（如「雷电将军的cos」「每日大赛 自慰」）' },
        limit: { type: 'number', description: '每个站返回几条，默认 3，最多 20。填 0 表示该站有多少给多少（群友说「都要」「非常多」时用）' },
        more: { type: 'boolean', description: '群友说「换一批」时填 true —— 会排除上一次已经发过的条目，往下翻' },
        focus: { type: 'string', description: '群友指定平台时填：cosplaytele / gamebus / album / twitter。说「多找cosplaytele的」就填 cosplaytele；没说就不填' }
      },
      required: ['keyword']
    },
    async execute(ctx, args) {
      const kw = String(args?.keyword ?? '').trim();
      if (!kw) return { content: '要找什么？', isError: true };
      // 每站默认 3 条（用户要求"每个平台都选几个"）。相关性过滤会砍掉一部分，
      // 所以 worker 侧会多抓一些再筛。
      // limit=0 → 该站有多少给多少（群友说「都要」「非常多」时用）
      const rawLimit = Number(args?.limit);
      const limit = rawLimit === 0 ? 0 : Math.min(20, Math.max(1, rawLimit || 3));
      const more = args?.more === true || args?.more === 'true';
      const focus = String(args?.focus ?? '').trim().toLowerCase();
      // 先看图片通道最近是否刚失败过 —— 搜索要 20+ 秒（抓封面），
      // 提前告知比搜完才发现发不出去好。不发任何探测消息。
      const chk = selfcheckImageChannel(ctx);
      if (!chk.ok) {
        return {
          content: `⚠️ 图片通道可能受限：${chk.reason}\n`
            + `可以先等一会儿再试，或用 site_search（不带封面）先看列表。`,
          isError: false
        };
      }
      const cd = checkCooldown(ctx, '多站搜索');
      if (!cd.ok) return { content: cd.message, isError: false };
      await sendSearchHint(ctx);
      // 「换一批」：把这次会话里已经发过的 URL 交给 worker 排除，往下翻
      const seenKey = 'find_video_shown';
      const seen = more ? (shownUrls.get(ctx.chatKey) || new Set()) : new Set();
      await acquireSearchSlot();
      let r;
      try {
        r = await runGrab({ mode: 'route', keyword: kw, limit,
          cosLimit: limit, perSite: limit,
          focus: focus || undefined,
          exclude: seen.size ? [...seen].slice(-300) : undefined,
          __timeoutMs: GRAB_SEARCH_TIMEOUT_MS }, null);
      } finally {
        releaseSearchSlot();
        markSearchDone(ctx);
      }
      if (!r.ok) {
        if (r.code === 'NO_RESULT') {
          // ⚠️ 这里必须给出**具体**的下一步，否则模型会自己乱拆词重试。
          // 实测 2026-10-27：用户说「帮我找cos的涩涩」，模型先搜 cosplay → 0 条，
          // 再搜 cos → 0 条，然后连试 11 次（中间还插了 selfcheck），
          // 全程只回了群友一句「等一下 站点有冷却」。
          //
          // 根因：错误提示只写「换个说法试试」，模型不知道往哪个方向换，
          // 就只能穷举。现在明确告诉它：要么要具体角色名，要么就是网络问题。
          return {
            content: '站点没返回结果。两种可能，按顺序判断：\n'
              + '\n① 关键词太宽泛。「cos」这种词各站都搜不出东西，'
              + '必须给**具体角色名或作品名**（如「胡桃」「芙宁娜」「碧蓝航线」）。'
              + '\n   如果群友只说「找cos」，反问一句「想看哪个角色」，不要自己反复试。'
              + '\n\n② 网络/代理抖了。这时三个站会同时返回 0 条。'
              + '可以调 selfcheck 看代理通不通，但**不要连续重试超过 1 次**——'
              + '每次重试都要等冷却，群友会干等。'
              + '\n\n⚠️ 最多再试 1 次。还不行就如实告诉群友「站点暂时抓不到」，'
              + '不要反复换关键词刷屏。',
            isError: false
          };
        }
        if (r.code === 'NETWORK_DOWN') {
          // 站点连不上：**明确告诉群友是网络问题**，不要让模型编成"没货"
          return {
            content: '站点连不上（代理挂了或网络抖动）。'
              + '\n这不是关键词的问题，也不是卡片的问题 —— 是抓取通道断了。'
              + '\n告诉群友「站点暂时连不上，过会儿再试」，'
              + '**不要换关键词重试**（换词也没用，反而让群友以为自己的词不对）。',
            isError: false
          };
        }
        if (r.code === 'KEYWORD_TOO_BROAD') {
          return {
            content: r.error + '。\n'
              + '需要**具体角色名或作品名**，例如「胡桃」「芙宁娜」「碧蓝航线」。'
              + '\n如果群友只说了「找cos」「找涩涩」这种笼统的话，'
              + '**反问一句想看哪个角色**，不要自己反复试探。'
              + '\n⚠️ 最多再试 1 次，不要连续换词。',
            isError: false
          };
        }
        if (r.code === 'NO_RELEVANT') {
          // 过滤后一条不剩 —— 必须如实说，不能把无关内容凑数发出去
          if (more) {
            shownUrls.delete(ctx.chatKey);
            return { content: `${r.error}。
已经把"换一批"的记录清空了，可以再试一次。`, isError: false };
          }
          return { content: `${r.error}。
可以换个说法（用角色的中文名/英文名），或指定平台（cosplaytele / gamebus / 每日大赛）。`, isError: false };
        }
        return { content: `搜索失败：${r.error}`, isError: true };
      }
      const plan = r.plan || {};
      // 候选缓存统一在下面排序后存一次（见 putSiteCandidates(sorted)）。
      // 早期这里先存了一次原始顺序，后面又存排序后的 —— 两次调用多余且容易
      // 让"编号"和"缓存顺序"不一致，直接删掉这次。
      // ⚠️ 编号必须**全局连续**，而且顺序要与 pend.items 完全一致 ——
      // 之前按站点分别从 1 开始数，导致三个站点都有"1"，用户说"下 1"
      // 时 pick_download 用全局下标取值，就会下到别的站的内容。
      // 现在：先把 items 按展示顺序（cosplaytele 优先）重排，编号即数组下标+1。
      const items = r.items || [];
      // ── 推特限流降级（D）──────────────────────────────────────────────
      // route 模式的推特部分是 index.js 另调的；如果那边限流，
      // worker 会在 result 里带 twitterDegraded 标记，这里照常用其他站的结果，
      // 只是额外提醒一句，不让整次搜索失败。
      const twDegraded = r.twitterDegraded === true;
      const order = ['cosplaytele', '游戏巴士', '每日大赛', '其他'];
      const rank = (it) => {
        const idx = order.indexOf(it.site || '其他');
        return idx < 0 ? order.length : idx;
      };
      const sorted = items
        .map((it, i) => ({ it, i }))
        .sort((a, b) => rank(a.it) - rank(b.it) || a.i - b.i)
        .map((x) => x.it);
      // 存快照：顺序即展示顺序，代号写进文案
      const snapId = putSiteCandidates(ctx.chatKey, { items: sorted, keyword: kw, site: 'multi', plan });

      // ⚠️ 必须**真的把卡片发出去**。
      // 早期这里只返回文字，模型会拿文字自己 send_message 转述，
      // 结果标题被压缩成「官宣领证那条」、图片和详情全丢了 ——
      // 用户看到的就是"A1 A2 没有标题图片详情"。
      // 记录本次展示过的 URL（「换一批」要排除它们）
      if (!shownUrls.has(ctx.chatKey)) shownUrls.set(ctx.chatKey, new Set());
      const shown = shownUrls.get(ctx.chatKey);
      for (const it of sorted) if (it.url) shown.add(String(it.url));
      if (shown.size > 500) {
        // 只保留最近 500 条，避免无限增长
        const arr = [...shown].slice(-500);
        shownUrls.set(ctx.chatKey, new Set(arr));
      }

      const note = `路由：${(plan.sites || []).join(' + ')} · 搜索词「${plan.keyword || kw}」`
        + (r.coverCount === 0 ? ' · ⚠️ 预览图未抓到' : '')
        + (twDegraded ? ' · 推特限频，已跳过' : '');
      let out;
      try {
        out = await sendSiteCandidates(ctx, sorted, { note, snapId });
      } catch (err) {
        return {
          content: `搜到 ${sorted.length} 条，但发送卡片失败：${String(err?.message ?? err).slice(0, 150)}。`
            + `候选已存为快照 ${snapId}，群友说「下 ${snapId}1」仍可下载。`,
          isError: true
        };
      }
      // 注意措辞：卡片失败时**什么都没发**（不做裸发兜底），
      // 不能说"已通过逐条消息发出"，那会误导模型以为已经送达。
      const via = out?.viaCard
        ? '合并转发卡片（一层）'
        : `❌没有发出（卡片通道失败：${String(out?.cardError || '未知').slice(0, 80)}）`;
      if (!out?.viaCard) {
        return {
          content: `搜索到 ${sorted.length} 条（快照 ${snapId}），但卡片发送失败，群友什么都没收到。`
            + `原因：${String(out?.cardError || '未知').slice(0, 120)}。`
            + `可以稍等几秒让模型重试一次（QQ 有时会瞬时限流）。`
            + `⚠️ 绝对不要用 send_message 把条目内容发出去——内容只能出现在卡片里。`,
          isError: true
        };
      }
      return {
        content: `已通过${via}把 ${sorted.length} 条候选（快照 ${snapId}，含封面+标题+链接）发给群友。`
          + `群友回复编号（如「下 ${snapId}3」）后调 x-media-finder__pick_download。`
          + `\n你**可以**用 send_message 接一句很短的话（1-8 字，像"喏，自己翻""这批行不行"），`
          + `但**不要复述卡片内容**（不要重念标题/编号/条数）。`
          + `\n也可以什么都不说——但如果你想说，就必须调 send_message，光写正文发不出去。`
      };
    }
  });

  // ── 按编号下载（配合候选列表）────────────────────────────────────────
  api.registerTool({
    id: 'pick_download',
    name: '按编号下载',
    description: '群友看过候选列表后回复编号（如「下 A3」「A1,A4」「all」），用这个工具下载对应视频。编号必须带快照代号（A/B/C…），否则可能取到之后新搜的列表。自动判断站点并下载，完成后包进一层合并转发。',
    category: 'media',
    icon: '⬇️',
    parameters: {
      type: 'object',
      properties: {
        indices: {
          type: 'string',
          description: '要下载的编号，必须带快照代号，如 "A3" 或 "A1,A3"；也可 "all" / "none"。代号来自候选列表标题（【A】/【B】…）。'
        },
        force: {
          type: 'boolean',
          description: '群友明确说「重发」时才填 true（绕过"10 分钟内不重复发同一视频"的去重）'
        }
      },
      required: ['indices']
    },
    async execute(ctx, args) {
      const raw0 = String(args?.indices ?? '').trim();
      if (!raw0 || raw0.toLowerCase() === 'none') {
        pendingSites.delete(String(ctx.chatKey));
        return { content: '好，那就不下了。', isError: false };
      }

      // 解析编号：支持 "3" / "1,3" / "A3" / "A1,A3" / "all"
      // 带字母前缀时明确指定第几个快照（A=最早，最新的是最后那个代号）；
      // 不带字母就用**最新**快照，保留「下 3」这种简单说法。
      const raw = raw0.toLowerCase();
      const wants = [];        // [{snap:'A'|'', n:3}]
      for (const part of raw.split(/[,，\s]+/).filter(Boolean)) {
        if (part === 'all') { wants.push({ snap: '', n: 0, all: true }); continue; }
        const m = part.match(/^([a-z])?(\d+)$/);
        if (m) wants.push({ snap: (m[1] || '').toUpperCase(), n: Number.parseInt(m[2], 10) });
      }
      if (!wants.length) {
        return { content: `看不懂这个编号：${raw0}。可以说「下 3」或「下 A3」。`, isError: true };
      }

      // 按快照分组取
      const bySnap = new Map();   // snapId -> [numbers]
      for (const w of wants) {
        const snap = takeSiteCandidates(ctx.chatKey, w.snap);
        if (!snap) {
          if (!pendingSites.has(String(ctx.chatKey))) loadSnapshots();
          const avail = (pendingSites.get(String(ctx.chatKey)) || []).map((x) => x.snapId);
          return {
            content: w.snap
              ? `找不到快照 ${w.snap}（可用：${avail.join('/') || '无'}）。若刚重载过技能，请重搜一次。`
              : '没有待选列表（30 分钟内有效）。重新搜一次吧。',
            isError: true
          };
        }
        const key = snap.snapId;
        const arr = bySnap.get(key) || { snap, nums: [] };
        if (w.all) arr.all = true;
        else arr.nums.push(w.n);
        bySnap.set(key, arr);
      }

      const picked = [];
      const picksEchoParts = [];
      for (const { snap, nums, all } of bySnap.values()) {
        const sel = all ? snap.items.slice(0, 3) : snap.items.filter((_, i) => nums.includes(i + 1));
        for (const it of sel) {
          const idx = snap.items.indexOf(it) + 1;
          picked.push(it);
          picksEchoParts.push(`【${snap.snapId}${idx}】${String(it.title || '').slice(0, 56)}`);
        }
      }
      if (!picked.length) {
        const all = pendingSites.get(String(ctx.chatKey)) || [];
        const avail = all.map((x) => `${x.snapId}(${x.items.length}条)`).join(' ');
        const detail = all
          .map((x) => `${x.snapId}: 共${x.items.length}条, ${Math.round((Date.now() - x.at) / 1000)}秒前`)
          .join(' | ');
        logFns.warn?.(`pick_download 编号对不上：请求「${raw0}」，可用快照 [${detail || '无'}]`);
        return {
          content: `编号对不上。可用快照：${avail || '无'}` +
            (all.length ? `（${detail}）` : '。列表可能已超时（30 分钟），请重新搜索。'),
          isError: true
        };
      }
      // 回显这次实际要下的是哪几条（带快照代号）——
      // 群友能立刻发现"这不是我选的那条"，比下完才发现好。
      const picksEcho = picksEchoParts.join('\n');
      // ── 只解析地址，不下载 ──────────────────────────────────────────
      //
      // 2026-10-27 需求变更：「不用下载视频文件发送，给出视频地址即可」。
      //
      // 原来的做法是后台下载完整 mp4 → 作为 video 段发卡片。问题：
      //   · 磁盘：一个视频几百 MB，几个人下就撑满
      //   · 带宽：服务器 → QQ 要传一遍完整文件
      //   · CPU：需要 ffmpeg 转码/压缩
      //   · 风险：等于在服务器上留存并分发完整内容
      //
      // 现在只做「解析地址 → 发地址卡片」，秒级完成，不起下载任务。
      const cd = checkCooldown(ctx, '解析地址');
      if (!cd.ok) return { content: cd.message, isError: false };

      const s = settings();
      const links = [];     // [{title, pageUrl, hls, referer, site, duration, cover}]
      const failed = [];

      // ── 按群决定发送方式 ────────────────────────────────────────────
      // 有的群希望直接收到视频文件（方便），有的群只要链接（省流量/合规）。
      // 配置见 videoModeByGroup，例如 "123456789:file,987654321:link"。
      const vm = resolveVideoMode(ctx);
      const wantFile = vm.mode === 'file';
      logFns.log?.(`pick_download 发送方式：${vm.mode}（${vm.reason}）`);

      // ── 发文件模式：必须走后台，不能同步等 ──────────────────────────
      //
      // 实测：gamebus 一个视频下载 5-20 分钟。如果同步等，工具调用不返回，
      // 整轮对话卡死（群友再 @ 没反应），而且超过 QQ 侧的工具超时就被判失败。
      //
      // 所以这里立刻返回"已开始下载"，下载完由后台自己发卡片。
      if (wantFile) {
        const s2 = settings();
        const bg = runInBackground(ctx, `pickdl:${picked.length}:${Date.now()}`,
          `下载 ${picked.length} 个视频`, async (onP) => {
            const fileRes = [];
            const linkFallback = [];
            const errs = [];
            for (let seq = 0; seq < picked.length; seq += 1) {
              const it = picked[seq];
              const u = String(it.url || '');
              const isGamebus = /gamebus001\.cc\/video\//i.test(u)
                || (it.site === '游戏巴士' && it.id && !u);
              const isAlbum = /\/archives\/\d+/i.test(u)
                || /sumjpknqx|tadqmtxhg|zwztjllvs/i.test(u);
              const isCosplaytele = /cosplaytele\.com/i.test(u);
              const isAv = it.site === 'av';
              const maxMB = Number(s2.videoMaxSendMB) || 150;
              let dl = null;
              try {
                if (isGamebus && it.id) {
                  dl = await runGrab({ mode: 'gamebus_video', videoId: it.id,
                    maxSendMB: maxMB, cacheDays: s2.videoCacheDays,
                    cacheMaxGB: s2.videoCacheMaxGB }, onP);
                } else if (isAlbum) {
                  const wi = Number.isFinite(Number(it.videoIndex)) ? Number(it.videoIndex) : 0;
                  dl = await runGrab({ mode: 'album_video', url: u,
                    maxVideos: wi + 1, maxSendMB: maxMB,
                    cacheDays: s2.videoCacheDays, cacheMaxGB: s2.videoCacheMaxGB }, onP);
                  if (dl && dl.ok && Array.isArray(dl.videos) && dl.videos.length) {
                    const v = dl.videos[Math.min(wi, dl.videos.length - 1)];
                    dl = { ok: true, localPath: v.localPath, bytes: v.bytes,
                           compressed: v.compressed, title: v.title,
                           duration: 0, cached: v.cached };
                  }
                } else if (isCosplaytele) {
                  dl = await runGrab({ mode: 'video', url: u,
                    maxSendMB: maxMB, cacheDays: s2.videoCacheDays,
                    cacheMaxGB: s2.videoCacheMaxGB }, onP);
                } else if (isAv && u) {
                  dl = await runGrab({ mode: 'av_video', url: u,
                    maxSendMB: maxMB, cacheDays: s2.videoCacheDays,
                    cacheMaxGB: s2.videoCacheMaxGB }, onP);
                }
              } catch (e) {
                errs.push(`第${seq + 1}条异常：${String(e?.message ?? e).slice(0, 60)}`);
              }

              if (dl && dl.ok && dl.localPath) {
                const bytes = Number(dl.bytes) || 0;
                if (bytes > maxMB * 1048576) {
                  linkFallback.push({ title: String(it.title || '').slice(0, 140),
                    pageUrl: u, site: it.site || '',
                    note: `文件 ${Math.round(bytes / 1048576)}MB 超过上限 ${maxMB}MB，只给地址` });
                } else {
                  fileRes.push({ localPath: dl.localPath, bytes,
                    title: dl.title || String(it.title || '').slice(0, 140),
                    compressed: dl.compressed, cached: dl.cached,
                    duration: Number(dl.duration) || 0, note: u });
                }
              } else {
                linkFallback.push({ title: String(it.title || '').slice(0, 140),
                  pageUrl: u, site: it.site || '',
                  note: `下载失败（${dl?.error || dl?.code || '超时'}），只给地址` });
              }
            }
            return { fileRes, linkFallback, errs };
          }, async (r) => {
            // 后台完成回调：先发文件卡片，再发降级的链接卡片
            if (r.fileRes && r.fileRes.length) {
              const c = await sendVideosAsOneCard(ctx, r.fileRes, { force: !!args?.force });
              if (!c.viaCard) {
                logFns.error?.('文件卡片发送失败：', c.error);
              }
            }
            if (r.linkFallback && r.linkFallback.length) {
              await sendVideoLinksCard(ctx, r.linkFallback);
            }
          });

        if (!bg.started) return { content: bg.reason, isError: false };
        return {
          content: `已在后台下载 ${picked.length} 个视频（${vm.reason}）。`
            + `\n下好会自动发出卡片，这期间可以正常聊别的。`
            + `\n⚠️ 不要再用 send_message 说"开始下载了"或复述结果。`,
          isError: false
        };
      }

      await acquireSearchSlot();
      try {
        for (let seq = 0; seq < picked.length; seq += 1) {
          const it = picked[seq];
          const u = String(it.url || '');
          const isGamebus = /gamebus001\.cc\/video\//i.test(u)
            || (it.site === '游戏巴士' && it.id && !u);
          const isAlbum = /\/archives\/\d+/i.test(u)
            || /sumjpknqx|tadqmtxhg|zwztjllvs/i.test(u);
          const isCosplaytele = /cosplaytele\.com/i.test(u);
          const isTweet = it.site === 'twitter_post'
            || /^https?:\/\/(?:www\.)?(?:x|twitter)\.com\/\w+\/status\/\d+/.test(u);
          const isAv = it.site === 'av';

          let r = null;
          let entry = {
            title: String(it.title || '').slice(0, 140),
            pageUrl: u,
            site: it.site || '',
            duration: Number(it.duration) || 0,
            cover: it.coverLocal || '',
          };

          if (isGamebus && it.id) {
            r = await runGrab({ mode: 'gamebus_video', videoId: it.id,
              resolveOnly: true }, null);
            if (r && r.ok) {
              entry.hls = r.hls;
              entry.referer = r.referer;
              entry.title = entry.title || r.title;
              entry.duration = entry.duration || Number(r.duration) || 0;
              entry.pageUrl = r.pageUrl || entry.pageUrl;
            }
          } else if (isAlbum) {
            const wantIdx = Number.isFinite(Number(it.videoIndex)) ? Number(it.videoIndex) : 0;
            r = await runGrab({ mode: 'album_video', url: u,
              maxVideos: wantIdx + 1, resolveOnly: true }, null);
            if (r && r.ok && Array.isArray(r.videos) && r.videos.length) {
              const v = r.videos[Math.min(wantIdx, r.videos.length - 1)];
              entry.hls = v.hls;
              entry.referer = r.referer;
              entry.title = entry.title || v.title;
              entry.pageUrl = r.pageUrl || entry.pageUrl;
            }
          } else if (isCosplaytele) {
            r = await runGrab({ mode: 'video', url: u, resolveOnly: true }, null);
            if (r && r.ok) {
              entry.hls = r.hls;
              entry.referer = r.referer;
              entry.title = entry.title || r.title;
              entry.duration = entry.duration || Number(r.duration) || 0;
            }
          } else if (isAv) {
            const avs = String(it.avSite || '').toLowerCase();
            if (avs === 'magnet' || /^magnet:/.test(String(it.magnet || ''))) {
              // 磁力：本来就不用下载，直接给磁力链
              if (it.magnet) {
                entry.magnet = it.magnet;
                entry.note = `体积 ${it.size || '?'}`;
              } else {
                r = { ok: false, error: '这条没有磁力链接' };
              }
            } else if (u) {
              r = await runGrab({ mode: 'av_resolve', url: u }, null);
              if (r && r.ok) {
                const plays = (r.playUrls || []).map((x) => x);
                entry.hls = (r.m3u8 || [])[0] || '';
                entry.referer = r.referer;
                entry.title = entry.title || r.title;
                entry.kind = r.kind;
                if (r.kind === 'mp4' && (r.mp4 || []).length) {
                  entry.mp4 = (r.mp4 || [])[0];
                }
              }
            } else {
              r = { ok: false, error: '这条没有页面链接' };
            }
          } else if (isTweet) {
            // 推文：直接给原帖链接，不解析视频地址
            // （X 的视频直链有时效和签名，给出去大概率失效，页面链接反而永久有效）
            entry.note = '推特原帖，点开即可看';
          } else {
            r = { ok: false, error: `无法识别站点：${u.slice(0, 60)}` };
          }

          if (r && !r.ok) {
            failed.push({ idx: seq + 1, title: entry.title, error: r.error || r.code });
            // 页面链接仍然有效，照样给出去
            if (entry.pageUrl) links.push(entry);
            continue;
          }
          if (entry.pageUrl || entry.hls || entry.magnet) {
            links.push(entry);
          }
        }
      } finally {
        releaseSearchSlot();
        markSearchDone(ctx);
      }

      // ── 结果分两路：文件卡片 + 链接卡片 ────────────────────────────
      // 发文件模式下，下载成功的进 fileResults，失败的降级进 links。
      // 两种卡片可以同时发（比如 3 条里 2 条下成功、1 条超限降级）。
      const parts = [];

      if (links.length) {
        const sent = await sendVideoLinksCard(ctx, links);
        if (sent.ok) {
          parts.push(`已发 ${links.length} 条视频地址`);
        } else {
          parts.push(`地址卡片发送失败（${String(sent.error || '').slice(0, 80)}）`);
        }
      }

      if (!parts.length) {
        const why = failed.length
          ? failed.map((f) => `第${f.idx}条：${f.error}`).join('；')
          : '这些条目都没有可用地址';
        return { content: `没拿到任何结果：${why}`, isError: false };
      }

      let out = parts.join('；') + `。`;
      out += `\n（发送方式：${vm.mode}，${vm.reason}）`;
      if (failed.length) {
        out += `\n其中 ${failed.length} 条没解析到播放地址，但页面链接仍可用。`;
      }
      out += `\n⚠️ 不要再用 send_message 复述内容，卡片里都有。`;
      return { content: out };
    }
  });

  api.registerTool({
    id: 'gamebus_video',
    name: '游戏巴士视频地址',
    description: '取游戏巴士（gamebus001.cc）视频的播放地址，以卡片发出。**不下载视频文件**，只给地址。',
    category: 'media',
    icon: '🎮',
    parameters: {
      type: 'object',
      properties: {
        videoId: { type: 'string', description: '视频 ID（数字），从 find_video 或 gamebus_search 结果里拿' }
      },
      required: ['videoId']
    },
    async execute(ctx, args) {
      const vid = String(args?.videoId ?? '').trim();
      if (!/^\d+$/.test(vid)) return { content: 'videoId 要给数字。', isError: true };
      const cd = checkCooldown(ctx, '游戏巴士解析');
      if (!cd.ok) return { content: cd.message, isError: false };
      const vm = resolveVideoMode(ctx);
      const s = settings();

      if (vm.mode === 'file') {
        // 发文件：后台下载后发视频卡片
        const bg = runInBackground(ctx, `gamebus:${vid}`, '游戏巴士视频', (onP) => runGrab({
          mode: 'gamebus_video', videoId: vid,
          maxSendMB: s.videoMaxSendMB, cacheDays: s.videoCacheDays,
          cacheMaxGB: s.videoCacheMaxGB
        }, onP), async (r) => {
          await sendVideosAsOneCard(ctx, [{
            localPath: r.localPath, bytes: r.bytes, title: r.title,
            compressed: r.compressed, cached: r.cached,
            duration: r.duration, note: `https://gamebus001.cc/video/${vid}`
          }]);
        });
        if (!bg.started) return { content: bg.reason, isError: false };
        return {
          content: `已在后台下载（${vm.reason}），下好自动发出。`
            + `⚠️ 不要再用 send_message 说"开始下载了"。`
        };
      }

      // 发链接
      await acquireSearchSlot();
      let r;
      try {
        r = await runGrab({ mode: 'gamebus_video', videoId: vid, resolveOnly: true }, null);
      } finally {
        releaseSearchSlot();
        markSearchDone(ctx);
      }
      if (!r || !r.ok) {
        return { content: `取播放地址失败：${r?.error || r?.code || '未知'}`, isError: false };
      }
      const sent = await sendVideoLinksCard(ctx, [{
        title: r.title, pageUrl: r.pageUrl || `https://gamebus001.cc/video/${vid}`,
        hls: r.hls, referer: r.referer, site: '游戏巴士',
        duration: r.duration
      }]);
      if (!sent.ok) {
        return { content: `地址拿到了但卡片发送失败：${String(sent.error || '').slice(0, 100)}。`
          + `⚠️ 不要用 send_message 发。`, isError: false };
      }
      return { content: `已发视频地址卡片（${vm.reason}）。⚠️ 不要再用 send_message 复述。` };
    }
  });

  // ── 每日大赛搜索（中文站，可搜中文）──────────────────────────────────
  api.registerTool({
    id: 'album_search',
    name: '每日大赛搜索',
    description: '在每日大赛站（中文成人视频站）按关键词搜视频。支持中文关键词。返回标题和链接，标题里通常写明角色名。要下载视频再用 album_video。',
    category: 'media',
    icon: '🔞',
    parameters: {
      type: 'object',
      properties: {
        keyword: { type: 'string', description: '关键词，中文即可（如 自慰、露出、cos、雷电将军）' },
        limit: { type: 'number', description: '返回几条，默认 10，最多 20' }
      },
      required: ['keyword']
    },
    async execute(ctx, args) {
      const kw = String(args?.keyword ?? '').trim();
      if (!kw) return { content: '要搜什么？', isError: true };
      const limit = Math.min(80, Math.max(1, Number(args?.limit) || 30));
      const pages = Math.min(20, Math.max(1, Number(args?.pages) || 3));
      const chk2 = selfcheckImageChannel(ctx);
      if (!chk2.ok) {
        return { content: `⚠️ 图片通道可能受限：${chk2.reason}`, isError: false };
      }
      await sendSearchHint(ctx, `（翻 ${pages} 页，取前 ${limit} 条）`);
      const cd = checkCooldown(ctx, '每日大赛搜索');
      if (!cd.ok) return { content: cd.message, isError: false };
      await acquireSearchSlot();
      let r;
      try {
        r = await runGrab({ mode: 'album_search', keyword: kw, limit, pages,
          __timeoutMs: GRAB_SEARCH_TIMEOUT_MS }, null);
      } finally {
        releaseSearchSlot();
        markSearchDone(ctx);
      }
      if (!r.ok) {
        if (r.code === 'NO_RESULT') return { content: '每日大赛没搜到，换个词试试', isError: false };
        return { content: `搜索失败：${r.error}`, isError: true };
      }
      const items = r.items || [];
      const snapId = putSiteCandidates(ctx.chatKey, { items, keyword: kw, site: 'album' });
      let out;
      try {
        out = await sendSiteCandidates(ctx, items, { note: `关键词：${kw}`, snapId });
      } catch (err) {
        // 发送环节抛异常也要把"候选已就绪"告诉模型，否则模型会自己编文字发
        return {
          content: `搜到 ${items.length} 条，但发送卡片失败：${String(err?.message ?? err).slice(0, 150)}。候选已存好，群友可以直接说编号（如「下 3」）来下载。`
          + `\n编号 1-${items.length}：\n` + items.slice(0, 30).map((it, i) => `${i + 1}. ${it.title}`).join('\n'),
          isError: true
        };
      }
      const via = out?.viaCard
        ? '合并转发卡片（一层）'
        : `❌没有发出（卡片通道失败：${String(out?.cardError || '未知').slice(0, 80)}）`;
      if (!out?.viaCard) {
        return {
          content: `搜到 ${items.length} 条（快照 ${snapId}），但卡片发送失败，群友什么都没收到。`
            + `原因：${String(out?.cardError || '未知').slice(0, 120)}。`
            + `⚠️ 绝对不要用 send_message 把条目内容发出去——内容只能出现在卡片里。`,
          isError: true
        };
      }
      return {
        content: `已通过${via}把 ${items.length} 条候选（快照 ${snapId}）发给群友，编号 ${snapId}1 - ${snapId}${items.length}（每条含封面+标题+链接）。`
          + `群友回复编号（如「下 ${snapId}3」）后调 x-media-finder__pick_download。`
          + `\n你**可以**用 send_message 接一句很短的话（1-8 字），但不要复述卡片内容（别重念标题/编号/条数）。`
          + `\n想说就得调 send_message，光写正文发不出去。`
      };
    }
  });

  // ── 每日大赛视频下载 ─────────────────────────────────────────────────
  api.registerTool({
    id: 'album_video',
    name: '每日大赛视频地址',
    description: '取每日大赛文章页里所有视频的播放地址，以卡片发出。支持一篇文章多个视频。**不下载视频文件**，只给地址。',
    category: 'media',
    icon: '🎥',
    parameters: {
      type: 'object',
      properties: {
        url: { type: 'string', description: '文章页链接，如 https://album.sumjpknqx.cc/archives/198200/' },
        maxVideos: { type: 'number', description: '最多下载几个视频，默认 3' }
      },
      required: ['url']
    },
    async execute(ctx, args) {
      const url = String(args?.url ?? '').trim();
      if (!/^https?:\/\//.test(url)) return { content: '链接不对。', isError: true };
      const maxVideos = Math.min(6, Math.max(1, Number(args?.maxVideos) || 3));
      const cd = checkCooldown(ctx, '每日大赛解析');
      if (!cd.ok) return { content: cd.message, isError: false };
      const vm = resolveVideoMode(ctx);
      const s = settings();

      if (vm.mode === 'file') {
        const bg = runInBackground(ctx, `album:${url}`, '每日大赛视频', (onP) => runGrab({
          mode: 'album_video', url, maxVideos,
          maxSendMB: s.videoMaxSendMB, cacheDays: s.videoCacheDays,
          cacheMaxGB: s.videoCacheMaxGB
        }, onP), async (r) => {
          const vids = (r.videos || []).map((v) => ({
            localPath: v.localPath, bytes: v.bytes, title: v.title,
            compressed: v.compressed, cached: v.cached,
            note: v.tags ? `#${String(v.tags).split(',').join(' #')}\n${url}` : url
          }));
          await sendVideosAsOneCard(ctx, vids);
        });
        if (!bg.started) return { content: bg.reason, isError: false };
        return {
          content: `已在后台下载（${vm.reason}，最多 ${maxVideos} 个），下好自动发出。`
            + `⚠️ 不要再用 send_message 说"开始下载了"。`
        };
      }

      await acquireSearchSlot();
      let r;
      try {
        r = await runGrab({ mode: 'album_video', url, maxVideos,
          resolveOnly: true }, null);
      } finally {
        releaseSearchSlot();
        markSearchDone(ctx);
      }
      if (!r || !r.ok) {
        return { content: `取播放地址失败：${r?.error || r?.code || '未知'}`, isError: false };
      }
      const vids = (r.videos || []).map((v) => ({
        title: v.title, pageUrl: r.pageUrl || url,
        hls: v.hls, referer: r.referer, site: '每日大赛',
        note: v.tags ? `#${String(v.tags).split(',').join(' #')}` : ''
      }));
      const sent = await sendVideoLinksCard(ctx, vids);
      if (!sent.ok) {
        return { content: `拿到 ${vids.length} 个地址但卡片发送失败：`
          + `${String(sent.error || '').slice(0, 100)}。⚠️ 不要用 send_message 发。`,
          isError: false };
      }
      return { content: `已发 ${vids.length} 条视频地址卡片。⚠️ 不要再用 send_message 复述。` };
    }
  });

  // ── 站点视频下载（HLS 抓取 + 合并 + 压缩）─────────────────────────────
  api.registerTool({
    id: 'site_video',
    name: '站点视频下载',
    description: '把 cosplaytele 文章页里的视频抓下来并发出去。内部流程：Playwright 带 Referer 打开播放器拿到 m3u8 → 下载全部分片 → ffmpeg 合并 → 超过 150MB 自动压缩 → 发送。耗时较长（1GB 约 10-20 分钟），下载完自动发。',
    category: 'media',
    icon: '🎬',
    parameters: {
      type: 'object',
      properties: {
        url: { type: 'string', description: '文章页链接，如 https://cosplaytele.com/furina-15/' }
      },
      required: ['url']
    },
    async execute(ctx, args) {
      const url = String(args?.url ?? '').trim();
      if (!/^https:\/\/cosplaytele\.com\//.test(url)) {
        return { content: '给的链接不像 cosplaytele 的文章页。', isError: true };
      }
      const cd = checkCooldown(ctx, 'cosplaytele 解析');
      if (!cd.ok) return { content: cd.message, isError: false };
      const vm = resolveVideoMode(ctx);
      const s = settings();

      if (vm.mode === 'file') {
        let lastNote = '';
        const bg = runInBackground(ctx, `site:${url}`, 'cosplaytele 视频', (onP) => runGrab({
          mode: 'video', url,
          maxSendMB: s.videoMaxSendMB, cacheDays: s.videoCacheDays,
          cacheMaxGB: s.videoCacheMaxGB
        }, (ev) => { if (ev.note) lastNote = ev.note; onP(ev); }), async (r) => {
          await sendVideosAsOneCard(ctx, [{
            localPath: r.localPath, bytes: r.bytes,
            title: String(r.title || '').replace(/\s*-\s*Cosplaytele\s*$/i, '').slice(0, 160),
            compressed: r.compressed, cached: r.cached,
            duration: r.duration, note: url
          }]);
        });
        if (!bg.started) return { content: bg.reason, isError: false };
        return {
          content: `已在后台下载（${vm.reason}），下好自动发出。`
            + `⚠️ 不要再用 send_message 说"开始下载了"。`
        };
      }

      // 发链接
      await acquireSearchSlot();
      let r;
      try {
        r = await runGrab({ mode: 'video', url, resolveOnly: true }, null);
      } finally {
        releaseSearchSlot();
        markSearchDone(ctx);
      }
      if (!r || !r.ok) {
        return { content: `取播放地址失败：${r?.error || r?.code || '未知'}`, isError: false };
      }
      const sent = await sendVideoLinksCard(ctx, [{
        title: String(r.title || '').replace(/\s*-\s*Cosplaytele\s*$/i, '').slice(0, 160),
        pageUrl: url, hls: r.hls, referer: r.referer,
        site: 'cosplaytele', duration: r.duration
      }]);
      if (!sent.ok) {
        return { content: `地址拿到了但卡片发送失败：${String(sent.error || '').slice(0, 100)}。`
          + `⚠️ 不要用 send_message 发。`, isError: false };
      }
      return { content: `已发视频地址卡片（${vm.reason}）。⚠️ 不要再用 send_message 复述。` };
    }
  });

  // ── 磁力解析：看里面是什么 ─────────────────────────────────────────
  api.registerTool({
    id: 'magnet_parse',
    name: '解析磁力链接',
    description: '群友丢一条 magnet:?xt=urn:btih:... 过来问「这里面是什么」「帮我看看」'
      + '「解析一下」时用。用 aria2 取种子元数据，列出文件名和体积（不下载）。'
      + '群友看完说「下第 2 个」再用 pick_download 下载。',
    category: 'media',
    icon: '🧲',
    parameters: {
      type: 'object',
      properties: {
        magnet: { type: 'string', description: '完整的 magnet:?xt=urn:btih:... 链接' }
      },
      required: ['magnet']
    },
    async execute(ctx, args) {
      const mag = String(args?.magnet ?? '').trim();
      if (!mag) return { content: '没给磁力链接。', isError: true };
      if (!/^magnet:\?xt=urn:btih:/i.test(mag)) {
        return { content: '这不是磁力链接（要以 magnet:?xt=urn:btih: 开头）。', isError: true };
      }

      await sendText(ctx, '🧲 正在解析磁力（连做种节点取文件列表，约 30-60 秒）…');
      const rep = makeDownloadReporter(ctx, '磁力解析');
      const r = await runGrab({ mode: 'magnet_info', magnet: mag,
        timeoutSec: 90, __timeoutMs: 180 * 1000 }, (ev) => { rep.onProgress(ev); });

      if (!r.ok) {
        return {
          content: `磁力解析失败：${String(r.error || r.code || '').slice(0, 150)}。`
            + `\n常见原因：这个种子没人做种，或者当前网络连不上 BT 节点。`
            + `⚠️ 如实告诉群友，不要说"我不支持磁力"——工具是支持的，只是这个种子取不到元数据。`,
          isError: false
        };
      }

      const files = r.files || [];
      const nodes = [];

      // 汇总节点
      const head = [
        `🧲 ${r.name || '（无标题）'}`,
        `总大小：${r.totalSize || '?'}`,
        `文件数：${files.length}`,
        `InfoHash：${r.infoHash || '?'}`
      ].join('\n');
      nodes.push({ type: 'node', data: {
        user_id: Number(ctx.selfId) || 0,
        nickname: '磁力信息',
        content: [{ type: 'text', data: { text: head } }]
      } });

      // 每个文件一个节点（最多 12 个，够看）
      files.slice(0, 12).forEach((f2, i) => {
        const name = String(f2.path || '').replace(/^\.\//, '');
        const txt = [
          `【${i + 1}】${f2.size || '?'}`,
          name.slice(0, 200)
        ].join('\n');
        nodes.push({ type: 'node', data: {
          user_id: Number(ctx.selfId) || 0,
          nickname: `${i + 1}. ${String(name.split('/').pop() || '').slice(0, 26)}`.slice(0, 40),
          content: [{ type: 'text', data: { text: txt } }]
        } });
      });

      const src = `磁力解析：${files.length} 个文件`;
      const sent = await sendForwardWithRetry(ctx, nodes,
        { source: src, summary: src, prompt: src });

      // 存快照：群友说「下 2」就下第 2 个文件
      putSiteCandidates(ctx.chatKey, {
        items: files.slice(0, 12).map((f2, i) => ({
          site: 'av',
          avSite: 'magnet',
          url: '',
          title: String(f2.path || '').slice(0, 150),
          id: `${r.infoHash || 'x'}:${f2.idx || i + 1}`,
          magnet: r.magnet || mag,
          size: f2.size || ''
        })),
        keyword: r.name || 'magnet',
        site: 'magnet'
      });

      if (!sent.ok) {
        return {
          content: `解析成功（${files.length} 个文件），但卡片发送失败：`
            + `${String(sent.error || '').slice(0, 100)}。稍等重试。`,
          isError: true
        };
      }
      return {
        content: `已通过合并转发卡片发出磁力信息：${r.name || ''}，共 ${files.length} 个文件，`
          + `总 ${r.totalSize || '?'}。`
          + `群友说「下 2」就调 x-media-finder__pick_download 下载对应文件。`
          + `\n你**可以**用 send_message 接一句很短的话，但不要复述文件列表。想说就得调 send_message。`
      };
    }
  });

  // ── AV 番号搜索：FC2 / MissAV / Pornhub / 磁力 ───────────────────────
  api.registerTool({
    id: 'av_search',
    name: '番号搜片',
    description: '按番号或关键词搜 FC2 / MissAV / Pornhub，并附 sukebei 磁力。'
      + '群友说「搜 FC2-PPV-4605376」「找 SSIS-001」「这个番号哪能看」时用。'
      + '会自动识别番号（FC2-PPV-xxxx / SSIS-001 等），搜到的每条带编号，'
      + '群友说「下 2」就按编号下载，视频只在合并转发卡片里发。',
    category: 'media',
    icon: '🎬',
    parameters: {
      type: 'object',
      properties: {
        keyword: { type: 'string', description: '番号或关键词，如 FC2-PPV-4605376、SSIS-001、素人' },
        limit: { type: 'number', description: '每个站几条，默认 5' }
      },
      required: ['keyword']
    },
    async execute(ctx, args) {
      const kw = String(args?.keyword ?? '').trim();
      if (!kw) return { content: '要给番号或关键词。', isError: true };
      const limit = Math.min(12, Math.max(1, Number(args?.limit) || 5));

      const cd = checkCooldown(ctx, '番号搜片');
      if (!cd.ok) return { content: cd.message, isError: false };
      await sendText(ctx, '🔍 正在四个站搜番号（FC2 / MissAV / Pornhub / 磁力）…');
      const rep = makeDownloadReporter(ctx, '番号搜片');
      await acquireSearchSlot();
      let r;
      try {
        r = await runGrab({ mode: 'av_search', keyword: kw, limit,
          sites: ['fc2', 'missav', 'pornhub', 'magnet'],
          __timeoutMs: 300 * 1000 }, (ev) => { rep.onProgress(ev); });
      } finally {
        releaseSearchSlot();
        markSearchDone(ctx);
      }

      if (!r.ok) {
        if (r.code === 'NO_RESULT') {
          return { content: `四个站都没搜到「${kw}」。换个番号或关键词试试。`, isError: false };
        }
        return { content: `搜索失败：${r.error || r.code}`, isError: true };
      }

      const items = r.items || [];
      // 存快照供 pick_download
      const snapId = putSiteCandidates(ctx.chatKey, {
        items: items.map((it) => ({
          site: 'av',
          avSite: String(it.site || '').toLowerCase(),
          url: it.url,
          title: it.title,
          id: it.id,
          magnet: it.magnet || '',
          size: it.size || '',
          seeders: it.seeders || ''
        })),
        keyword: kw,
        site: 'av'
      });

      // 一层卡片：每条一个节点
      const nodes = items.map((it, i) => {
        const segs = [];
        // 封面用本地文件（远程 URL 协议端拉不到，必定 921 失败）
        if (it.coverLocal && isSendableImageRef(it.coverLocal)) {
          segs.push({ type: 'image', data: { file: toFileUri(it.coverLocal) } });
        }
        const tag = it.site === 'Magnet'
          ? `🧲 磁力${it.seeders ? ' · ' + it.seeders + '做种' : ''}${it.size ? ' · ' + it.size : ''}`
          : `【${it.site}】`;
        const lines = [`【${i + 1}】${tag}`, String(it.title || '').slice(0, 150)];
        if (it.site === 'Magnet' && it.magnet) {
          lines.push(String(it.magnet).slice(0, 300));
        } else if (it.url) {
          lines.push(it.url);
        }
        segs.push({ type: 'text', data: { text: lines.join('\n') } });
        return { type: 'node', data: {
          user_id: Number(ctx.selfId) || 0,
          nickname: `${i + 1}. ${it.site || 'AV'}`.slice(0, 40),
          content: segs
        } };
      });

      const src = `番号搜索：${kw}（${items.length} 条）`;
      const sent = await sendForwardWithRetry(ctx, nodes,
        { source: src, summary: src, prompt: src });

      if (!sent.ok) {
        return {
          content: `搜到 ${items.length} 条（快照 ${snapId}），但卡片发送失败：`
            + `${String(sent.error || '').slice(0, 100)}。稍等重试。`
            + `⚠️ 不要用 send_message 把内容发出去。`,
          isError: true
        };
      }
      const vids = items.filter((x) => x.site !== 'Magnet').length;
      const mags = items.filter((x) => x.site === 'Magnet').length;
      return {
        content: `已通过合并转发卡片发出 ${items.length} 条（快照 ${snapId}，`
          + `可播放页 ${vids} 条 + 磁力 ${mags} 条）。`
          + `群友说「下 2」就调 x-media-finder__pick_download，indices 填编号。`
          + `\n你**可以**用 send_message 接一句很短的话，但不要复述卡片内容。想说就得调 send_message。`
      };
    }
  });

  // ── 以图搜源：图 → 原帖链接 + 账号 + 主页图 ────────────────────────────
  api.registerTool({
    id: 'find_image_source',
    name: '以图搜源',
    description: '群友发一张图问「这谁」「哪来的」「找出处」时用。'
      + 'Google Lens 识图 → 提取推特原帖链接 + 账号 → 查账号资料和主页图。'
      + '返回：原帖链接、账号（含粉丝数/简介）、头像和 banner 图（本地文件）。'
      + '⚠️ 直接把带图消息的 messageId（聊天记录里的 #数字）填进来就行，不要填 URL。',
    category: 'media',
    icon: '🔍',
    parameters: {
      type: 'object',
      properties: {
        messageId: { type: ['integer', 'string'], description: '带图消息的 id（聊天记录里的 #数字，可能为负）。最常用，直接填这个就行' },
        imagePath: { type: 'string', description: '图片的本地文件路径（已有本地文件时才用）' }
      },
      required: []
    },
    async execute(ctx, args) {
      let img = String(args?.imagePath ?? '').trim();
      const mid = args?.messageId != null ? String(args.messageId).trim() : '';

      // 优先用 messageId：因为 get_message_images 只把图给模型"看"，
      // 不返回本地路径（它返回的是 base64 图片数据）。模型手上只有
      // messageId 和图片 URL，所以必须能直接从 messageId 取图。
      if (!img && mid) {
        const entry = ctx.store.findByMid(ctx.chatKey, mid);
        if (!entry) {
          return { content: `当前会话找不到消息 ${mid}。填聊天记录里那条消息前面的 #数字。`, isError: true };
        }
        const urls = (entry.media || [])
          .filter((m) => m.kind === 'image' && m.url)
          .map((m) => m.url);
        if (!urls.length) {
          return { content: `消息 ${mid} 里没有图片。`, isError: true };
        }
        // 下载第一张图到本地
        const dlDir = path.join(HERE, 'data', 'imgsearch');
        fs.mkdirSync(dlDir, { recursive: true });
        const dst = path.join(dlDir, `src_${Date.now()}.jpg`);
        try {
          const res = await fetch(urls[0], { signal: AbortSignal.timeout(60000) });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const buf = Buffer.from(await res.arrayBuffer());
          if (buf.length < 1000) throw new Error(`图片太小（${buf.length} 字节）`);
          fs.writeFileSync(dst, buf);
          img = dst;
        } catch (err) {
          return {
            content: `下载消息里的图片失败：${String(err?.message ?? err).slice(0, 120)}。`
              + `可以让群友重发一张，或换个时间再试。`,
            isError: true
          };
        }
      }

      if (!img) {
        return { content: '要给 messageId（带图消息的 #编号）或 imagePath。', isError: true };
      }
      if (!fs.existsSync(img)) {
        // 模型可能把 messageId 或 URL 填进了 imagePath，纠正它
        return {
          content: `图片不存在：${img}。`
            + `如果这是消息编号，请改用 messageId 参数；如果是 URL，也要用 messageId。`,
          isError: true
        };
      }

      // 识图要跑 60 秒以上，**不能同步等**：
      // 实测 16:41 那次，识图跑到第 30 秒时技能热重载（文件监听触发），
      // 这一轮工具调用被中断 → 工具返回空 → 模型以为成功，回了
      // "找到了，卡片在上边"，但群里什么都没有。
      // 改成后台跑：工具立刻返回，卡片由后台任务发。
      const bg = runInBackground(ctx, `img:${Date.now()}`, '以图搜源',
        (onP) => runGrab({ mode: 'image_source', image: img,
          __timeoutMs: 300 * 1000 }, onP),
        async (r) => { await renderImageSourceResult(ctx, r); },
        { acceptEmpty: true });

      if (!bg.started) return { content: bg.reason, isError: false };
      return {
        content: `已在后台开始识图（约 1-2 分钟），找到来源后会自动发卡片。`
          + `这期间可以正常聊别的。`
          + `⚠️ 绝对不要回复"找到了/卡片在上边"——卡片还没发，等它自己发。`
          + `也不要自己复述结果，工具会直接发卡片。`
      };


    }

  });

  // ── 自检工具：部署后第一件事 ────────────────────────────────────────
  api.registerTool({
    id: 'selfcheck',
    name: 'X 搜索自检',
    description: '检查本技能的环境是否就绪：twscrape 是否装了、账号库有没有账号、图片原图转换是否正常。不联网、不发消息。部署完或群友反馈"搜不了"时调用。',
    category: 'media',
    icon: '🩺',
    parameters: { type: 'object', properties: {}, required: [] },
    async execute(ctx) {
      const s = settings();
      const py = pythonOrWarn();
      // 图片通道状态（这是记账，不是探测 —— 详见 selfcheckImageChannel 注释）
      const ch = ctx ? selfcheckImageChannel(ctx) : { ok: true, reason: '无 ctx' };
      if (!py) {
        return { content: '❌ 找不到可用的 Python。请安装 Python 3.10+ 或在设置里填解释器路径。', isError: true };
      }
      const db = accountsDbPath();
      // 自检永远跳过缓存：它的用途就是"现在到底行不行"
      const r = await runWorkerCheck();
      invalidatePreflight();
      const lines = [
        `Python：${py.exe}${py.args.length ? ` ${py.args.join(' ')}` : ''}${py.configuredMissing ? '（用的是自动探测，配置的路径不可用）' : ''}`,
        `账号库：${db}${fs.existsSync(db) ? '' : '（不存在！先按 README 登录）'}`,
        `嵌套层数：${s.nestDepth}　节点写法：${s.nodeSyntax}`,
        `群聊白名单：${s.groupAllowlist.length ? s.groupAllowlist.join(', ') : '（空 = 群聊禁用）'}`,
        ch.ok
          ? '图片通道：最近正常'
          : `图片通道：⚠️ ${ch.reason}`
      ];
      if (r.ok && r.report) {
        lines.push(`twscrape：${r.report.twscrape}`);
        lines.push(`账号数：${r.report.accounts}`);
        lines.push(`原图转换：${r.report.origSample}`);
      } else {
        lines.push(`❌ 自检失败：${r.error || '未知原因'}`);
      }
      return { content: lines.join('\n'), isError: !r.ok };
    }
  });
}

/**
 * 技能可用性：有 Python 才显示工具（否则模型会看到一个永远失败的工具）。
 *
 * ⚠️ 这里**不判断 enabled**：启用状态由核心 tool-registry 统一负责，
 * 这里只说"环境是否就绪"。两边都判会导致关闭技能时报出错误的原因。
 */
export function available() {
  if (!pythonOrWarn()) return { ok: false, reason: '未找到可用的 Python 解释器' };
  return true;
}

/** 热重载/卸载时清掉探测缓存，让新配置立即生效。 */
export function dispose() {
  cachedPython = undefined;
  invalidatePreflight();
}

/**
 * 测试用导出：把内部纯函数暴露出来，便于脱离 ctx / 网络单独验证。
 *
 * 为什么值得开这个口子：关键词拦截是安全底线，必须有独立于"技能是否启用"
 * 的验证手段 —— 否则一旦技能开关是关的，拦截逻辑就永远测不到，
 * 等真开了才发现它其实不生效，那时已经在群里跑起来了。
 * 命名带 __ 前缀表示"非公开 API，别在别处依赖"。
 */
export const __test = {
  hitsDenyKeyword,
  settings,
  accountsDbPath,
  resolvePython,
  mediaMessageSegments,
  collectTags,
  // 视频发送方式相关（按群分流）
  parseModeMap,
  resolveVideoMode,
  checkCooldown,
  markSearchDone,
};
