// 能力雷达图（手绘 SVG，不引图表库）。
//
// 关键设计：**同时画本次和上次两条**。
// 一张孤立的雷达图家长其实看不懂好坏；两条叠在一起，「变化」才看得见 ——
// 这正是家长报告要传递的核心信息。

const TAU = Math.PI * 2;

export default {
  name: 'AbilityRadar',
  props: {
    dims: { type: Array, default: () => [] },   // [{ dim_id, name, latest, previous }]
    size: { type: Number, default: 300 },
    max: { type: Number, default: 5 },
  },
  computed: {
    n() { return this.dims.length; },
    cx() { return this.size / 2; },
    cy() { return this.size / 2; },
    radius() { return this.size / 2 - 52; },
    usable() { return this.n >= 3; },
    rings() {
      const out = [];
      for (let r = 1; r <= this.max; r++) {
        out.push({ level: r, points: this.polygon(() => r) });
      }
      return out;
    },
    axes() {
      return this.dims.map((d, i) => {
        const a = this.angle(i);
        return {
          x2: this.cx + this.radius * Math.cos(a),
          y2: this.cy + this.radius * Math.sin(a),
        };
      });
    },
    latestPoints() { return this.polygon((i) => this.dims[i].latest); },
    previousPoints() { return this.polygon((i) => this.dims[i].previous); },
    hasPrevious() { return this.dims.some((d) => d.previous !== null && d.previous !== undefined); },
    labels() {
      return this.dims.map((d, i) => {
        const a = this.angle(i);
        const r = this.radius + 22;
        const x = this.cx + r * Math.cos(a);
        const y = this.cy + r * Math.sin(a);
        const cos = Math.cos(a);
        let anchor = 'middle';
        if (cos > 0.3) anchor = 'start';
        else if (cos < -0.3) anchor = 'end';
        return { name: d.name, x, y, anchor, latest: d.latest };
      });
    },
  },
  methods: {
    angle(i) { return -Math.PI / 2 + (TAU * i) / this.n; },
    polygon(valueOf) {
      return this.dims
        .map((d, i) => {
          const raw = valueOf(i, d);
          const v = raw === null || raw === undefined ? 0 : raw;
          const a = this.angle(i);
          const r = (this.radius * Math.min(v, this.max)) / this.max;
          return `${(this.cx + r * Math.cos(a)).toFixed(1)},${(this.cy + r * Math.sin(a)).toFixed(1)}`;
        })
        .join(' ');
    },
  },
  template: `
  <div>
    <div v-if="!usable" class="empty">
      至少要有 3 个能力维度才能画雷达图<br>
      <span class="small">当前 {{ n }} 个，可在「设置」里增补维度</span>
    </div>
    <template v-else>
      <svg :viewBox="'0 0 ' + size + ' ' + size" width="100%" :style="{ maxWidth: size + 'px', display: 'block', margin: '0 auto' }">
        <polygon v-for="ring in rings" :key="'r' + ring.level"
                 :points="ring.points" fill="none" stroke="#e4e7ec" stroke-width="1" />
        <line v-for="(ax, i) in axes" :key="'a' + i"
              :x1="cx" :y1="cy" :x2="ax.x2" :y2="ax.y2" stroke="#e4e7ec" stroke-width="1" />
        <polygon v-if="hasPrevious" :points="previousPoints"
                 fill="rgba(152,162,179,.18)" stroke="#98a2b3" stroke-width="1.2" stroke-dasharray="4 3" />
        <polygon :points="latestPoints" fill="rgba(37,99,235,.16)" stroke="#2563eb" stroke-width="1.6" />
        <text v-for="(lb, i) in labels" :key="'l' + i"
              :x="lb.x" :y="lb.y" :text-anchor="lb.anchor" dominant-baseline="central"
              font-size="12" fill="#5b6472">{{ lb.name }}</text>
      </svg>
      <div class="small muted" style="text-align:center; margin-top:6px">
        <span style="color:#2563eb">■</span> 本次
        <span v-if="hasPrevious" style="margin-left:10px; color:#98a2b3">▨ 上次</span>
      </div>
    </template>
  </div>`,
};
