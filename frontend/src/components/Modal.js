// 通用弹窗
export default {
  name: 'Modal',
  props: {
    title: { type: String, default: '' },
    wide: { type: Boolean, default: false },
  },
  emits: ['close'],
  template: `
  <div class="modal-mask" @click.self="$emit('close')">
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
