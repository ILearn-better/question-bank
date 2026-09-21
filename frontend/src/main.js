// 拾课（shike）· 前端入口
import { createApp } from 'vue';
import App from './App.js';
import router from './router.js';

createApp(App).use(router).mount('#app');
