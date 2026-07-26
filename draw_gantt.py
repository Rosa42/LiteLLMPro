import matplotlib
matplotlib.use('Agg')  # Non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import os

# 1. 设置支持中文的字体
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'Arial Unicode MS', 'sans-serif']
plt.rcParams['axes.unicode_minus'] = False

# 2. 准备项目数据 (任务名称, 开始周, 持续周数, 类别颜色)
tasks = [
    ("T10 上线部署与验收", 6, 1, "#de425b"),
    ("T9 性能、安全与异常测试", 5, 1, "#f47a60"),
    ("T8 前后端联调与集成测试", 4, 1, "#e4bcad"),
    ("T7 数据库与部署环境准备", 1, 2, "#93b499"),
    ("T6 前端管理界面开发", 2, 2, "#488f31"),
    ("T5 鉴权、限流与日志开发", 2, 2, "#689f38"),
    ("T4 后端中转核心开发", 2, 2, "#87af42"),
    ("T3 前端原型设计", 1, 1, "#b5cc95"),
    ("T2 API 接口规范设计", 1, 1, "#d1dbaa"),
    ("T1 需求分析与范围确认", 0, 1, "#f1e7be"),
]

# 3. 初始化画布
fig, ax = plt.subplots(figsize=(11, 6), dpi=120)

# 4. 循环绘制每一个条形
for i, (task_name, start_week, duration, color) in enumerate(tasks):
    ax.barh(i, width=duration, left=start_week, align='center',
            color=color, alpha=0.9, edgecolor='grey', height=0.55)
    ax.text(start_week + duration/2, i, f"{duration}周",
            ha='center', va='center', color='black', fontsize=9, fontweight='bold')

# 5. 美化时间轴
ax.set_xlim(0, 6)
ax.set_xticks(range(7))
ax.set_xticklabels([f"W{i}\n(第{i}周)" if i > 0 else "项目启动" for i in range(7)])
ax.xaxis.grid(True, linestyle='--', alpha=0.6, color='#cccccc')

# 6. 美化任务列表
ax.set_yticks(range(len(tasks)))
ax.set_yticklabels([t[0] for t in tasks], fontsize=10, fontweight='bold')

# 7. 关键契约交接线
ax.axvline(x=1, color='#e056fd', linestyle='-.', linewidth=1.5, alpha=0.8)
ax.text(1.05, len(tasks)-0.3, "🎯 API接口规范冻结 (开启多路并行)",
        color='#be2edd', fontsize=9, fontweight='bold', va='center')

# 8. 联调交接线
ax.axvline(x=4, color='#eb4d4b', linestyle='-', linewidth=1.5, alpha=0.8)
ax.text(4.05, 0.3, "💻 多路代码冻结 (进入集成联调)",
        color='#eb4d4b', fontsize=9, fontweight='bold', va='center')

# 9. 图例
legend_labels = [
    mpatches.Patch(color='#f1e7be', label='筹备与设计阶段'),
    mpatches.Patch(color='#488f31', label='四路并行开发阶段'),
    mpatches.Patch(color='#de425b', label='多路集成交付阶段'),
]
ax.legend(handles=legend_labels, loc='lower right', frameon=True, facecolor='#ffffff', edgecolor='#e0e0e0')

# 10. 收尾调整
ax.set_title("API 中转站项目多路协同甘特图 (契约驱动模型)", fontsize=14, pad=20, fontweight='bold')
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
ax.spines['left'].set_color('#cccccc')
ax.spines['bottom'].set_color('#cccccc')

plt.tight_layout()

# 11. 保存图片
output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_gantt_chart.png")
plt.savefig(output_path, dpi=300, bbox_inches='tight')
print(f"图片已保存: {output_path}")
print(f"文件大小: {os.path.getsize(output_path) / 1024:.1f} KB")
