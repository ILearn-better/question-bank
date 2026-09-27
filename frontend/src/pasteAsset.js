// 从剪贴板里挖出一张图 —— 反馈的「附图」和作业的「作业原件」共用这一份。
//
// 为什么单独抽出来：剪贴板里的形态比想象中杂（截图、微信复制的图、从文件夹复制的图文件、
// 网页里的 <img>、data: URL……），判断顺序也很微妙。这种逻辑一旦在两个地方各写一遍，
// 迟早会各修一半、行为不一致 —— 而它的失败方式又特别隐蔽（粘不进去、用户反复试）。
//
// 唯一的铁律：**绝不能吞掉用户已经选好的文字**。所以「剪贴板里除了图/链接还有别的文字」
// 一律返回 null（不插手），由调用方让默认粘贴照常发生。

/**
 * 判断这次粘贴该怎么处理。
 *
 * 返回：
 *   null                    → 不是贴图场景，调用方**什么都别做**（让默认粘贴照常发生）
 *   {kind:'file', file}     → 剪贴板里就是文件（截图、从微信/文件夹复制）
 *   {kind:'data', dataUrl}  → text/html 里是 data:image/…（本地就能解，不联网）
 *   {kind:'url', url}       → 只有一个图片链接；本服务不联网，调用方提示用户先存本地
 *   {kind:'otherfile', name}→ 复制的是别的文件（如 PDF）；反馈那里只能贴图片，
 *                             作业那里可以直接当作业原件收下
 *   {kind:'none'}           → 看得出想贴图，但抓不到（提示一句，别让人反复试）
 */
export function pickPastedAsset(e) {
  const dt = e.clipboardData;
  if (!dt) return null;
  // ① 剪贴板里真的有文件二进制（截图、从微信/QQ 复制的图、从文件夹复制的文件）—— 最该走的一条
  for (const it of dt.items || []) {
    if (it.kind === 'file' && it.type) {
      const f = it.getAsFile();
      if (f) return { kind: 'file', file: f, isImage: it.type.startsWith('image/') };
    }
  }
  for (const f of dt.files || []) {
    if (f.type) return { kind: 'file', file: f, isImage: f.type.startsWith('image/') };
  }

  let html = '', text = '';
  try { html = dt.getData('text/html') || ''; } catch (err) { /* 某些环境不给读 */ }
  try { text = (dt.getData('text/plain') || '').trim(); } catch (err) { /* 同上 */ }
  // 去掉链接之后还剩什么字 —— 用来判断「这是只复制了一个链接/一张图」还是「复制了一段文章」
  const noUrl = text.replace(/https?:\/\/\S+/gi, '').trim();
  // 有些复制源只给 text/html、不给 text/plain，所以还要把标签剥掉再看一遍里面有没有正文
  const htmlText = html.replace(/<[^>]*>/g, ' ').replace(/&nbsp;/gi, ' ')
    .replace(/https?:\/\/\S+/gi, ' ').trim();
  const srcOf = (s) => (String(s).match(/src\s*=\s*["']([^"']+)["']/i) || [])[1] || '';
  const src = srcOf(html);

  // ②③④ 只有「剪贴板里除了图/链接没别的文字」才算一次贴图操作。
  //     只要还带着别的文字（粘一段带图的网页文章、往正文里粘参考链接），一概不插手：
  //     把老师已经选好的文字吞掉，比少贴一张图糟糕得多 —— 那是在毁他的内容。
  if (noUrl || htmlText) return null;
  if (/^data:image\//i.test(src)) return { kind: 'data', dataUrl: src };
  if (/^https?:\/\//i.test(src)) return { kind: 'url', url: src };
  // 直接粘了个图片地址。只认「看着就是图片」的地址 —— 粘普通网址是正常的文字操作
  if (/^https?:\/\/\S+\.(png|jpe?g|gif|webp|bmp|heic|heif)(\?\S*)?$/i.test(text)) {
    return { kind: 'url', url: text };
  }
  if (/<img/i.test(html)) return { kind: 'none' };
  return null;
}

/** 剪贴板里只有链接时的统一说法。提示条是纯文本渲染的，别写 Markdown 的 **。 */
export function needLocalCopyMessage(url) {
  const shown = url && url.length > 48 ? url.slice(0, 48) + '…' : url;
  return '剪贴板里只有图片的链接，没有图片本身' + (shown ? `（${shown}）` : '') +
    '。请先右键「图片另存为」，或者干脆截个图，再按 Ctrl+V 贴进来。';
}
