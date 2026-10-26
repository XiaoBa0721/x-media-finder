// 跨次去重：同一个会话（私聊某人 / 某个群）不重复收到同一条内容。
//
// ── 为什么需要 ────────────────────────────────────────────────────────────
// 原来的去重只在一次搜索内部生效（worker 里的 seen_ids 局部集合）。
// 连搜两次同一个词，两次各自都能拿到那 12 条热门结果，于是同一个群里
// 同一批图会出现两遍 —— 非常难看，也是"质量低"的一部分观感来源。
//
// ── 设计取舍 ──────────────────────────────────────────────────────────────
// 1) **按会话隔离**：私聊 A 和群 B 各自一份记录。群 B 看过的内容不影响
//    私聊 A —— 否则会变成"别人看过的我就看不到"，那是 bug 不是特性。
// 2) **按内容指纹去重**，不是按推文 id：
//    同一条推文被删了又发、或不同账号搬运同一张图，id 不同但内容相同。
//    指纹 = 推文 id（有则用）+ 首图 URL 归一化后取哈希。
// 3) **有 TTL**：热门内容过一段时间又允许出现。永久去重会让常搜的词
//    越搜越空，最后变成"没货"，那是另一种形式的坏体验。
// 4) **有容量上限**：每个会话最多记 N 条，超了丢最旧的（LRU 语义）。
//    不设上限的话文件会无限长大。
//
// 存储：resources/app/data/x-media-finder/seen-<会话hash>.json
// 用文件而不是内存，是为了重启后仍然有效（用户显然希望"我昨天看过的别再发"）。

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP_ROOT = path.resolve(HERE, '..', '..');
const DATA_DIR = path.join(APP_ROOT, 'data', 'x-media-finder');

/** 默认 TTL：7 天。热门图一周后允许重新出现。 */
const DEFAULT_TTL_MS = 7 * 24 * 60 * 60 * 1000;

/** 每个会话最多记多少条指纹。 */
const DEFAULT_MAX_PER_CHAT = 3000;

/** 会话 key 直接当文件名不安全（含冒号、QQ 号），统一哈希。 */
function chatFile(chatKey) {
  // 简单稳定哈希：FNV-1a。不用 crypto 是为了让文件名可预测、便于人工排查。
  let h = 0x811c9dc5;
  const s = String(chatKey || '');
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  const safe = String(chatKey || '').replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 40);
  return path.join(DATA_DIR, `seen-${safe}-${h.toString(16)}.json`);
}

/**
 * 内容指纹。
 *
 * 组成：
 *   · 推文 id（最准，X 内唯一）
 *   · 首图/首视频 URL 的"归一化"形式（去掉 name=orig 等查询参数，
 *     因为同一张图的 thumb 与 orig 是不同 URL，但内容一样）
 * 两者都取到就拼起来，保证"同图不同推"也能识别。
 *
 * @param {object} item
 * @returns {string} 指纹
 */
export function fingerprint(item) {
  const parts = [];
  const id = String(item?.id ?? '').trim();
  if (id) parts.push(`id:${id}`);

  const firstImage = (item?.images || [])[0];
  const firstVideo = (item?.videos || [])[0];
  const mediaUrl = firstImage || firstVideo || '';
  if (mediaUrl) {
    // 归一化：砍掉 ? 之后的所有参数（name=orig / format=jpg 都不影响内容）
    const base = String(mediaUrl).split('?')[0];
    parts.push(`m:${base}`);
  }

  if (!parts.length) {
    // 兜底：作者 + 文本前 40 字（极少走到，因为无媒体的 item 已被 worker 丢掉）
    parts.push(`a:${item?.author || ''}|${String(item?.text || '').slice(0, 40)}`);
  }
  return parts.join('|');
}

/**
 * 一个会话的去重记录。
 * 用类而不是裸函数，是为了把"加载一次、多次判断、最后统一保存"串起来，
 * 避免每条 item 都读一次文件。
 */
