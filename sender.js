// 合并转发卡片构造：把搜索结果拼成 OneBot v11 的 forward 节点树。
//
// ── 为什么单独一个文件 ────────────────────────────────────────────────────
// 拼接逻辑是本次唯一容易踩协议坑的地方，抽出来后可以脱机单测（见 selftest.mjs），
// 不用连 SnowLuma 就能验证「节点树结构对不对」。
//
// ── 本机 SnowLuma 的硬约束（从 index.mjs 的 parseForwardNodes 读出）────────
//   1. MAX_FORWARD_DEPTH = 3。depth 从 0 开始，每进一层 content 数组 +1，
//      depth >= 3 直接抛 "forward nesting depth exceeds 3"。
//      → 所以最多 3 层节点，再多必定失败，不是"建议"是"抛异常"。
//   2. 一个 node 的 content 数组，要么**全是** type:'node' 段（＝嵌套一层），
//      要么**全是**普通段（text/image/video）。文本和 node 混在一个数组里
//      会被 isNestedNodeArray 判 false，然后严格解析器拒掉里面的 node 段。
//      → 外层卡片的"标题"和"内容组"必须是两个**并列**的 node，不能塞同一个 content。
//   3. 两种节点写法都支持（parseForwardNodes 里 rawType === 'node' 分支 vs 裸对象分支）：
//        typed : { type: 'node', data: { user_id, nickname, content } }
//        bare  : { user_id, nickname, content }
//      这就是 nodeSyntax 开关。换 OneBot 实现时如果一种报 INVALID_FIELD，切另一种。
//   4. user_id 缺省会回落到机器人自己的 selfId；nickname 缺省是 user_id 的字符串形式。
//      所以昵称一定要显式给，否则卡片上显示一串 QQ 号。
//
// ⚠️ 重要：嵌套只能用 typed 写法
//   isNestedNodeArray 的判定是 `String(seg.type ?? "") !== "node"` —— 裸对象没有 type
//   字段，所以"裸 node 数组"**不会**被识别成嵌套，会掉进 parseMessage 而被
//   "segment 必须有 type" 拒掉。因此 nodeSyntax=bare 只作用于最外层；
//   只要有嵌套层，内层一律自动用 typed。这不是保守选择，是协议端的行为决定的
//   （selftest.mjs 的 "bare 模式下内层必须是 typed" 用例锁住了这一点）。

/** 节点写法：typed = 带 type/data 包装；bare = 裸对象。 */
export const NODE_SYNTAX = Object.freeze({ TYPED: 'typed', BARE: 'bare' });

/**
 * 造一个 forward 节点。
 *
 * @param {'typed'|'bare'} syntax 节点写法（见文件头注释 3）
 * @param {object} node { userId, nickname, content }
 *        content 可以是普通段数组（text/image/video），也可以是**纯 node 段数组**（再嵌一层）
 * @returns {object} OneBot v11 的 node 段
 */
export function makeNode(syntax, { userId, nickname, content }) {
  // user_id 必须是正整数，SnowLuma 侧 toPositiveInt 失败会退回 selfId；
  // 这里主动挡掉 NaN/负数，避免"明明传了却显示成机器人自己"的诡异现象。
  const uid = Number(userId);
  const payload = {
    user_id: Number.isFinite(uid) && uid > 0 ? Math.trunc(uid) : undefined,
    nickname: String(nickname || '').trim() || undefined,
    content
  };
  if (syntax === NODE_SYNTAX.BARE) {
    // 裸对象写法：直接把字段摊在顶层，不要 type/data
    const out = { content };
    if (payload.user_id !== undefined) out.user_id = payload.user_id;
    if (payload.nickname !== undefined) out.nickname = payload.nickname;
    return out;
  }
  // typed 写法（标准 OneBot v11）
  const data = { content };
  if (payload.user_id !== undefined) data.user_id = payload.user_id;
  if (payload.nickname !== undefined) data.nickname = payload.nickname;
  return { type: 'node', data };
}

/**
 * 视频段。
 *
 * SnowLuma 的 video 元素支持 url / fileId / 本地路径三种形态，
 * 最终都会走 stageSourceToDisk（单文件上限 1536MB，我们自己在下载侧限 40MB）。
 * 这里传 file:/// 形式的本地路径。
 */
