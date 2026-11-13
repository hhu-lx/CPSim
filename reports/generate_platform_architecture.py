from pathlib import Path

OUT = Path(__file__).resolve().parent / "platform_architecture.svg"

def esc(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def text(x, y, value, size=15, anchor="middle", weight="400", fill="#183B56"):
    return f'<text x="{x}" y="{y}" font-family="Times New Roman, serif" font-size="{size}px" font-weight="{weight}" text-anchor="{anchor}" fill="#000000">{esc(value)}</text>'

def box(x, y, w, h, title, subtitle, fill, stroke):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
            + text(x + w / 2, y + 31, title, 14, weight="500")
            + text(x + w / 2, y + 56, subtitle, 10, fill="#425466"))

def arrow(x1, y1, x2, y2, color="#607D8B", label=None):
    out = f'<path d="M{x1},{y1} L{x2},{y2}" stroke="{color}" stroke-width="1.6" fill="none" marker-end="url(#arrow)"/>'
    if label:
        out += text((x1 + x2) / 2, (y1 + y2) / 2 - 5, label, 9, fill="#425466")
    return out

def main():
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1040" viewBox="0 0 1600 1040">',
           '<defs><marker id="arrow" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L8,3 z" fill="#607D8B"/></marker></defs>',
           '<rect width="1600" height="1040" fill="#FFFFFF"/>',
           text(800, 42, "算电协同平台总体架构", 26, weight="600"),
           text(800, 70, "基础时序数据驱动的任务调度—电网仿真—协同优化闭环", 14, fill="#607D8B")]
    for y, label in [(100, "数据输入层"), (300, "仿真与调度层"), (515, "优化决策层"), (730, "电网计算层"), (940, "输出与分析层")]:
        svg.append(text(30, y + 42, label, 13, fill="#607D8B"))
        svg.append(f'<line x1="65" y1="{y-14}" x2="1560" y2="{y-14}" stroke="#D9E2EC" stroke-width="1"/>')

    for x, title, sub in [(95, "平台配置", "platform.yaml"), (380, "任务数据表", "提交时间、时长、算力需求"), (665, "电网时序", "负荷 / 光伏 / 风电"), (950, "基础发电表", "base_generation_pu_second.csv"), (1235, "设备功率曲线", "GPU、制冷、UPS、网络")]:
        svg.append(box(x, 105, 240, 82, title, sub, "#EEF5FA", "#284B63"))
    for x, title, sub in [(135, "仿真时钟与数据加载", "秒级推进；现实时间映射；时序读取"), (535, "任务生命周期管理", "提交 → 调度 → 开始 → 结束"), (935, "数据中心设备仿真", "算力、制冷、UPS、网络、PUE")]:
        svg.append(box(x, 305, 330, 90, title, sub, "#EAF4F4", "#2A6F73"))
    for x, title, sub in [(135, "模式一：非协同优化", "固定峰谷电价 + 规则调度"), (535, "模式二：分层优化", "先分配数据中心，再优化能源"), (935, "模式三：联合优化", "任务分配与能源调度一体化")]:
        svg.append(box(x, 520, 330, 90, title, sub, "#FFF6E5", "#C9851A"))
    svg.append(text(800, 635, "目标函数接口：成本、碳排放、延迟或用户自定义目标", 12, fill="#8A5A00"))
    for x, title, sub in [(100, "节点负荷构造", "基础负荷 + 任务增量负荷"), (430, "发电构造", "基础发电 + 新能源 + 增量发电"), (760, "电网模型", "Pandapower IEEE 39 节点"), (1090, "潮流与评价", "电价、碳排放、约束")]:
        svg.append(box(x, 745, 280, 90, title, sub, "#F1F0FA", "#5E548E"))
    for x, title, sub in [(135, "任务结果", "task_execution.csv"), (485, "电网结果", "simulation_stats.csv"), (835, "可视化报告", "功率、任务、算力、时长"), (1185, "实时日志", "状态与统计")]:
        svg.append(box(x, 955, 280, 58, title, sub, "#F7F7F7", "#607D8B"))

    for x in [215, 500, 785, 1070, 1355]:
        svg.append(arrow(x, 187, x if x < 800 else 1100, 305, "#8AA3B3"))
    for start, end in [((300, 350), (535, 350)), ((865, 350), (935, 350))]:
        svg.append(arrow(*start, *end, "#2A6F73"))
    svg.extend([arrow(700, 395, 700, 520, "#607D8B", "任务与设备状态"), arrow(300, 395, 300, 520), arrow(1100, 395, 1100, 520), arrow(300, 610, 240, 745, "#C9851A"), arrow(700, 610, 570, 745, "#C9851A", "COPT"), arrow(1100, 610, 900, 745, "#C9851A", "COPT"), arrow(380, 790, 430, 790, "#5E548E"), arrow(710, 790, 760, 790, "#5E548E"), arrow(1040, 790, 1090, 790, "#5E548E"), arrow(240, 835, 275, 955), arrow(570, 835, 625, 955), arrow(900, 835, 975, 955), arrow(1230, 835, 1325, 955)])
    svg.append('</svg>')
    OUT.write_text("\n".join(svg), encoding="utf-8")
    print(OUT)

if __name__ == "__main__":
    main()
