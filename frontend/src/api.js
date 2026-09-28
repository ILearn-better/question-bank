// 统一 API 层：组件里不再出现裸 fetch。
// 这样将来改鉴权、改 baseURL、加统一错误处理，只需要动这一个文件。

const BASE = '';

async function request(path, options = {}) {
  const opts = { ...options };
  const isForm = opts.body instanceof FormData;
  opts.headers = isForm ? {} : { 'Content-Type': 'application/json', ...(opts.headers || {}) };

  let res;
  try {
    res = await fetch(BASE + path, opts);
  } catch (e) {
    throw new Error('连不上后端服务，请确认服务已启动');
  }

  if (!res.ok) {
    let msg = `请求失败 ${res.status}`;
    try {
      const data = await res.json();
      if (data && data.detail) {
        msg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
      }
    } catch (e) { /* 响应不是 JSON，保留默认文案 */ }
    // 把状态码带上：调用方需要区分「参数错」「对象已被删」这类情况
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  if (res.status === 204) return null;
  return res.json();
}

function qs(params) {
  const usable = Object.entries(params || {}).filter(([, v]) => v !== undefined && v !== null && v !== '');
  return usable.length ? '?' + usable.map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&') : '';
}

/** 下载类接口（导出文件）：fetch 成 Blob 再触发保存。
 *
 * 为什么不用 window.open（出卷那里就是这么干的）：
 *   导出失败时后端返回的是 JSON 报错（比如导出 PDF 需要本机 Word 但没有），
 *   window.open 会在新标签页里摊开一段 JSON，用户看不懂也不知道该怎么处理。
 *   走 fetch 就能把它变成一条提示。
 */
export async function download(path, filename, method = 'GET') {
  let res;
  try {
    res = await fetch(BASE + path, { method });
  } catch (e) {
    throw new Error('连不上后端服务，请确认服务已启动');
  }
  if (!res.ok) {
    let msg = `导出失败 ${res.status}`;
    try {
      const data = await res.json();
      if (data && data.detail) {
        msg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
      }
    } catch (e) { /* 不是 JSON，保留默认文案 */ }
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1000);
}

export const api = {
  get: (p, params) => request(p + qs(params)),
  post: (p, body) => request(p, { method: 'POST', body: JSON.stringify(body ?? {}) }),
  put: (p, body) => request(p, { method: 'PUT', body: JSON.stringify(body ?? {}) }),
  patch: (p, body) => request(p, { method: 'PATCH', body: JSON.stringify(body ?? {}) }),
  del: (p) => request(p, { method: 'DELETE' }),
  upload: (p, formData) => request(p, { method: 'POST', body: formData }),
};

// ---------------- 领域接口 ----------------
export const dashboardApi = {
  today: () => api.get('/api/dashboard/today'),
};

export const studentsApi = {
  list: (params) => api.get('/api/students', params),
  get: (id) => api.get(`/api/students/${id}`),
  create: (body) => api.post('/api/students', body),
  update: (id, body) => api.patch(`/api/students/${id}`, body),
  remove: (id) => api.del(`/api/students/${id}`),
  timeline: (id, limit = 100) => api.get(`/api/students/${id}/timeline`, { limit }),
  /** 学生的归档文件夹在哪、里面有多少东西（数的是磁盘，不是数据库登记）。 */
  folder: (id) => api.get(`/api/students/${id}/folder`),
  openFolder: (id) => api.post(`/api/students/${id}/folder/open`),
};

export const lessonsApi = {
  list: (params) => api.get('/api/lessons', params),
  create: (body) => api.post('/api/lessons', body),
  update: (id, body) => api.patch(`/api/lessons/${id}`, body),
  complete: (id) => api.post(`/api/lessons/${id}/complete`),
  remove: (id) => api.del(`/api/lessons/${id}`),
  monthly: (period) => api.get('/api/billing/monthly', { period }),
};

export const feedbackApi = {
  get: (lessonId) => api.get(`/api/lessons/${lessonId}/feedback`),
  save: (lessonId, body) => api.put(`/api/lessons/${lessonId}/feedback`, body),
  remove: (lessonId) => api.del(`/api/lessons/${lessonId}/feedback`),

  /** 反馈模板（四段的可复用文本 + 快捷短语）。存数据库，用户可自己加。 */
  templates: () => api.get('/api/feedback-templates'),
  createTemplate: (body) => api.post('/api/feedback-templates', body),
  removeTemplate: (id) => api.del(`/api/feedback-templates/${id}`),

  /** 润色模板：一整篇文档的格式与文风参考，润色时整段进提示词。
   *  和上面的 templates 是两回事 —— 那边是「录反馈时的快捷短语」，这边是「成文长什么样」。 */
  docTemplates: () => api.get('/api/feedback-doc-templates'),
  createDocTemplate: (body) => api.post('/api/feedback-doc-templates', body),
  removeDocTemplate: (id) => api.del(`/api/feedback-doc-templates/${id}`),

  /** 反馈配图上传。返回可直接放进 images 数组的 URL。
   *  **必须带 lessonId**：图片要归到「学生 / 上课日期」目录下（后端 services/storage.py）。 */
  uploadImage: (lessonId, formData) =>
    api.upload(`/api/feedbacks/image` + qs({ lesson_id: lessonId }), formData),

  /** 真删一张配图（连磁盘上的文件一起）。已保存的反馈若还在引用它，服务端会拒绝删。 */
  deleteImage: (lessonId, url) =>
    api.post('/api/feedbacks/image/remove', { lesson_id: lessonId, url }),

  /** AI 整篇润色：四段记录进去，一整篇文档出来（建议稿，需老师确认）。
   *  注意：会把内容发到第三方 AI 服务，界面必须先提示。 */
  polish: (lessonId, body) => api.post(`/api/lessons/${lessonId}/feedback/polish`, body),

  /** 导出 txt / docx / pdf。
   *  source：auto（默认，有整篇就导整篇）/ doc（强制整篇）/ fields（强制四段）。 */
  downloadExport: (lessonId, format, filename, source = 'auto') =>
    download(`/api/lessons/${lessonId}/feedback/export` + qs({ format, source }), filename),
};

/** 课后补充：老师挑好、下次上课打印给学生的材料（记在今天那节课里）。
 *  与作业方向相反：作业是收（他交回来的），这里是发（我准备给他的）。 */
export const supplementsApi = {
  list: (lessonId) => api.get(`/api/lessons/${lessonId}/supplements`),
  create: (lessonId, body) => api.post(`/api/lessons/${lessonId}/supplements`, body),
  update: (id, body) => api.patch(`/api/supplements/${id}`, body),
  remove: (id) => api.del(`/api/supplements/${id}`),
  /** 这个学生还没给的（学生详情页那句提醒）。 */
  pending: (studentId) => api.get(`/api/students/${studentId}/supplements/pending`),
  /** 出学生版 PDF（不含答案）：后端同时把文件写进学生的归档目录，并把状态置成「已给」。
   *  走 download() 而不是 window.open —— 这样失败时能把后端的报错变成一句提示。
   *  ⚠️ 这个接口是 **POST**（它会写库：状态改成已给、记一次题目使用），
   *  而 download() 默认 GET —— 不传 method 会 404（踩过）。 */
  print: (id, filename) => download(`/api/supplements/${id}/print`, filename, 'POST'),
};

/** 上课文件（讲义 / 课件 / 试卷）：上传后由后端在本机抽文字，供 AI 润色当参考资料。
 *  ⚠️ 接的是纯文本接口，模型不收文件本身 —— 所以发出去的是**抽出来的文字**。 */
export const lessonFilesApi = {
  /** role: material（上课材料）/ homework（学生作业原件）—— 两份清单互相看不见。 */
  list: (lessonId, role = 'material') =>
    api.get(`/api/lessons/${lessonId}/files`, { role }),
  upload: (lessonId, formData, role = 'material') =>
    api.upload(`/api/lessons/${lessonId}/files` + qs({ role }), formData),
  remove: (id) => api.del(`/api/lesson-files/${id}`),
  /** 原件地址。inline=true 时**图片**会就地显示（预览用），其它类型照样下载。 */
  rawUrl: (id, inline) => `/api/lesson-files/${id}/raw` + (inline ? '?inline=1' : ''),
  /** 某个学生的全部资料（按上课日期分组）+ 磁盘归档路径。做学生情况分析时用它。 */
  byStudent: (studentId) => api.get(`/api/students/${studentId}/files`),
};

/** AI 润色的接口配置（地址 / 模型 / 密钥）。密钥只存本机，接口一律脱敏返回。 */
export const aiApi = {
  /** 服务商预设（DeepSeek / 通义 / Kimi / 本机 Ollama …），选中后自动填好地址与模型。 */
  providers: () => api.get('/api/ai/providers'),
  settings: () => api.get('/api/ai/settings'),
  save: (body) => api.put('/api/ai/settings', body),
  /** 测试连接。带上表单里的值，这样**没保存也能先试**。 */
  test: (body) => api.post('/api/ai/test', body || {}),
};

export const curriculumApi = {
  list: () => api.get('/api/curricula'),
  create: (body) => api.post('/api/curricula', body),
  nodes: (id) => api.get(`/api/curricula/${id}/nodes`),
};

export const papersApi = {
  /** 筛题库。响应是 {total, items}，与录题页在用的 GET /questions（纯数组）不同。 */
  search: (params) => api.get('/api/questions/search', params),
  /** 库里实际用过的标签及次数 —— 出卷筛选列表用这个，而不是让用户自由输。 */
  tags: () => api.get('/api/questions/tags'),
  /** 库里实际用过的知识点及次数。与 tags 同理：有它才能「录了什么就能按什么筛」。 */
  knowledgePoints: () => api.get('/api/questions/knowledge-points'),
  /** 导出/预览合成一个 URL：HTML 是 inline（直接看），Word/PDF 是下载。
   *  这样前端只要 window.open，不用把二进制读进 fetch 再自己造 Blob。 */
  exportUrl: (params) => '/api/papers/export' + qs(params),
};

export const notesApi = {
  list: (params) => api.get('/api/notes', params),
  get: (id) => api.get(`/api/notes/${id}`),
  create: (body) => api.post('/api/notes', body),
  /** 自动保存走这个：只传改动过的字段。拖动（folder_id + position）也走它。 */
  update: (id, body) => api.patch(`/api/notes/${id}`, body),
  remove: (id) => api.del(`/api/notes/${id}`),
  uploadImage: (formData) => api.upload('/api/notes/image', formData),
  /** 把现成的 Word / PDF / HTML / 文本 变成一篇笔记（原件也一起存下来）。 */
  importDoc: (formData, folderId) =>
    api.upload('/api/notes/import-doc' + qs({ folder_id: folderId }), formData),
  /** 导出能力：PDF 要本机有 Word、公式渲染要 node + Office 的 XSLT。
   *  先问一次，界面上就能把按钮状态和原因写清楚，而不是等用户点了才报错。 */
  exportCaps: () => api.get('/api/notes/export/caps'),
  /** 导出单篇笔记。Word / PDF 都是服务端生成，这里只管下载。
   *  with_answer=false 出学生版：正文里插的题**答案整段不会被写进文件**。 */
  downloadExport: (id, params, filename) =>
    download(`/api/notes/${id}/export` + qs(params), filename),
  /** 把一道题库里的题变成能插进正文的块（题干 + 答案 + 图片副本）。 */
  questionBlock: (qid, index) => api.post('/api/notes/question-block', { qid, index }),
};

/** 笔记目录树（体系 → 目录… → 笔记）。 */
export const noteFoldersApi = {
  /** 整棵树一次拿全：目录结构 + 每个目录下的笔记（只含轻量字段）。
   *  笔记量级不大，一次拿全才能做到「点开目录立刻看到内容」，不必逐个目录去拉。 */
  tree: () => api.get('/api/note-folders/tree'),
  create: (body) => api.post('/api/note-folders', body),
  /** 改名 / 移动 / 排序共用：移动传 {parent_id, position}，position 是**放在第几位**。 */
  update: (id, body) => api.patch(`/api/note-folders/${id}`, body),
  /** 删目录**不删内容**：里面的子目录与笔记上移到父目录（返回值里有移了几样）。 */
  remove: (id) => api.del(`/api/note-folders/${id}`),
};

/** 笔记备份 / 迁移：一个 zip 装下目录、正文、板书、配图，能在别的设备上导回来。 */
export const notesBackupApi = {
  /** 导出走 download()：失败时后端给的是中文原因（不是摊开一页 JSON）。 */
  export: (filename) => download('/api/notes-backup/export', filename),
  /** 导入。dryRun 只试算不写库 —— 拿不准的时候先让它算一遍。
   *  overwrite 默认 false：同 id 的笔记跳过（保留本机那份），别无声盖掉较新的内容。 */
  import: (formData, { overwrite = false, dryRun = false } = {}) =>
    api.upload('/api/notes-backup/import' + qs({
      overwrite: overwrite ? 'true' : undefined,
      dry_run: dryRun ? 'true' : undefined,
    }), formData),
};

export const abilityApi = {
  dims: () => api.get('/api/ability-dims'),
  createDim: (body) => api.post('/api/ability-dims', body),
  updateDim: (id, body) => api.patch(`/api/ability-dims/${id}`, body),
  disableDim: (id) => api.del(`/api/ability-dims/${id}`),
};

/** 学生作业：交没交 / 原件 / 老师按几个维度打分。
 *  维度是**另一套**（homework-dims），与课堂的 ability-dims 分开 —— 两边永远不混。 */
export const homeworkApi = {
  get: (lessonId) => api.get(`/api/lessons/${lessonId}/homework`),
  save: (lessonId, body) => api.put(`/api/lessons/${lessonId}/homework`, body),
  /** 「这节没布置作业 / 不用记」= 整条撤掉（不是存一个空状态）。 */
  remove: (lessonId) => api.del(`/api/lessons/${lessonId}/homework`),
  /** 作业雷达：最新一次 vs 上一次，外加交/迟交/未交的条数。 */
  radar: (studentId) => api.get(`/api/students/${studentId}/homework-radar`),
  dims: () => api.get('/api/homework-dims'),
  createDim: (body) => api.post('/api/homework-dims', body),
  updateDim: (id, body) => api.patch(`/api/homework-dims/${id}`, body),
  disableDim: (id) => api.del(`/api/homework-dims/${id}`),
};

export const systemApi = {
  info: () => api.get('/api/system/info'),
  backup: () => api.post('/api/system/backup'),
  backups: () => api.get('/api/system/backups'),
  fkCheck: () => api.get('/api/system/foreign-key-check'),
};
