// 通用弹窗
//
// 关于层级（z-index）：弹窗是可以**嵌套**打开的 —— 比如「AI 润色」里点「存为新模板」，
// 会在润色弹窗之上再开一个命名弹窗。如果所有弹窗都写死同一个层级（app.css 里是 100），
// 后开的那个会被先开的盖住，用户就不得不先关掉外层的才能操作里层的。
// 所以这里不写死：层级按「当前打开栈里的位置」算，后开的永远在上面。
// 用位置而不是「打开时算好并定死」—— 中间某个弹窗关掉后，后面的会自动补位，不会串层。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

const Z_BASE = 100;              // 与 app.css 里 .modal-mask 的默认层级一致
const stack = ref([]);           // 当前打开着的弹窗 id（按打开先后）
let uid = 0;

export default {
  name: 'Modal',
  props: {
    title: { type: String, default: '' },
    wide: { type: Boolean, default: false },
  },
  emits: ['close'],
  setup() {
    const myId = ++uid;
    onMounted(() => { stack.value = [...stack.value, myId]; });
    onBeforeUnmount(() => { stack.value = stack.value.filter(i => i !== myId); });

    const zIndex = computed(() => Z_BASE + (stack.value.indexOf(myId) + 1) * 10);
    return { zIndex };
  },
  template: `
  <div class="modal-mask" :style="'z-index:' + zIndex" @click.self="$emit('close')">
    <div class="modal" :style="wide ? 'max-width: 860px' : ''">
      <div class="modal-head">
        <h3>{{ title }}</h3>
        <div class="spacer"></div>
        <button class="btn ghost sm" @click="$emit('close')">关闭</button>
      </div>
      <div class="modal-body"><slot></slot></div>
      <div class="modal-foot" v-if="$slots.foot"><slot name="foot"></slot></div>
    </div>
  </div>`,
};