export function videoSeg(file) {
  return { type: 'video', data: { file: String(file) } };
}

/**
 * 把本地绝对路径转成 file:/// URI。
 *
 * 为什么需要转换：Windows 路径 `D:\a\b.mp4` 直接塞进段里时，
 * 部分协议端会当成相对路径处理。file:/// 是明确的绝对 URI。
 * 注意 Windows 盘符要写成 file:///D:/a/b.mp4（正斜杠、三个斜杠）。
 */
export function toFileUri(p) {
  const s = String(p ?? '').trim();
  if (!s) return '';
  if (/^file:\/\//i.test(s)) return s;
  const normalized = s.replace(/\\/g, '/');
  if (/^[a-zA-Z]:\//.test(normalized)) return `file:///${normalized}`;
  if (normalized.startsWith('/')) return `file://${normalized}`;
  return normalized;
}

/** 文本段。 */
export function textSeg(text) {
  return { type: 'text', data: { text: String(text ?? '') } };
}

/**
 * 图片段。
 * @param {string} file 三种形态都行：http(s) URL / base64://xxx / 本地绝对路径
 */
export function imageSeg(file) {
  return { type: 'image', data: { file: String(file) } };
}

/** 把数组按每 n 个切成若干组（用于"每张卡片最多 N 条媒体"）。 */
export function chunk(items, n) {
  const size = Math.max(1, Math.trunc(Number(n) || 1));
  const out = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

/** 数一条结果里有几条媒体（图 + 视频）。 */
export function mediaCountOf(item) {
  return (item?.images?.length || 0) + (item?.videos?.length || 0);
}

/**
 * 按"媒体条数"分组，而不是按"推文条数"。
 *
 * 需求是「单张卡片最多 2 条媒体」，一条推文可能自带 4 张图 ——
 * 所以不能简单地每 2 条推文一张卡，必须按媒体累加来切。
 * 单条推文媒体数超过上限时它自己独占一张卡（不拆推文，避免同一作者被割裂）。
 *
 * @param {Array<object>} items
 * @param {number} limit 每张卡最多几条媒体
 * @returns {Array<Array<object>>} 每张卡包含的 items
 */
export function groupByMedia(items, limit) {
  const cap = Math.max(1, Math.trunc(Number(limit) || 2));
  const cards = [];
  let cur = [];
  let curCount = 0;
  for (const item of items || []) {
    const n = Math.max(1, mediaCountOf(item));
    if (cur.length && curCount + n > cap) {
      cards.push(cur);
      cur = [];
      curCount = 0;
    }
    cur.push(item);
    curCount += n;
  }
  if (cur.length) cards.push(cur);
  return cards;
}

/**
 * 判断一个 file 引用是不是"可以交给协议端"的图片源。
 *
 * 三种形态都合法（SnowLuma 的 pickMediaSource / stageSource 都支持）：
 *   · http(s)://…       远端地址，协议端自己下载
 *   · base64://…        内联数据（已下载好的图）
 *   · data:image/…;…    内联 data URI
 *   · 本地绝对路径       协议端读本地文件（不推荐，会暴露宿主路径）
 *
 * ⚠️ 早期版本这里只放行 http(s)，导致 base64 图被**静默丢弃** ——
 *    卡片发出去了、返回 ok，但里头没有图。这类"成功但内容缺失"最难排查，
 *    所以判定放宽到全部合法形态，并且不认识的一律记日志而不是悄悄跳过。
 */
export function isSendableImageRef(file) {
  const s = String(file ?? '').trim();
  if (!s) return false;
  if (/^(https?|base64):\/\//i.test(s)) return true;
  if (/^data:image\//i.test(s)) return true;
  // Windows 盘符路径 或 POSIX 绝对路径
  if (/^[a-zA-Z]:[\\/]/.test(s)) return true;
  if (/^\//.test(s)) return true;
  return false;
}

/**
 * 判断字符串是不是可用的视频直链。
 * 只要 http(s) 且看起来是 mp4（或至少是 http 链接）；m3u8 在 worker 层已经排掉了。
 */
export function isSendableVideoRef(url) {
  return /^https?:\/\//i.test(String(url ?? '').trim());
}

/**
 * 把一条搜索结果转成卡片里的"媒体段数组"。
 *
 * 图片走 image 段（SnowLuma 收到 URL 会自己下载上传，不需要我们落地文件）；
 * 视频按需求只放 mp4 直链文本 —— 嵌套卡片里的 video 段在部分协议端会被拒或无法预览，
 * 文本链接是唯一稳的做法。
 *
 * @param {object} item { author, images: string[], videos: string[], likes, text }
 * @param {object} opts { showAuthor, authorHeader }
 * @returns {Array} OneBot 段数组
 */
export function mediaSegments(item, opts = {}) {
  const segs = [];
  if (opts.showAuthor !== false && item.author) {
    const likes = Number(item.likes) || 0;
    const head = `${opts.authorHeader !== false ? '@' : ''}${item.author}${likes ? ` · ♥${likes}` : ''}`;
    segs.push(textSeg(head));
  }

  // 原文：群友要知道这条推文写了什么（尤其是视频推——只有链接看不出内容）
  if (opts.showText !== false && item.text) {
    const t = String(item.text).trim();
    if (t) segs.push(textSeg(t));
  }

  // 图片：优先内联数据（worker 已下载好）。
  // 为什么：传 URL 时是**协议端**去下载，而协议端常常上不了 Twitter CDN，
  // 表现就是卡片正常但没有图。内联后与协议端网络环境无关。
  const imgs = Array.isArray(item.imageData) && item.imageData.length
    ? item.imageData
    : (item.images || []);
  for (const url of imgs) {
    if (isSendableImageRef(url)) segs.push(imageSeg(url));
  }

  // 视频：预览图 → 时长 → 链接。
  // 顺序很重要：先让群友看到画面和时长，再给可点的直链；
  // 只丢一条裸 URL 的话，光看链接完全不知道是什么内容。
  const details = Array.isArray(item.videoDetails) && item.videoDetails.length
    ? item.videoDetails
    : (item.videos || []).map((u) => ({ url: u, thumbnail: '', duration: 0 }));

  let videoIdx = 0;
  for (const vd of details) {
    videoIdx += 1;
    const url = String(vd?.url || '');
    if (!isSendableVideoRef(url)) continue;

    // 预览图（有就放，没有就跳过，不要留空段）
    const thumb = String(vd?.thumbData || vd?.thumbnail || '');
    if (thumb && isSendableImageRef(thumb)) segs.push(imageSeg(thumb));

    // 时长：有的推没给，0 就不显示
    const dur = Number(vd?.duration) || 0;
    const durText = dur > 0
      ? `${Math.floor(dur / 60)}:${String(dur % 60).padStart(2, '0')}`
      : '';

    // 多个视频时标序号，单个不标
    const label = details.length > 1 ? `🎬 视频 ${videoIdx}` : '🎬 视频';
    const parts = [label];
    if (durText) parts.push(durText);
    if (vd?.kind === 'gif') parts.push('GIF');
    segs.push(textSeg(parts.join(' · ')));

    // video 段在**同一条消息/同一个节点内**必须独占一行（SnowLuma 的
    // assertVideoSendPolicy：effectiveSegmentCount 必须为 1，
    // 否则报 `video must be the only segment in a message`）。
    // 但**转发节点之间**不受此限 —— 只要让每个视频单独占一个节点，
    // 节点内容里只有 video 一个段，就能塞进同一张合并转发卡片。
    // 实测：32MB 视频放进嵌套节点，send_private_forward_msg 返回 ok。
    // 没下载成功（localPath 不存在）时退回文本链接，保证卡片仍能发出。
    if (!vd?.localPath) segs.push(textSeg(url));
  }

  return segs;
}

/**
 * 卡片第一条（标题节点）的默认文案模板。
 *
 * 可用占位符：{keyword} {page} {total} {media} {results} {nickname} {depth_hint}
 * 用 \n 换行会拆成多条文本段（QQ 里就是多行）。
 *
 * 想改默认输出，把配置项 `headerTemplate` 设成自己的模板即可，
 * 不用动代码（见 README「自定义卡片首条文案」）。
 */
export const DEFAULT_HEADER_TEMPLATE =
  '🔞 X 结果 · {keyword}\n第 {page}/{total} 张 · {media} 条媒体';

/** depth=3 时 {depth_hint} 展开成这句，depth=2 时展开成空串。 */
export const DEPTH_HINT_TEXT = '（点开后还要再点一次「查看」）';

/**
 * 渲染模板：替换占位符并拆行。
 *
 * 设计要点：
 *   · 未知占位符原样保留（不静默吞掉），便于用户发现自己拼错了
 *   · 空行会被去掉，避免卡片里出现无意义的空文本段
 *   · 全部渲染成空时返回空数组，调用方据此回退默认模板
 *
 * @param {string} template 模板原文
 * @param {object} vars 占位符取值
 * @returns {string[]} 逐行文本（已去空白、去空行）
 */
export function renderTemplate(template, vars = {}) {
  const raw = String(template ?? '');
  if (!raw.trim()) return [];
  const filled = raw.replace(/\{(\w+)\}/g, (whole, key) => {
    if (!Object.prototype.hasOwnProperty.call(vars, key)) return whole;  // 未知占位符原样留
    const v = vars[key];
    return v === undefined || v === null ? '' : String(v);
  });
  return filled
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

/**
 * 生成标题节点的文本段。
 *
 * 优先用用户模板；渲染不出内容（模板为空/全是空行）时回退默认，
 * 因为 SnowLuma 遇到空 content 节点会直接抛
 * 「forward node content is empty」，整张卡都发不出去。
 *
 * @returns {Array} OneBot 文本段数组
 */
export function buildHeaderSegments(p) {
  const items = Array.isArray(p.items) ? p.items : [];
  const vars = {
    keyword: p.keyword ?? '',
    page: p.page ?? 1,
    total: p.totalPages ?? 1,
    media: items.reduce((s, it) => s + mediaCountOf(it), 0),
    results: items.length,
    nickname: p.selfNickname || '机器人',
    depth_hint: Number(p.depth) === 3 ? DEPTH_HINT_TEXT : ''
  };

  let lines = renderTemplate(p.headerTemplate, vars);
  if (!lines.length) {
    // 用户没配 / 配成空 → 用默认模板再渲染一次
    lines = renderTemplate(DEFAULT_HEADER_TEMPLATE, vars);
  }
  if (Number(p.depth) === 3 && !String(p.headerTemplate || '').includes('{depth_hint}')) {
    // 模板里没写 {depth_hint} 时补一行提示：三层嵌套点开还要再点一次，
    // 不提示的话第一次用的人会以为没内容。用户显式写了就不重复加。
    if (lines.length && !lines.some((l) => l.includes('点开'))) lines.push(DEPTH_HINT_TEXT);
  }
  if (!lines.length) lines = ['搜索结果'];
  return lines.map(textSeg);
}

/**
 * 构造一条完整的合并转发消息（一次 send_group_forward_msg 的 messages 参数）。
 *
 * 树形（nestDepth = 2，默认）：
 *   messages[0] 标题节点   content = [text, text, ...]        ← 普通段
 *   messages[1] 内容组节点 content = [条1节点, 条2节点, ...]    ← 纯 node 段（嵌一层）
 *     条N节点   content = [作者文本, image, image, 视频链接]   ← 普通段
 *
 * nestDepth = 3 时，在标题与内容组之间再插一个"索引"节点，把条目挂到它下面：
 *   messages[0] 标题 → messages[1] 索引(content=纯node) → 条(content=普通段)
 * 这是协议上限（MAX_FORWARD_DEPTH=3）能容纳的最深结构，不要再加。
 *
 * ⚠️ 为什么标题和内容组是**两个并列**的 node 而不是塞进同一个 content：
 *    SnowLuma 的 isNestedNodeArray 要求 content 要么全是 node 段、要么全不是，
 *    混排会被判 false 然后严格解析器拒掉里面的 node 段。
 *
 * @param {object} p
 * @param {'typed'|'bare'} p.syntax
 * @param {number} p.depth 2 或 3
 * @param {number} p.selfId 机器人 QQ
 * @param {string} p.selfNickname 机器人昵称（卡片上显示的名字）
 * @param {string} p.keyword 用户搜的词
 * @param {number} p.page 第几张（从 1 开始）
 * @param {number} p.totalPages 共几张
 * @param {Array<object>} p.items 本张卡片里的结果（每项一条独立节点）
 * @param {object} p.opts 透传给 mediaSegments
 * @returns {Array} messages 数组
 */
export function buildForwardCard(p) {
  const outerSyntax = p.syntax === NODE_SYNTAX.BARE ? NODE_SYNTAX.BARE : NODE_SYNTAX.TYPED;
  // 内层必须 typed：裸对象没有 type 字段，isNestedNodeArray 认不出嵌套（见文件头 ⚠️）
  const innerSyntax = NODE_SYNTAX.TYPED;
  const depth = Number(p.depth) === 3 ? 3 : 2;
  const who = { userId: p.selfId, nickname: p.selfNickname || '机器人' };
  const items = Array.isArray(p.items) ? p.items : [];

  // ── 标题节点：内容由 headerTemplate 决定（默认如实写清这是什么）──────────
  // 默认刻意不伪装成"群友闲聊"：内容是什么就写什么。收卡片的人一眼知道
  // 自己在点什么，不需要猜。要改文案就配 headerTemplate，见 renderTemplate。
  const headerNode = makeNode(outerSyntax, {
    ...who,
    content: buildHeaderSegments({ ...p, items, depth })
  });

  // ── 每条结果一个节点（内层 → 必须 typed）─────────────────────────────────
  const itemNodes = items.map((item, idx) => {
    const segs = mediaSegments(item, p.opts || {});
    if (!segs.length) segs.push(textSeg('（这条没有可显示的媒体）'));
    return makeNode(innerSyntax, {
      ...who,
      nickname: items.length > 1 ? `${who.nickname} · ${idx + 1}` : who.nickname,
      content: segs
    });
  });

  // ── 视频节点：每个视频单独一个节点 ──────────────────────────────────────
  // 为什么要单独成节点：SnowLuma 的 assertVideoSendPolicy 要求 video 段在
  // **同一个节点内** effectiveSegmentCount === 1（"video must be the only
  // segment in a message"）。但节点与节点之间没有这个限制 —— 所以把每个
  // 视频拆成独立节点、内容只放一个 video 段，就能塞进同一张卡片。
  // 这样群友点开卡片直接能播放，不用跳去 X 看链接。
  // 实测：32MB 视频放进嵌套节点，send_private_forward_msg 返回 ok。
  const videoNodes = [];
  for (const it of items) {
    for (const vd of (it.videoDetails || [])) {
      if (!vd?.localPath) continue;   // 没下载成功的走原来的文本链接
      const dur = Number(vd.duration) || 0;
      const durText = dur > 0
        ? ` ${Math.floor(dur / 60)}:${String(dur % 60).padStart(2, '0')}`
        : '';
      videoNodes.push(makeNode(innerSyntax, {
        ...who,
        nickname: `${it.author || who.nickname} · 🎬${durText}`.slice(0, 40),
        content: [{ type: 'video', data: { file: toFileUri(vd.localPath) } }]
      }));
    }
  }
  const allItemNodes = itemNodes.concat(videoNodes);

  if (depth === 3) {
    // 真·三层：messages → 包装节点 → 内层节点 → 普通媒体段。
    // 这是 MAX_FORWARD_DEPTH=3 能容纳的极限（parseForwardNodes 在 depth 0/1/2
    // 各进一次，depth=3 直接抛），再多一层协议端必崩。
    const innerNode = makeNode(innerSyntax, {
      ...who,
      nickname: `${who.nickname} · 媒体`,
      content: allItemNodes
    });
    const wrapNode = makeNode(outerSyntax, {
      ...who,
      nickname: `${who.nickname} · 内容`,
      content: [innerNode]
    });
    return [headerNode, wrapNode];
  }
  // depth 2：条目节点直接挂外层的内容组节点（它也在顶层 → outerSyntax）
  const groupsNode = makeNode(outerSyntax, {
    ...who,
    nickname: `${who.nickname} · 内容`,
    content: allItemNodes
  });
  return [headerNode, groupsNode];
}

// 私聊不套卡片时直接用 mediaSegments（见上）。
// 早先这里有个 flatMessageSegments 薄包装，功能与 mediaSegments 完全相同，
// 属于纯粹的死代码 —— 删掉，避免后来的人以为两者有语义差异。
