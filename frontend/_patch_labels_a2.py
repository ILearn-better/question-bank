# -*- coding: utf-8 -*-
r"""一次性补丁 2：给 A 页面的挂载检查补「分类标签」断言（锚点按实际内容改）。"""
import io

P = 'grade_mount_check.js'
s = io.open(P, encoding='utf-8').read()

OLD = """    check('★ 只跳过去，**不自动把这块设成不判**（模型说像卷头也可能读错）',
      vm.regionBlocks.filter((b) => b.skipped).length, skipBefore);
  } finally {"""

NEW = r"""    check('★ 只跳过去，**不自动把这块设成不判**（模型说像卷头也可能读错）',
      vm.regionBlocks.filter((b) => b.skipped).length, skipBefore);
  } finally {"""

assert OLD in s, '锚点'

# 在这段之后插入新的一节：先定位这一节的收尾
ANCHOR = """    check('★ 只跳过去，**不自动把这块设成不判**（模型说像卷头也可能读错）',
      vm.regionBlocks.filter((b) => b.skipped).length, skipBefore);
  } finally {"""
assert ANCHOR in s

NEW_SECTION = r"""
  /* ============ 模型给的分类标签（0031） ============ */
  /* 用户原话：「ai输出中要附带对这道题的标签(例如:学段\知识点\体系\考法等)」。
   * 与工作台同一组断言（两个界面都要显示，规则必须一致）：
   *   ① 顺序固定（JSON 键序跨语言不保证，直接遍历 Object.keys 会让标签跳来跳去）
   *   ② 模型多给的键要丢掉（difficulty 不是这一组的）
   *   ③ 「学段」有单独样式 —— 它是唯一能拿来筛的枚举值 */
  vm.detail.items = vm.detail.items.map((x, i) => Object.assign({}, x,
    i === 0 ? { labels: { exam: '公式变形', stage: '初中', kp: '一元二次方程',
                          system: '数与代数', difficulty: '中档' } } : {}));
  await sleep(200);
  const lbs = Array.from(D.querySelectorAll('.g-card .g-label')).map((e) => e.textContent.trim());
  checkTrue('★ A 页面也显示了分类标签', lbs.length >= 4, lbs);
  check('★ 顺序固定 = 学段 → 体系 → 知识点 → 考法（不跟 JSON 键序走）',
    lbs.slice(0, 4).map((t) => t.split('·')[0]), ['学段', '体系', '知识点', '考法']);
  checkTrue('★ 标签带取值（不是只有名字）', /学段·初中/.test(lbs[0] || ''), lbs[0]);
  checkTrue('★ 模型多给的键被丢掉（difficulty 不是这一组的）',
    lbs.every((t) => !/中档|difficulty/.test(t)), lbs);
  checkTrue('★ 「学段」有单独样式（它是唯一能拿来筛的枚举值）',
    !!D.querySelector('.g-card .g-label.lb-stage'), true);
"""

# 找到「模板反引号计数」那一节之前插入（那里是全文件共用的静态断言区）
MARK = "  /* ============ 模板静态断言"
if MARK not in s:
    MARK = "  check('★ 模板字面量里反引号只有 2 个"
assert MARK in s, '找不到插入点'

s = s.replace(MARK, NEW_SECTION + "\n" + MARK, 1)
io.open(P, 'w', encoding='utf-8', newline='\n').write(s)
print('patched')
