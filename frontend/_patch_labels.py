# -*- coding: utf-8 -*-
r"""一次性补丁：给两个挂载检查补「分类标签」那一节断言。

用脚本文件改而不是 heredoc：bash 会吃掉一层反斜杠（踩了三次）。
"""
import io


def sub(path, old, new):
    s = io.open(path, encoding='utf-8').read()
    assert old in s, '没找到锚点：%s' % path
    io.open(path, 'w', encoding='utf-8', newline='\n').write(s.replace(old, new, 1))
    print('patched', path)


# ==================================================================
# 1) 工作台（workbench_check.js）：给三题各配一组 labels，再加断言
# ==================================================================
sub('workbench_check.js',
    "        confidence: 'high', kp_name: '一元二次方程', tags: [], error_reason: '',\n"
    "        flags: [], figure_note: '', block_kind: 'question', tail_note: '', status: 'pending' },",
    "        confidence: 'high', kp_name: '一元二次方程', tags: [], error_reason: '',\n"
    "        flags: [], figure_note: '', block_kind: 'question', tail_note: '', status: 'pending',\n"
    "        /* 0031：模型给的分类标签。故意**乱序 + 带一个空值 + 带一个多余的键**，\n"
    "           用来验「显示顺序固定 / 空值不渲染半个标签 / 多余键被丢掉」。 */\n"
    "        labels: { exam: '公式变形', stage: '初中', kp: '一元二次方程',\n"
    "                  system: '数与代数', difficulty: '中档', blank: '   ' } },")

sub('workbench_check.js',
    "        flags: ['low_confidence'], figure_note: '',\n"
    "        block_kind: 'question',",
    "        flags: ['low_confidence'], figure_note: '',\n"
    "        block_kind: 'question',\n"
    "        labels: { stage: '初中', system: '图形与几何', kp: '等腰三角形性质',\n"
    "                  exam: '逻辑推理' },")

# 第 3 题（框里没读到题）：**不给** labels —— 界面上就不该有那一行
sub('workbench_check.js',
    "        flags: ['region_empty'], figure_note: '', block_kind: 'header', tail_note: '',\n"
    "        status: 'pending' },",
    "        flags: ['region_empty'], figure_note: '', block_kind: 'header', tail_note: '',\n"
    "        status: 'pending', labels: {} },")

OLD = """  check('判不了的那一题给了原因（不能只留一个灰点）',
    D.querySelectorAll('.g-un').length, 1);"""

NEW = r"""  check('判不了的那一题给了原因（不能只留一个灰点）',
    D.querySelectorAll('.g-un').length, 1);

  /* ============ 13b. 模型给的分类标签（0031） ============ */
  /* 用户原话：「ai输出中要附带对这道题的标签(例如:学段\知识点\体系\考法等)」。
   * 三个容易做错的点：
   *   ① **顺序必须固定**。JSON 的键序跨语言（Python → JSON → JS）之后不保证，
   *      直接遍历 Object.keys() 会让四个标签的先后跳来跳去。用 LABEL_ORDER 数组。
   *   ② **空值不能渲染成半个标签**（「学段·」比没有还难看）。
   *   ③ **模型多给的键要丢掉**（difficulty 不是这组的，界面上不该出现）。
   *   ④ 没有标签的题（比如框里没读到题）**整行都不显示**，不留空行。 */
  const lb1 = Array.from(gcards[0].querySelectorAll('.g-label')).map((e) => e.textContent.trim());
  check('★ 第 1 题有 4 个标签', lb1.length, 4);
  check('★ 显示顺序固定 = 学段 → 体系 → 知识点 → 考法（不跟 JSON 键序走）',
    lb1.map((t) => t.split('·')[0]), ['学段', '体系', '知识点', '考法']);
  checkTrue('★ 标签带取值（不是只有名字）', lb1[0] === '学段·初中', lb1[0]);
  checkTrue('★ 模型多给的键被丢掉（difficulty 不是这一组的）',
    lb1.every((t) => !/中档|difficulty/.test(t)), lb1);
  checkTrue('★ 空值不渲染成半个标签（没有「空·」这种）',
    lb1.every((t) => !/·\s*$/.test(t)), lb1);
  checkTrue('★ 「学段」有单独的样式（它是唯一能拿来筛的枚举）',
    !!gcards[0].querySelector('.g-label.lb-stage'), true);
  check('第 2 题也有标签', gcards[1].querySelectorAll('.g-label').length, 4);
  check('★ 没标签的题 → 整行都不显示（不留一条空行）',
    gcards[2].querySelectorAll('.g-label').length, 0);"""

sub('workbench_check.js', OLD, NEW)

# ==================================================================
# 2) A 页面（grade_mount_check.js）：给桩的 item 加 labels 并断言
# ==================================================================
sub('grade_mount_check.js',
    "  figure_note: '', status: 'pending', question_id: null,\n}, o);",
    "  figure_note: '', status: 'pending', question_id: null,\n"
    "  labels: {},                 // 0031：模型给的分类标签（学段/体系/知识点/考法）\n}, o);")

sub('grade_mount_check.js',
    "    item({ id: 101, seq: 1, q_no: '1', qtype: '解答题', verdict: 'correct',",
    "    item({ id: 101, seq: 1, q_no: '1', qtype: '解答题', verdict: 'correct',\n"
    "      labels: { stage: '初中', system: '数与代数', kp: '一元二次方程',\n"
    "                exam: '公式变形', difficulty: '中档' },")

OLD_A = """    check('★ 只跳过去，**不自动把这块设成不判**（模型说像卷头也可能读错）',
      vm.regions.filter((r) => r.skipped).length, 0);"""

NEW_A = r"""    check('★ 只跳过去，**不自动把这块设成不判**（模型说像卷头也可能读错）',
      vm.regions.filter((r) => r.skipped).length, 0);

  /* ============ 模型给的分类标签（0031） ============ */
  /* 与工作台同一组断言（两处界面都要显示，规则必须一致）。 */
  vm.detail.items = vm.detail.items.map((x, i) => Object.assign({}, x,
    i === 0 ? { labels: { exam: '公式变形', stage: '初中', kp: '一元二次方程',
                          system: '数与代数', difficulty: '中档' } } : {}));
  await sleep(120);
  const lbs = Array.from(D.querySelectorAll('.g-card .g-label')).map((e) => e.textContent.trim());
  checkTrue('★ A 页面也显示了分类标签', lbs.length >= 4, lbs);
  check('★ 顺序固定 = 学段 → 体系 → 知识点 → 考法（不跟 JSON 键序走）',
    lbs.slice(0, 4).map((t) => t.split('·')[0]), ['学段', '体系', '知识点', '考法']);
  checkTrue('★ 模型多给的键被丢掉（difficulty 不是这一组的）',
    lbs.every((t) => !/中档|difficulty/.test(t)), lbs);
  checkTrue('★ 「学段」有单独样式（它是唯一能拿来筛的枚举值）',
    !!D.querySelector('.g-card .g-label.lb-stage'), true);"""

sub('grade_mount_check.js', OLD_A, NEW_A)
print('done')
