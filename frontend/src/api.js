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
export async function download(path, filename) {
  let res;
  try {
    res = await fetch(BASE + path);
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
  /** 自动保存走这个：只传改动过的字段。 */
  update: (id, body) => api.patch(`/api/notes/${id}`, body),
  remove: (id) => api.del(`/api/notes/${id}`),
  uploadImage: (formData) => api.upload('/api/notes/image', formData),
  /** 导出能力：PDF 要本机有 Word、公式渲染要 node + Office 的 XSLT。
   *  先问一次，界面上就能把按钮状态和原因写清楚，而不是等用户点了才报错。 */
  exportCaps: () => api.get('/api/notes/export/caps'),
  /** 导出单篇笔记。Word / PDF 都是服务端生成，这里只管下载。 */
  downloadExport: (id, params, filename) =>
    download(`/api/notes/${id}/export` + qs(params), filename),
};

export const abilityApi = {
  dims: () => api.get('/api/ability-dims'),
  createDim: (body) => api.post('/api/ability-dims', body),
  updateDim: (id, body) => api.patch(`/api/ability-dims/${id}`, body),
  disableDim: (id) => api.del(`/api/ability-dims/${id}`),
};

export const systemApi = {
  info: () => api.get('/api/system/info'),
  backup: () => api.post('/api/system/backup'),
  backups: () => api.get('/api/system/backups'),
  fkCheck: () => api.get('/api/system/foreign-key-check'),
};
