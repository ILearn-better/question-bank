# -*- coding: utf-8 -*-
r"""一次性补丁：给 A 页面的挂载检查补「分类标签」断言。"""
import io


def sub(path, old, new):
    s = io.open(path, encoding='utf-8').read()
    assert old in s, '没找到锚点：%s :: %r' % (path, old[:50])
    io.open(path, 'w', encoding='utf-8', newline='\n').write(s.replace(old, new, 1))
    print('patched', path)


P = 'grade_mount_check.js'

# 1) item() 默认值加 labels
sub(P,
    "  figure_note: '', status: 'pending', question_id: null,\n"
    "  block_kind: '', tail_note: '',\n}, o);",
    "  figure_note: '', status: 'pending', question_id: null,\n"
    "  block_kind: '', tail_note: '',\n"
    "  labels: {},                 // 0031：模型给的分类标签（学段/体系/知识点/考法）\n}, o);")

# 2) 第 1 题给一组标签（**故意乱序 + 多一个键**，用来验顺序固定与丢多余键）
sub(P,
    "    item({ id: 101, seq: 1, q_no: '1', qtype: '解答题', verdict: 'correct',",
    "    item({ id: 101, seq: 1, q_no: '1', qtype: '解答题', verdict: 'correct',\n"
    "      labels: { exam: '公式变形', stage: '初中', kp: '一元二次方程',\n"
    "                system: '数与代数', difficulty: '中档' },")

# 3) 断言
OLD = """    check('★ 只跳过去，**不自动把这块设成不判**（模型说像卷头也可能读错）',
      vm.regions.filter((r) => r.skipped).length, 0);"""

NEW = r"""    check('★ 只跳过去，**不自动把这块设成不判**（模型说像卷头也可能读错）',
      vm.regions.filter((r) => r.skipped).length, 0);

  /* ============ 模型给的分类标签（0031） ============ */
  /* 用户原话：「ai输出中要附带对这道题的标签(例如:学段\知识点\体系\考法等)」。
   * 与工作台同一组断言（两个界面都要显示，规则必须一致）。 */
  vm.detail.items = vm.detail.items.map((x, i) => Object.assign({}, x,
    i === 0 ? { labels: { exam: '公式变形', stage: '初中', kp: '一元二次方程',
                          system: '数与代数', difficulty: '中档' } } : {}));
  await sleep(150);
  const lbs = Array.from(D.querySelectorAll('.g-card .g-label')).map((e) => e.textContent.trim());
  checkTrue('★ A 页面也显示了分类标签', lbs.length >= 4, lbs);
  check('★ 顺序固定 = 学段 → 体系 → 知识点 → 考法（不跟 JSON 键序走）',
    lbs.slice(0, 4).map((t) => t.split('·')[0]), ['学段', '体系', '知识点', '考法']);
  checkTrue('★ 标签带取值（不是只有名字）', /学段·初中/.test(lbs[0] || ''), lbs[0]);
  checkTrue('★ 模型多给的键被丢掉（difficulty 不是这一组的）',
    lbs.every((t) => !/中档|difficulty/.test(t)), lbs);
  checkTrue('★ 「学段」有单独样式（它是唯一能拿来筛的枚举值）',
    !!D.querySelector('.g-card .g-label.lb-stage'), true);"""

sub(P, OLD, NEW)
print('done')