export class SeenStore {
  /**
   * @param {string} chatKey 会话 key，如 'group:123456789' / 'private:987654321'
   * @param {object} opts { ttlMs, maxPerChat, dir }
   */
  constructor(chatKey, opts = {}) {
    this.chatKey = String(chatKey || '');
    this.ttlMs = Number(opts.ttlMs) > 0 ? Number(opts.ttlMs) : DEFAULT_TTL_MS;
    this.maxPerChat = Number(opts.maxPerChat) > 0 ? Number(opts.maxPerChat) : DEFAULT_MAX_PER_CHAT;
    this.file = opts.dir
      ? path.join(opts.dir, path.basename(chatFile(this.chatKey)))
      : chatFile(this.chatKey);
    this.map = new Map();      // fingerprint -> 首次发送时间(ms)
    this.dirty = false;
    this.load();
  }

  /** 读盘。文件坏了就当空的（不能因为去重记录损坏就发不出东西）。 */
  load() {
    try {
      if (!fs.existsSync(this.file)) return;
      const raw = JSON.parse(fs.readFileSync(this.file, 'utf8').replace(/^\uFEFF/, ''));
      const entries = raw && typeof raw.entries === 'object' ? raw.entries : {};
      const now = Date.now();
      for (const [k, ts] of Object.entries(entries)) {
        const t = Number(ts);
        // 读的时候就丢掉过期项，避免文件越攒越多
        if (Number.isFinite(t) && now - t < this.ttlMs) this.map.set(k, t);
      }
    } catch {
      this.map.clear();
    }
  }

  /** 这条内容是否已经在这个会话里发过。 */
  has(item) {
    const fp = fingerprint(item);
    const ts = this.map.get(fp);
    if (ts === undefined) return false;
    if (Date.now() - ts >= this.ttlMs) {
      // 过期了：允许再次发送，同时清掉旧记录
      this.map.delete(fp);
      this.dirty = true;
      return false;
    }
    return true;
  }

  /** 标记为已发送（同时刷新时间戳，从"现在"重新计 TTL）。 */
  add(item) {
    this.map.set(fingerprint(item), Date.now());
    this.dirty = true;
    this.prune();
  }

  /** 批量标记。 */
  addAll(items) {
    for (const it of items || []) this.add(it);
  }

  /**
   * 从候选里挑出"没发过的"，并把挑中的标记为已发送。
   *
   * 这是主入口：worker 返回 N 条，这里过滤后剩下的是真正该发的。
   * @returns {{fresh: Array, skipped: number}}
   */
  filterFresh(items) {
    const fresh = [];
    let skipped = 0;
    for (const it of items || []) {
      if (this.has(it)) { skipped += 1; continue; }
      fresh.push(it);
    }
    return { fresh, skipped };
  }

  /** 超出容量时丢掉最旧的一批。 */
  prune() {
    if (this.map.size <= this.maxPerChat) return;
    const sorted = [...this.map.entries()].sort((a, b) => a[1] - b[1]);
    const drop = this.map.size - this.maxPerChat;
    for (let i = 0; i < drop; i++) this.map.delete(sorted[i][0]);
  }

  /**
   * 写盘。用"临时文件 + rename"避免写到一半崩了留下半个 JSON
   * （那种文件下次解析失败，去重记录就整个丢了）。
   */
  save() {
    if (!this.dirty) return;
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true });
      const payload = { chatKey: this.chatKey, updatedAt: new Date().toISOString(), entries: Object.fromEntries(this.map) };
      const tmp = `${this.file}.tmp`;
      fs.writeFileSync(tmp, JSON.stringify(payload), 'utf8');
      fs.renameSync(tmp, this.file);
      this.dirty = false;
    } catch {
      // 写不进去不影响发送：内存里的记录本轮仍然生效，只是重启后丢
    }
  }

  /** 当前记录条数（自检/排障用）。 */
  get size() {
    return this.map.size;
  }
}

export { DEFAULT_TTL_MS, DEFAULT_MAX_PER_CHAT };
