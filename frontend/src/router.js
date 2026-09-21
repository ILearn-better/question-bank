// 路由。
//
// 用 hash 模式（#/students）而不是 history 模式：
// 后端只是静态文件挂载，没有 SPA fallback。hash 模式下刷新任何子页面都不会 404，
// 不需要为此在后端加通配路由 —— 少一处能出错的地方。
import { createRouter, createWebHashHistory } from 'vue-router';

const routes = [
  {
    path: '/',
    name: 'dashboard',
    component: () => import('./views/Dashboard.js'),
    meta: { title: '今日工作台' },
  },
  {
    path: '/students',
    name: 'students',
    component: () => import('./views/Students.js'),
    meta: { title: '学生' },
  },
  {
    path: '/students/:id',
    name: 'student-detail',
    component: () => import('./views/StudentDetail.js'),
    meta: { title: '学生详情' },
  },
  {
    path: '/schedule',
    name: 'schedule',
    component: () => import('./views/Schedule.js'),
    meta: { title: '课表与课时费' },
  },
  {
    path: '/papers',
    name: 'papers',
    component: () => import('./views/Papers.js'),
    meta: { title: '出卷' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('./views/Settings.js'),
    meta: { title: '设置' },
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
];

export const router = createRouter({
  history: createWebHashHistory(),
  routes,
});

// 品牌只出现在用户可见的地方：页面标题就是其中一处。
router.afterEach((to) => {
  document.title = to.meta && to.meta.title ? `拾课 · ${to.meta.title}` : '拾课';
});

export default router;
