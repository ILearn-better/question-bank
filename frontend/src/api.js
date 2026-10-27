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

/** 课表 Excel 导入。预览与确认是两步 —— 见 routers/lesson_import.py 的说明。 */
export const scheduleImportApi = {
  /** 模板下载：交给 download() 用（它会处理失败时的 JSON 报错） */
  templateUrl: () => '/api/lessons/import/template',
  preview: (formData) => api.upload('/api/lessons/import/preview', formData),
  commit: (body) => api.post('/api/lessons/import/commit', body),
};

export const feedbackApi = {
  get: (lessonId) => api.get(`/api/lessons/${lessonId}/feedback`),
  save: (lessonId, body) => api.put(`/api/lessons/${lessonId}/feedback`, body),
  remove: (lessonId) => api.del(`/api/lessons/${lessonId}/feedback`),

  /** 反馈模板（五段的可复用文本 + 快捷短语）。存数据库，用户可自己加。 */
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

  /** AI 整篇润色：五段记录进去，一整篇文档出来（建议稿，需老师确认）。
   *  注意：会把内容发到第三方 AI 服务，界面必须先提示。 */
  polish: (lessonId, body) => api.post(`/api/lessons/${lessonId}/feedback/polish`, body),

  /** 导出 txt / docx / pdf。
   *  source：auto（默认，有整篇就导整篇）/ doc（强制整篇）/ fields（强制五段）。 */
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

/** 课前备课：老师上课前做的准备（一节一条，与反馈/作业/课后补充并列）。
 *  结构化四段（目标/重难点/流程/准备材料）+ 挑的材料 + 与笔记库双向联动（note_id）。 */
export const prepApi = {
  get: (lessonId) => api.get(`/api/lessons/${lessonId}/prep`),
  /** 存/改：四个字段 + note_id 都是「没传 = 保持」；items 传了（含空数组）= 整组替换。 */
  save: (lessonId, body) => api.put(`/api/lessons/${lessonId}/prep`, body),
  remove: (lessonId) => api.del(`/api/lessons/${lessonId}/prep`),
  /** 把这份备课「写回」笔记库（已关联就更新那篇正文，否则新建落未归档）。 */
  saveAsNote: (lessonId) => api.post(`/api/lessons/${lessonId}/prep/save-as-note`),
};

/** 上课文件（讲义 / 课件 / 试卷）：上传后由后端在本机抽文字，供 AI 润色当参考资料。
 *  ⚠️ 接的是纯文本接口，模型不收文件本身 —— 所以发出去的是**抽出来的文字**。 */
export const lessonFilesApi = {
  /** role: material（上课材料）/ homework（学生作业原件）/ prep（备课材料）—— 三份清单互相看不见。
   *  ⚠️ prep 只归档、**不抽文字**，且不限格式（老师电脑上做好的 .lnk 快捷方式走这一类）。 */
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
  /** 库里实际用过的标签及次数 —— 出卷筛选列表用这个，而不是让用户自由输。
   *  传 curriculumId 则只统计该体系里用过的（笔记插题按体系筛时要跟着变）。 */
  tags: (curriculumId) => api.get('/api/questions/tags', { curriculum_id: curriculumId }),
  /** 库里实际用过的知识点及次数。与 tags 同理：有它才能「录了什么就能按什么筛」。 */
  knowledgePoints: (curriculumId) =>
    api.get('/api/questions/knowledge-points', { curriculum_id: curriculumId }),
  /** 导出/预览合成一个 URL：HTML 是 inline（直接看），Word/PDF 是下载。
   *  这样前端只要 window.open，不用把二进制读进 fetch 再自己造 Blob。 */
  exportUrl: (params) => '/api/papers/export' + qs(params),
};

/** 卷种样式模板（高考 / 中考 / DSE / A-Level，四套内置 + 自己复制改的）。
 *  返回的每一项都带**解析后**的 paper / style / sections —— 界面显示的字段
 *  和导出时用的字段是同一套，不会出现「界面上改了却没生效」。 */
export const paperTemplatesApi = {
  list: () => api.get('/api/paper-templates'),
  get: (id) => api.get(`/api/paper-templates/${id}`),
  create: (body) => api.post('/api/paper-templates', body),
  /** 复制一份再改 —— 「可扩展」真正好用的路径：挑一套最像的复制，比从零填省事。 */
  copy: (sourceId, name) => api.post('/api/paper-templates/copy', { source_id: sourceId, name }),
  /** 改模板：**没传的块保持原样**（只改名字不该把排版重置回默认）。 */
  update: (id, body) => api.patch(`/api/paper-templates/${id}`, body),
  /** 内置的改得了、删不了（删了下次启动种子又建回来）。 */
  remove: (id) => api.del(`/api/paper-templates/${id}`),
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

/** 考试情况记录：**学生级**（不挂某节课）—— 考试日期和上课日期本来就不一定对得上。
 *  日期可以是「2026-09-15」，也可以是「2026-09」「9 月中旬」这种不精确的说法。
 *  试题 / 答题卡**先传暂存区，保存这次考试时才归档** —— 这样填表过程中
 *  随便改日期/类型都不会牵动文件（归档目录名里带着日期和类型）。 */
export const examsApi = {
  list: (studentId) => api.get(`/api/students/${studentId}/exams`),
  create: (body) => api.post('/api/exams', body),
  /** 改：**没传的字段 = 不改**（想清空就显式传 null / 空串）。 */
  update: (id, body) => api.patch(`/api/exams/${id}`, body),
  /** 删这次考试 —— 连同它的试题/答题卡一起（那些是这次考试的专属材料）。 */
  remove: (id) => api.del(`/api/exams/${id}`),

  /** 附件进暂存区，返回 cache_key（保存时再搬进正式目录）。
   *  role：paper（试题）/ sheet（答题卡）/ other（其他往来材料）。 */
  upload: (formData, role = 'other') =>
    api.upload('/api/exams/upload' + qs({ role }), formData),
  /** 真删一份**还没保存**的暂存附件。
   *  ⚠️ 只在界面上把它从列表里拿掉是没用的：文件不在数据库里，
   *  任何按记录的清理都碰不到它，会一直躺在硬盘上。 */
  removeCache: (cacheKey) => api.post('/api/exams/upload/remove', { cache_key: cacheKey }),

  /** 已归档附件：删 / 改归类 / 取原件（inline=1 时图片就地显示）。 */
  removeFile: (id) => api.del(`/api/exam-files/${id}`),
  setFileRole: (id, role) => api.patch(`/api/exam-files/${id}`, { role }),
  rawUrl: (id, inline) => `/api/exam-files/${id}/raw` + (inline ? '?inline=1' : ''),

  /** 考情分析模板：一次分析该写哪几段（内置两套，可改不可删）。 */
  templates: () => api.get('/api/exam-analysis-templates'),
  createTemplate: (body) => api.post('/api/exam-analysis-templates', body),
  updateTemplate: (id, body) => api.patch(`/api/exam-analysis-templates/${id}`, body),
  removeTemplate: (id) => api.del(`/api/exam-analysis-templates/${id}`),
};

/** 整体规划（学生级）：正文是 Markdown，点开某条才取完整正文（列表只给摘要）。 */
export const plansApi = {
  list: (studentId) => api.get(`/api/students/${studentId}/plans`),
  /** 单条详情：带完整正文 + 原件清单 —— 「每个规划点开才能详细查看」。 */
  get: (id) => api.get(`/api/plans/${id}`),
  create: (body) => api.post('/api/plans', body),
  /** 改：**没传的字段 = 不改**；files 是增量（只提交这次新传的）。 */
  update: (id, body) => api.patch(`/api/plans/${id}`, body),
  /** 删这条规划 —— 连同它的原件一起。 */
  remove: (id) => api.del(`/api/plans/${id}`),

  /** 原件进暂存区，顺手返回转好的 Markdown（前端决定填进正文还是追加）。
   *  ⚠️ 返回的 markdown 可能是空串 —— 那表示「这份转不出正文」（扫描件 / 图片 / 压缩包…），
   *  此时 note 里写着原因，原件照样归档，界面要照实说。 */
  upload: (formData) => api.upload('/api/plans/upload', formData),
  /** 真删一份**还没保存**的暂存原件（只在界面上拿掉是没用的，文件会一直留着）。 */
  removeCache: (cacheKey) => api.post('/api/plans/upload/remove', { cache_key: cacheKey }),

  /** 已归档原件：删 / 取回（一律下载 —— Word/PDF 就地打开多半是白屏或乱码）。 */
  removeFile: (id) => api.del(`/api/plan-files/${id}`),
  rawUrl: (id) => `/api/plan-files/${id}/raw`,
};

/** AI 批改作业：一份作业一条任务，逐题出「对 / 部分对 / 错 / 空白 / 判不了」初稿，
 *  老师核对（或改判定）之后才算数（见 docs/AI批改作业-实现计划.md）。
 *  ⚠️ 坐标一律是**按页归一化 0~1000 的整数**，前端 viewBox="0 0 1000 1000" 直接吃，不做换算。 */
export const gradeApi = {
  create: (body) => api.post('/api/homework-grades', body),
  /** 某节课的批改记录（只给进度，不带 items —— 详情要按 gid 单独取）。 */
  list: (lessonId) => api.get('/api/homework-grades', { lesson_id: lessonId }),
  get: (gid) => api.get(`/api/homework-grades/${gid}`),
  /** 开批**之前**改判定依据（AI 自己解 / 老师给的答案 / 评分细则）。
   *  ⚠️ 已经批出结果的不能再改，只能「重新批改」—— 否则旧判定是按旧依据批的。 */
  patchSetup: (gid, body) => api.patch(`/api/homework-grades/${gid}`, body),
  /** 开始/重跑。**幂等**：已经在跑时返回当前进度，不会重复开一个任务。 */
  // force：确认要覆盖老师已有的劳动（已确认 / 逐题复核 / 批注）时才带。
  // 不带时服务端会对这种情况返回 409 并说明会清掉什么 —— 让这个决定必须经过一次确认，
  // 而不是点一下按钮就没了。
  run: (gid, force) =>
    api.post(`/api/homework-grades/${gid}/run${force ? '?force=true' : ''}`),
  /** 终止。**只受理「真的在跑」的那一批**，没在跑时后端给 409（detail 里是理由）。
   *  ⚠️ 语义是「受理」而不是「立刻停」—— 在跑的那几页 AI 调用掐不断，
   *     拿到 200 之后要继续轮询，等状态落到 reviewing。 */
  cancel: (gid) => api.post(`/api/homework-grades/${gid}/cancel`),
  /** 整份确认（此后才进统计口径）。note 可以留空。 */
  confirm: (gid, note) => api.post(`/api/homework-grades/${gid}/confirm`, { note }),

  /** 页图 PNG，直接当 img.src 用。 */
  pageUrl: (gid, pno) => `/api/homework-grades/${gid}/page/${pno}`,
  /** 导出**带批改痕迹的副本**：fmt=pdf 整份多页、fmt=png 单页。原件不动。 */
  exportUrl: (gid, fmt = 'pdf', page = 1) =>
    `/api/homework-grades/${gid}/export?fmt=${encodeURIComponent(fmt)}&page=${Number(page) || 1}`,

  /** ★ 导出到**作业存档**：存进 `students/<学生>/<日期>/批改/`，
   *  之后在「学生资料 → 改作业」里能回看。
   *  ⚠️ 与上面的 exportUrl 是两件事：那个是下载到电脑上（发家长用），
   *     这个是留在库里（以后回看用）。后端自己出 PDF，前端只发一个 grade_id。 */
  archive: (gid, body) => api.post('/api/grade-archives/generate',
    Object.assign({ grade_id: gid }, body || {})),

  /** 一题一区域：老师框出每道题的作答道在哪，一框一次判；没框的页仍整页判。 */
  listRegions: (gid) => api.get(`/api/homework-grades/${gid}/regions`),
  /** **整页替换**：传这一页的完整框列表，传空数组 = 清空这一页的框。 */
  putRegions: (gid, body) => api.put(`/api/homework-grades/${gid}/regions`, body),
  /** 沿用同一节课另一份的框（一套卷只画一次）。跨课时会 422。 */
  copyRegions: (gid, fromGradeId) =>
    api.post(`/api/homework-grades/${gid}/regions/copy`, { from_grade_id: fromGradeId }),
  /** 一块切出来的样子（现裁、不落盘）。**保存前就能看**，切歪了当场发现。
   *  spec 形如 `1:40,540,1000,800`，跨页多段用 `;` 连接（与 /documents 那个同名接口同一套语法）。 */
  regionImageUrl: (gid, spec) =>
    `/api/homework-grades/${gid}/region-image?spec=${encodeURIComponent(spec)}`,
  /** 整页不送 AI（**整份替换**，与框同一套口径）。只存页号，不动框。 */
  setSkippedPages: (gid, pages) =>
    api.patch(`/api/homework-grades/${gid}/skipped-pages`, { pages }),

  /** 改一题的判定 / 学生作答 / 错因 / 考点 / 核对状态。
   *  ⚠️ 改 student_answer 会**作废这一题的判定并重跑**（旧的 evidence 引用已经不存在了）。 */
  patchItem: (iid, body) => api.patch(`/api/grade-items/${iid}`, body),
  /** 只重批这一题（不牵动整份）。 */
  rerunItem: (iid) => api.post(`/api/grade-items/${iid}/rerun`),
  /** 错题回存题库：不自动建题，question_id 由老师指定，tags 是追加的标签。 */
  toBank: (iid, body) => api.post(`/api/grade-items/${iid}/to-bank`, body),

  /** 原件上的标注：新增 / 改 / 删。 */
  addMark: (gid, body) => api.post(`/api/homework-grades/${gid}/marks`, body),
  /** **一次补发多条**。给批改页的本地草稿队列用：老师画完立刻关页面时，
   *  那些没送出去的笔迹会攒在本机，下次打开一次性补上（见 src/gradeDraft.js）。
   *  ⚠️ 服务端**不去重** —— 调用方要保证只发「服务端还没有的」。 */
  addMarks: (gid, marks) => api.post(`/api/homework-grades/${gid}/marks/batch`, { marks }),
  patchMark: (mid, body) => api.patch(`/api/grade-marks/${mid}`, body),
  removeMark: (mid) => api.del(`/api/grade-marks/${mid}`),
};

export const systemApi = {
  info: () => api.get('/api/system/info'),
  backup: () => api.post('/api/system/backup'),
  backups: () => api.get('/api/system/backups'),
  fkCheck: () => api.get('/api/system/foreign-key-check'),
};

/** ★ 批改文件留存：「学生资料 → 改作业」的存档。
 *  两条写入口 —— generate（从批改出 PDF）/ upload（老师手传），读只有一套。 */
export const archiveApi = {
  list: (studentId) => api.get(`/api/students/${studentId}/grade-archives`),
  get: (id) => api.get(`/api/grade-archives/${id}`),
  /** 把一份批改导出成带笔迹的 PDF 并存进学生目录（后端出 PDF，前端只发 grade_id）。 */
  generate: (body) => api.post('/api/grade-archives/generate', body),
  /** 手传一份文件（线下批好的卷子、别处拿到的答案…）。
   *  FormData 里带 student_id / archived_on / grade_id / grader / note。 */
  upload: (formData) => api.upload('/api/grade-archives/upload', formData),
  remove: (id) => api.del(`/api/grade-archives/${id}`),
  /** 原件地址。inline=1 时图片与 PDF 就地显示（预览用），其它类型照样下载。 */
  rawUrl: (id, inline) => `/api/grade-archives/${id}/raw` + (inline ? '?inline=1' : ''),
};
