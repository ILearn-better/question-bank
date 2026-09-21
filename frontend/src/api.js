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
    throw new Error(msg);
  }
  if (res.status === 204) return null;
  return res.json();
}

function qs(params) {
  const usable = Object.entries(params || {}).filter(([, v]) => v !== undefined && v !== null && v !== '');
  return usable.length ? '?' + usable.map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&') : '';
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
  /** 导出/预览合成一个 URL：HTML 是 inline（直接看），Word/PDF 是下载。
   *  这样前端只要 window.open，不用把二进制读进 fetch 再自己造 Blob。 */
  exportUrl: (params) => '/api/papers/export' + qs(params),
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
